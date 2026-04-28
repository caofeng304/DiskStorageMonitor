import os
import sys
import json
import base64
import shutil
import time
import threading
import tkinter as tk
from tkinter import filedialog, ttk, messagebox, simpledialog
from pathlib import Path
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

# ==========================================
# 全局配置与常量
# ==========================================
ADMIN_PASSWORD = "teslaadmin"  # 管理员密码

# 第三方库依赖检查
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    HAS_SCHEDULER = True
except ImportError:
    HAS_SCHEDULER = False
    print("错误: 缺少 apscheduler 库。请运行 pip install apscheduler")

try:
    import sv_ttk
    THEME_AVAILABLE = True
except ImportError:
    THEME_AVAILABLE = False

try:
    from asyncua import ua
    from asyncua.sync import Client as OpcUaClient
    HAS_OPCUA = True
except ImportError:
    ua = None
    OpcUaClient = None
    HAS_OPCUA = False
    print("警告: 缺少 asyncua 库。PLC OPC UA 功能不可用，请运行 pip install asyncua")

CONFIG_FILE = Path("config.json")


class Assets:
    """资源管理类"""
    # 注意：为了代码简洁，这里保留了你原本的截断 Base64。
    # 如果你本地没有 tesla.ico 文件，请将此处替换回完整的 Base64 字符串，否则回退图标会加载失败。
    TESLA_ICON_B64 = """iVBORw0KGgoAAAANSUhEUgAAADAAAAAwCAYAAABXAvmHAAAABGdB..."""


class OpcUaSignalClient:
    """负责与西门子 PLC 进行 OPC UA 心跳和 deleting 信号通信。"""

    HEARTBEAT_INTERVAL = 1.0
    RECONNECT_DELAY = 3.0

    def __init__(self, status_callback: Optional[Callable[[str], None]] = None):
        self.status_callback = status_callback or (lambda _text: None)
        self._config: Dict[str, Any] = {}
        self._client: Any = None
        self._heartbeat_node: Any = None
        self._deleting_node: Any = None
        self._heartbeat_variant_type: Any = None
        self._deleting_variant_type: Any = None
        self._heartbeat_sample: Any = False
        self._deleting_sample: Any = False
        self._heartbeat_state = False
        self._last_error = ""
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()

    def reconfigure(self, config: Dict[str, Any]) -> bool:
        normalized = self._normalize_config(config)
        with self._lock:
            changed = normalized != self._config
            self._config = normalized
        if changed and self.is_running():
            self.restart()
        return changed

    def is_running(self) -> bool:
        return bool(self._heartbeat_thread and self._heartbeat_thread.is_alive())

    def start(self) -> tuple[bool, str]:
        cfg = self._snapshot_config()
        if not cfg.get("plc_enabled"):
            return False, "PLC OPC UA 未启用"
        if not HAS_OPCUA:
            return False, "缺少 asyncua 依赖，无法启动 PLC OPC UA"
        if not cfg.get("plc_endpoint"):
            return False, "PLC Endpoint 未配置"
        if not cfg.get("plc_heartbeat_node"):
            return False, "PLC 心跳 NodeId 未配置"
        if self.is_running():
            return True, "PLC OPC UA 心跳已运行"

        self._stop_event = threading.Event()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="opcua-heartbeat",
            daemon=True
        )
        self._heartbeat_thread.start()
        return True, "PLC OPC UA 心跳线程已启动"

    def stop(self) -> str:
        thread = self._heartbeat_thread
        self._stop_event.set()
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        with self._lock:
            self._safe_reset_outputs_locked()
            self._disconnect_locked()
            self._heartbeat_thread = None
            self._heartbeat_state = False
        return "PLC OPC UA 已停止"

    def restart(self) -> tuple[bool, str]:
        self.stop()
        return self.start()

    def set_deleting(self, active: bool):
        cfg = self._snapshot_config()
        if not (cfg.get("plc_enabled") and cfg.get("plc_deleting_node") and HAS_OPCUA):
            return

        with self._lock:
            try:
                self._ensure_connected_locked()
                if self._deleting_node is None:
                    return
                self._write_logical_value_locked(
                    node=self._deleting_node,
                    variant_type=self._deleting_variant_type,
                    sample_value=self._deleting_sample,
                    state=active,
                    active_text="deleting",
                    inactive_text="idle"
                )
                self._last_error = ""
            except Exception as exc:
                self._handle_error_locked(f"PLC deleting 信号写入失败: {exc}")

    def _heartbeat_loop(self):
        while not self._stop_event.is_set():
            try:
                with self._lock:
                    self._ensure_connected_locked()
                    self._heartbeat_state = not self._heartbeat_state
                    self._write_logical_value_locked(
                        node=self._heartbeat_node,
                        variant_type=self._heartbeat_variant_type,
                        sample_value=self._heartbeat_sample,
                        state=self._heartbeat_state,
                        active_text="1",
                        inactive_text="0"
                    )
                    self._last_error = ""
            except Exception as exc:
                with self._lock:
                    self._handle_error_locked(f"PLC 心跳写入失败: {exc}")
                if self._stop_event.wait(self.RECONNECT_DELAY):
                    break
                continue

            if self._stop_event.wait(self.HEARTBEAT_INTERVAL):
                break

        with self._lock:
            self._safe_reset_outputs_locked()
            self._disconnect_locked()
            self._heartbeat_thread = None
            self._heartbeat_state = False

    def _ensure_connected_locked(self):
        if self._client is not None:
            return

        cfg = self._config
        client = OpcUaClient(cfg["plc_endpoint"], timeout=4)
        if cfg.get("plc_security"):
            client.set_security_string(cfg["plc_security"])
        if cfg.get("plc_username"):
            client.set_user(cfg["plc_username"])
        if cfg.get("plc_password"):
            client.set_password(cfg["plc_password"])

        client.connect()

        heartbeat_node = client.get_node(cfg["plc_heartbeat_node"])
        heartbeat_sample = heartbeat_node.read_value()
        heartbeat_variant_type = heartbeat_node.read_data_type_as_variant_type()

        deleting_node = None
        deleting_sample = False
        deleting_variant_type = None
        if cfg.get("plc_deleting_node"):
            deleting_node = client.get_node(cfg["plc_deleting_node"])
            deleting_sample = deleting_node.read_value()
            deleting_variant_type = deleting_node.read_data_type_as_variant_type()

        self._client = client
        self._heartbeat_node = heartbeat_node
        self._heartbeat_sample = heartbeat_sample
        self._heartbeat_variant_type = heartbeat_variant_type
        self._deleting_node = deleting_node
        self._deleting_sample = deleting_sample
        self._deleting_variant_type = deleting_variant_type
        self._notify("PLC OPC UA 已连接")

    def _safe_reset_outputs_locked(self):
        if self._client is None:
            return

        try:
            if self._heartbeat_node is not None:
                self._write_logical_value_locked(
                    node=self._heartbeat_node,
                    variant_type=self._heartbeat_variant_type,
                    sample_value=self._heartbeat_sample,
                    state=False,
                    active_text="1",
                    inactive_text="0"
                )
        except Exception:
            pass

        try:
            if self._deleting_node is not None:
                self._write_logical_value_locked(
                    node=self._deleting_node,
                    variant_type=self._deleting_variant_type,
                    sample_value=self._deleting_sample,
                    state=False,
                    active_text="deleting",
                    inactive_text="idle"
                )
        except Exception:
            pass

    def _disconnect_locked(self):
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:
                pass
        self._client = None
        self._heartbeat_node = None
        self._deleting_node = None
        self._heartbeat_variant_type = None
        self._deleting_variant_type = None

    def _write_logical_value_locked(self, node, variant_type, sample_value, state: bool,
                                    active_text: str, inactive_text: str):
        value = self._build_value(variant_type, sample_value, state, active_text, inactive_text)
        node.write_value(value, variant_type)

    def _build_value(self, variant_type, sample_value, state: bool,
                     active_text: str, inactive_text: str):
        if HAS_OPCUA and variant_type == ua.VariantType.Boolean:
            return bool(state)
        if HAS_OPCUA and variant_type in {
            ua.VariantType.SByte,
            ua.VariantType.Byte,
            ua.VariantType.Int16,
            ua.VariantType.UInt16,
            ua.VariantType.Int32,
            ua.VariantType.UInt32,
            ua.VariantType.Int64,
            ua.VariantType.UInt64,
        }:
            return 1 if state else 0
        if HAS_OPCUA and variant_type in {ua.VariantType.Float, ua.VariantType.Double}:
            return 1.0 if state else 0.0
        if HAS_OPCUA and variant_type == ua.VariantType.ByteString:
            return (active_text if state else inactive_text).encode("utf-8")
        if HAS_OPCUA and variant_type == ua.VariantType.String:
            return active_text if state else inactive_text

        if isinstance(sample_value, bool):
            return bool(state)
        if isinstance(sample_value, int):
            return 1 if state else 0
        if isinstance(sample_value, float):
            return 1.0 if state else 0.0
        if isinstance(sample_value, (bytes, bytearray)):
            return (active_text if state else inactive_text).encode("utf-8")
        if isinstance(sample_value, str):
            return active_text if state else inactive_text
        return bool(state)

    def _handle_error_locked(self, message: str):
        if message != self._last_error:
            self._last_error = message
            self._notify(message)
        self._disconnect_locked()

    def _notify(self, text: str):
        if text:
            self.status_callback(text)

    def _snapshot_config(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._config)

    @staticmethod
    def _normalize_config(config: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "plc_enabled": bool(config.get("plc_enabled")),
            "plc_endpoint": str(config.get("plc_endpoint", "")).strip(),
            "plc_username": str(config.get("plc_username", "")).strip(),
            "plc_password": str(config.get("plc_password", "")),
            "plc_security": str(config.get("plc_security", "")).strip(),
            "plc_heartbeat_node": str(config.get("plc_heartbeat_node", "")).strip(),
            "plc_deleting_node": str(config.get("plc_deleting_node", "")).strip(),
        }


# ==========================================
# 核心逻辑类
# ==========================================
class DiskCleanerCore:
    def __init__(self, plc_client: Optional[OpcUaSignalClient] = None):
        self.scheduler = None
        self.task_running = False
        self.plc_client = plc_client
        if HAS_SCHEDULER:
            self.scheduler = BackgroundScheduler()
        else:
            self.scheduler = None

    def set_plc_client(self, plc_client: OpcUaSignalClient):
        self.plc_client = plc_client

    def get_disk_free_gb(self, disk_path: str) -> float:
        try:
            if not os.path.exists(disk_path):
                return 0.0
            total, used, free = shutil.disk_usage(disk_path)
            return free / (1024 ** 3)
        except Exception as e:
            print(f"磁盘读取错误: {e}")
            return 0.0

    def perform_cleanup(self, folder: str, n_files: int, log_path: str) -> List[str]:
        """
        执行真实清理逻辑：直接删除文件
        """
        folder_path = Path(folder)
        if not folder_path.is_dir():
            return [f"错误: 文件夹不存在 {folder}"]

        deleted_files_log = []

        deleting_signal_active = False

        try:
            files = [f for f in folder_path.iterdir() if f.is_file()]
            if not files:
                return []

            # 按修改时间排序（最旧的在前）
            files_sorted = sorted(files, key=lambda x: x.stat().st_mtime)
            files_to_process = files_sorted[:n_files]

            log_file = Path(log_path)
            if log_file.parent and not log_file.parent.exists():
                log_file.parent.mkdir(parents=True, exist_ok=True)

            if self.plc_client:
                self.plc_client.set_deleting(True)
                deleting_signal_active = True

            with open(log_file, "a", encoding="utf-8") as log:
                header = f"\n===== [Triggered] at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} =====\n"
                log.write(header)

                for f in files_to_process:
                    size_mb = f.stat().st_size / (1024 * 1024)
                    action_msg = f"File: {f.name} | Size: {size_mb:.2f} MB"
                    try:
                        f.unlink()  # 直接删除
                        log.write(f"[DELETED] {action_msg}\n")
                        deleted_files_log.append(f"[已删] {f.name}")
                    except Exception as e:
                        log.write(f"ERROR deleting {f.name}: {str(e)}\n")
                        deleted_files_log.append(f"[失败] {f.name}")

        except Exception as e:
            print(f"清理过程出错: {e}")
            return [f"系统错误: {e}"]
        finally:
            if deleting_signal_active and self.plc_client:
                self.plc_client.set_deleting(False)

        return deleted_files_log

    def run_check_job(self, config: Dict, status_callback, is_manual: bool = False):
        disk = config["disk"]
        threshold = config["threshold"]
        folder = config["folder"]
        delete_n = config["delete_n"]
        log_path = config["log_path"]

        free_gb = self.get_disk_free_gb(disk)
        timestamp = datetime.now().strftime('%H:%M:%S')
        status_base = f"[{timestamp}] 剩余: {free_gb:.2f} GB"

        should_run = is_manual or (free_gb < threshold)

        if should_run:
            trigger_reason = "手动" if is_manual else "空间不足"
            status_callback(f"{status_base} -> {trigger_reason} -> 执行清理...")

            results = self.perform_cleanup(folder, delete_n, log_path)

            if results:
                status_callback(f"{status_base} | 已删除 {len(results)} 个文件 (详见日志)")
            else:
                status_callback(f"{status_base} | 目标无需清理或为空")
        else:
            status_callback(f"{status_base} (空间充足)")

    def start_scheduler(self, config: Dict, status_callback) -> tuple[bool, str]:
        if not HAS_SCHEDULER:
            return False, "未安装 apscheduler"
        if self.task_running:
            return False, "监控已在运行中"

        run_days = [i for i, v in enumerate(config["days"]) if v]
        if not run_days:
            return False, "请至少选择一个星期几"
        if not os.path.isdir(config["folder"]):
            return False, "无效的清理文件夹路径"
        if not config["log_path"]:
            return False, "请设置日志路径"

        if not self.scheduler.running:
            self.scheduler.start()

        for d in run_days:
            self.scheduler.add_job(
                self.run_check_job,
                "cron",
                day_of_week=d,
                hour=config["hour"],
                minute=config["minute"],
                args=[config, status_callback, False],
                id=f"job_{d}",
                replace_existing=True
            )
        self.task_running = True
        return True, "定时监控已启动"

    def stop_scheduler(self):
        if self.scheduler and self.scheduler.running:
            self.scheduler.remove_all_jobs()
        self.task_running = False
        return "⛔ 监控已停止"


# ==========================================
# UI 主程序类
# ==========================================
class CleanerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("磁盘空间智能清理工具 Pro (Live)")
        self.minsize(760, 900)
        self.center_window(760, 900)

        self.core = DiskCleanerCore()
        self.config = self.load_config()

        self.setup_icon()

        if THEME_AVAILABLE:
            sv_ttk.set_theme(self.config.get("theme", "dark"))
        self.attributes("-alpha", self.config.get("alpha", 0.98))

        self.setup_ui_variables()
        self.plc_client = OpcUaSignalClient(self.update_status_safe)
        self.core.set_plc_client(self.plc_client)
        self.build_ui()
        self.load_settings_to_ui()
        self.refresh_plc_connection(show_status=False)

        # 自动倒计时启动监控
        self.auto_start_seconds = 30
        self.auto_start_job_id = None
        self.start_auto_start_countdown()

        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    # ====================== UI 布局 ======================
    def center_window(self, width, height):
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        x = int((screen_width - width) / 2)
        y = int((screen_height - height) / 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def setup_icon(self):
        """
        双重保险设置图标：
        1. iconbitmap: 用于窗口左上角 (必须是 .ico)
        2. iconphoto: 用于任务栏大图标 (支持 png/gif，这里我们尝试通过 .ico 或 Base64 生成)
        """
        icon_path = "tesla.ico"

        # ---------------------------------------------------------
        # 第一步：设置窗口左上角的小图标 (Window Icon)
        # ---------------------------------------------------------
        if os.path.exists(icon_path):
            try:
                self.iconbitmap(icon_path)
            except Exception as e:
                print(f"窗口图标设置失败: {e}")

        # ---------------------------------------------------------
        # 第二步：设置任务栏的大图标 (Taskbar Icon) - 关键步骤！
        # ---------------------------------------------------------
        # 注意：iconphoto 的第一个参数 True 表示“应用级默认”，这对任务栏生效至关重要
        try:
            # 优先尝试从 Base64 加载（因为 PhotoImage 对 Base64 支持很好）
            # 只要你的 Base64 数据是有效的图片数据（PNG/GIF）
            if hasattr(Assets, 'TESLA_ICON_B64') and Assets.TESLA_ICON_B64:
                icon_data = base64.b64decode(Assets.TESLA_ICON_B64)
                self.app_icon_image = tk.PhotoImage(data=icon_data)
                self.iconphoto(True, self.app_icon_image)
                print("任务栏图标已通过 Base64 设置")
                return

            # 如果 Base64 失败，尝试直接读取 tesla.ico 为图片对象
            # 注意：Tkinter 8.6+ 的 PhotoImage 通常可以直接读取 .ico，但有些格式不支持
            if os.path.exists(icon_path):
                self.app_icon_image = tk.PhotoImage(file=icon_path)
                self.iconphoto(True, self.app_icon_image)
                print("任务栏图标已通过文件设置")

        except Exception as e:
            print(f"任务栏图标设置失败 (请确保 Assets.TESLA_ICON_B64 是有效的): {e}")

    # ====================== 配置管理 ======================
    def load_config(self):
        default_cfg = {
            "days": [0] * 7, "hour": 2, "minute": 0, "disk": "C:/",
            "threshold": 100.0, "folder": "", "delete_n": 10,
            "log_path": str(Path.cwd() / "cleaner_log.txt"),
            "alpha": 0.98, "theme": "dark",
            "plc_enabled": False,
            "plc_endpoint": "",
            "plc_username": "",
            "plc_password": "",
            "plc_security": "",
            "plc_heartbeat_node": "",
            "plc_deleting_node": "",
        }
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for key, val in default_cfg.items():
                        data.setdefault(key, val)
                    return data
            except:
                return default_cfg
        return default_cfg

    def save_config(self):
        data = self.get_current_config()
        data["alpha"] = self.alpha_var.get()
        data["theme"] = self.theme_var.get()
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            messagebox.showerror("保存失败", f"无法保存配置: {e}")

    # ====================== UI 变量 ======================
    def setup_ui_variables(self):
        self.day_vars = [tk.IntVar() for _ in range(7)]
        self.hour_var = tk.IntVar(value=2)
        self.minute_var = tk.IntVar(value=0)
        self.disk_var = tk.StringVar(value="C:/")
        self.threshold_var = tk.DoubleVar(value=100.0)
        self.folder_var = tk.StringVar()
        self.delete_var = tk.IntVar(value=10)
        self.log_var = tk.StringVar()
        self.status_var = tk.StringVar(value="准备就绪")
        self.alpha_var = tk.DoubleVar(value=self.config.get("alpha", 0.98))
        self.theme_var = tk.StringVar(value=self.config.get("theme", "dark"))
        self.plc_enabled_var = tk.IntVar(value=1 if self.config.get("plc_enabled") else 0)
        self.plc_endpoint_var = tk.StringVar(value=self.config.get("plc_endpoint", ""))
        self.plc_username_var = tk.StringVar(value=self.config.get("plc_username", ""))
        self.plc_password_var = tk.StringVar(value=self.config.get("plc_password", ""))
        self.plc_security_var = tk.StringVar(value=self.config.get("plc_security", ""))
        self.plc_heartbeat_node_var = tk.StringVar(value=self.config.get("plc_heartbeat_node", ""))
        self.plc_deleting_node_var = tk.StringVar(value=self.config.get("plc_deleting_node", ""))

    def load_settings_to_ui(self):
        cfg = self.config
        for i, v in enumerate(cfg["days"]):
            self.day_vars[i].set(v)
        self.hour_var.set(cfg["hour"])
        self.minute_var.set(cfg["minute"])
        self.disk_var.set(cfg["disk"])
        self.threshold_var.set(cfg["threshold"])
        self.folder_var.set(cfg["folder"])
        self.delete_var.set(cfg["delete_n"])
        self.log_var.set(cfg["log_path"])
        self.plc_enabled_var.set(1 if cfg.get("plc_enabled") else 0)
        self.plc_endpoint_var.set(cfg.get("plc_endpoint", ""))
        self.plc_username_var.set(cfg.get("plc_username", ""))
        self.plc_password_var.set(cfg.get("plc_password", ""))
        self.plc_security_var.set(cfg.get("plc_security", ""))
        self.plc_heartbeat_node_var.set(cfg.get("plc_heartbeat_node", ""))
        self.plc_deleting_node_var.set(cfg.get("plc_deleting_node", ""))

    # ====================== UI 构建 ======================
    def build_ui(self):
        main_container = ttk.Frame(self, padding=20)
        main_container.pack(fill=tk.BOTH, expand=True)

        # 外观设置
        app_frame = ttk.LabelFrame(main_container, text="⚙️ 系统设置", padding=10)
        app_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(app_frame, text="透明度:").pack(side=tk.LEFT, padx=5)
        ttk.Scale(app_frame, from_=0.5, to=1.0, variable=self.alpha_var,
                  command=self.update_alpha).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=10)
        if THEME_AVAILABLE:
            ttk.Button(app_frame, text="🌓 切换主题", command=self.toggle_theme).pack(side=tk.RIGHT, padx=5)

        # 调度配置
        sched_frame = ttk.LabelFrame(main_container, text="🕒 调度配置 (Schedule)", padding=10)
        sched_frame.pack(fill=tk.X, pady=(0, 10))
        d_frame = ttk.Frame(sched_frame)
        d_frame.pack(anchor=tk.W, pady=5)
        ttk.Label(d_frame, text="重复: ").pack(side=tk.LEFT)
        for i, txt in enumerate(["一", "二", "三", "四", "五", "六", "日"]):
            ttk.Checkbutton(d_frame, text=txt, variable=self.day_vars[i]).pack(side=tk.LEFT, padx=2)
        t_frame = ttk.Frame(sched_frame)
        t_frame.pack(anchor=tk.W, pady=5)
        ttk.Label(t_frame, text="时间: ").pack(side=tk.LEFT)
        ttk.Spinbox(t_frame, from_=0, to=23, width=4, textvariable=self.hour_var).pack(side=tk.LEFT)
        ttk.Label(t_frame, text=":").pack(side=tk.LEFT)
        ttk.Spinbox(t_frame, from_=0, to=59, width=4, textvariable=self.minute_var).pack(side=tk.LEFT)

        # 核心规则
        rule_frame = ttk.LabelFrame(main_container, text="🛡️ 清理策略 (Strategy)", padding=10)
        rule_frame.pack(fill=tk.X, pady=(0, 10))
        rule_frame.columnconfigure(1, weight=1)

        ttk.Label(rule_frame, text="监控分区:").grid(row=0, column=0, sticky=tk.W)
        ttk.Combobox(rule_frame, textvariable=self.disk_var,
                     values=self.get_drive_letters(), width=10).grid(row=0, column=1, sticky=tk.W, padx=5, pady=5)

        ttk.Label(rule_frame, text="剩余空间低于(GB):").grid(row=1, column=0, sticky=tk.W)
        ttk.Spinbox(rule_frame, from_=1, to=9999, increment=10, textvariable=self.threshold_var, width=10).grid(
            row=1, column=1, sticky=tk.W, padx=5, pady=5)

        ttk.Label(rule_frame, text="每次删除最旧文件数:").grid(row=2, column=0, sticky=tk.W)
        ttk.Spinbox(rule_frame, from_=1, to=999, textvariable=self.delete_var, width=10).grid(
            row=2, column=1, sticky=tk.W, padx=5, pady=5)

        # 路径设置
        path_frame = ttk.LabelFrame(main_container, text="📂 路径 (Paths)", padding=10)
        path_frame.pack(fill=tk.X, pady=(0, 10))
        path_frame.columnconfigure(1, weight=1)

        ttk.Label(path_frame, text="清理目标:").grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(path_frame, textvariable=self.folder_var).grid(row=0, column=1, sticky=tk.EW, padx=5, pady=5)
        ttk.Button(path_frame, text="...", width=3, command=self.choose_folder).grid(row=0, column=2)

        ttk.Label(path_frame, text="日志文件:").grid(row=1, column=0, sticky=tk.W)
        ttk.Entry(path_frame, textvariable=self.log_var).grid(row=1, column=1, sticky=tk.EW, padx=5, pady=5)
        ttk.Button(path_frame, text="...", width=3, command=self.choose_log).grid(row=1, column=2)
        ttk.Button(path_frame, text="打开", width=5, command=self.open_log).grid(row=1, column=3, padx=(2, 0))

        plc_frame = ttk.LabelFrame(main_container, text="PLC / OPC UA", padding=10)
        plc_frame.pack(fill=tk.X, pady=(0, 10))
        plc_frame.columnconfigure(1, weight=1)
        plc_frame.columnconfigure(3, weight=1)

        ttk.Checkbutton(plc_frame, text="启用 PLC 通信", variable=self.plc_enabled_var).grid(
            row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 5)
        )
        ttk.Button(plc_frame, text="重连 PLC", command=self.on_plc_reconnect).grid(
            row=0, column=3, sticky=tk.E, pady=(0, 5)
        )

        ttk.Label(plc_frame, text="Endpoint:").grid(row=1, column=0, sticky=tk.W)
        ttk.Entry(plc_frame, textvariable=self.plc_endpoint_var).grid(
            row=1, column=1, columnspan=3, sticky=tk.EW, padx=5, pady=5
        )

        ttk.Label(plc_frame, text="Heartbeat NodeId:").grid(row=2, column=0, sticky=tk.W)
        ttk.Entry(plc_frame, textvariable=self.plc_heartbeat_node_var).grid(
            row=2, column=1, columnspan=3, sticky=tk.EW, padx=5, pady=5
        )

        ttk.Label(plc_frame, text="Deleting NodeId:").grid(row=3, column=0, sticky=tk.W)
        ttk.Entry(plc_frame, textvariable=self.plc_deleting_node_var).grid(
            row=3, column=1, columnspan=3, sticky=tk.EW, padx=5, pady=5
        )

        ttk.Label(plc_frame, text="用户名:").grid(row=4, column=0, sticky=tk.W)
        ttk.Entry(plc_frame, textvariable=self.plc_username_var).grid(
            row=4, column=1, sticky=tk.EW, padx=5, pady=5
        )
        ttk.Label(plc_frame, text="密码:").grid(row=4, column=2, sticky=tk.W)
        ttk.Entry(plc_frame, textvariable=self.plc_password_var, show="*").grid(
            row=4, column=3, sticky=tk.EW, padx=5, pady=5
        )

        ttk.Label(plc_frame, text="Security String:").grid(row=5, column=0, sticky=tk.W)
        ttk.Entry(plc_frame, textvariable=self.plc_security_var).grid(
            row=5, column=1, columnspan=3, sticky=tk.EW, padx=5, pady=5
        )

        # 底部操作栏
        act_frame = ttk.Frame(main_container)
        act_frame.pack(fill=tk.X, pady=(10, 0))

        # 倒计时取消按钮
        self.btn_cancel_timer = ttk.Button(act_frame, text="取消倒计时", command=self.cancel_auto_start,
                                           state="disabled")
        self.btn_cancel_timer.pack(side=tk.TOP, pady=(0, 5))

        btn_container = ttk.Frame(act_frame)
        btn_container.pack(fill=tk.X)

        ttk.Button(btn_container, text="⚡ 立即执行一次", command=self.on_run_now).pack(side=tk.LEFT, padx=(0, 10))
        self.btn_start = ttk.Button(btn_container, text="▶ 启动监控", style="Accent.TButton",
                                    command=self.on_start_click)
        self.btn_start.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(btn_container, text="⏹ 停止", command=self.on_stop_click).pack(side=tk.LEFT, padx=(10, 0))

        # 状态栏
        self.status_label = ttk.Label(main_container, textvariable=self.status_var,
                                      anchor=tk.CENTER, foreground="grey", font=("Helvetica", 9))
        self.status_label.pack(side=tk.BOTTOM, fill=tk.X, pady=10)

    # ====================== UI 回调 ======================
    def update_alpha(self, val):
        self.attributes("-alpha", float(val))

    def toggle_theme(self):
        if THEME_AVAILABLE:
            current = sv_ttk.get_theme()
            new = "light" if current == "dark" else "dark"
            sv_ttk.set_theme(new)
            self.theme_var.set(new)

    def choose_folder(self):
        self.cancel_auto_start()
        d = filedialog.askdirectory()
        if d:
            self.folder_var.set(d)

    def choose_log(self):
        self.cancel_auto_start()
        f = filedialog.asksaveasfilename(defaultextension=".txt",
                                         filetypes=[("Text Files", "*.txt")])
        if f:
            self.log_var.set(f)

    def open_log(self):
        path = self.log_var.get()
        if path and os.path.exists(path):
            os.startfile(path)
        else:
            messagebox.showinfo("提示", "日志文件尚不存在")

    def get_current_config(self):
        return {
            "days": [var.get() for var in self.day_vars],
            "hour": self.hour_var.get(),
            "minute": self.minute_var.get(),
            "disk": self.disk_var.get(),
            "threshold": self.threshold_var.get(),
            "folder": self.folder_var.get(),
            "delete_n": self.delete_var.get(),
            "log_path": self.log_var.get(),
            "plc_enabled": bool(self.plc_enabled_var.get()),
            "plc_endpoint": self.plc_endpoint_var.get().strip(),
            "plc_username": self.plc_username_var.get().strip(),
            "plc_password": self.plc_password_var.get(),
            "plc_security": self.plc_security_var.get().strip(),
            "plc_heartbeat_node": self.plc_heartbeat_node_var.get().strip(),
            "plc_deleting_node": self.plc_deleting_node_var.get().strip(),
        }

    def update_status_safe(self, text):
        self.after(0, lambda: self.status_var.set(text))

    def refresh_plc_connection(self, show_status: bool = True):
        self.plc_client.reconfigure(self.get_current_config())
        success, msg = self.plc_client.start()
        if show_status and msg:
            self.update_status_safe(msg)
        return success, msg

    def on_plc_reconnect(self):
        self.refresh_plc_connection(show_status=True)

    def on_run_now(self):
        self.cancel_auto_start()
        self.refresh_plc_connection(show_status=False)

        cfg = self.get_current_config()
        if not os.path.isdir(cfg["folder"]):
            messagebox.showerror("错误", "清理目标文件夹无效")
            return

        password = simpledialog.askstring("身份验证", "请输入管理员密码以继续:", show='*', parent=self)
        if password is None:
            return
        if password != ADMIN_PASSWORD:
            messagebox.showerror("错误", "密码错误，拒绝访问。")
            return

        if not messagebox.askyesno("⚠ 最终确认",
                                   f"身份验证通过。\n\n您即将手动运行清理。\n这将【永久删除】 {cfg['delete_n']} 个最旧文件！\n\n确定要继续吗？"):
            return

        self.status_var.set("正在执行检查...")
        threading.Thread(target=self.core.run_check_job, args=(cfg, self.update_status_safe, True), daemon=True).start()

    def on_start_click(self):
        self.cancel_auto_start(silent=True)
        self.refresh_plc_connection(show_status=False)
        cfg = self.get_current_config()
        success, msg = self.core.start_scheduler(cfg, self.update_status_safe)
        self.status_var.set(msg)
        if success:
            self.btn_start.state(["disabled"])

    def on_stop_click(self):
        self.cancel_auto_start()
        msg = self.core.stop_scheduler()
        self.status_var.set(msg)
        self.btn_start.state(["!disabled"])

    def on_closing(self):
        self.plc_client.stop()
        self.core.stop_scheduler()
        self.save_config()
        self.destroy()

    def get_drive_letters(self):
        drives = []
        try:
            import string
            from ctypes import windll
            bitmask = windll.kernel32.GetLogicalDrives()
            for letter in string.ascii_uppercase:
                if bitmask & 1:
                    drives.append(f"{letter}:/")
                bitmask >>= 1
        except:
            drives = ["C:/"]
        return drives

    # ====================== 自动倒计时功能 ======================
    def start_auto_start_countdown(self):
        self.btn_cancel_timer.state(["!disabled"])
        self.update_status_safe(f"⏳ 程序将在 {self.auto_start_seconds} 秒后自动启动监控")
        self.auto_start_job_id = self.after(1000, self.update_auto_start_status)

    def update_auto_start_status(self):
        self.auto_start_seconds -= 1
        if self.auto_start_seconds <= 0:
            self.on_start_click()
        else:
            self.update_status_safe(f"⏳ 程序将在 {self.auto_start_seconds} 秒后自动启动监控")
            self.auto_start_job_id = self.after(1000, self.update_auto_start_status)

    def cancel_auto_start(self, silent=False):
        """取消自动启动的倒计时"""
        if self.auto_start_job_id:
            self.after_cancel(self.auto_start_job_id)
            self.auto_start_job_id = None
            if not silent:
                self.update_status_safe("自动启动已取消，等待手动操作")
            self.btn_cancel_timer.state(["disabled"])
            self.btn_cancel_timer.pack_forget()


# ==========================================
# 启动程序
# ==========================================
if __name__ == "__main__":
    # -----------------------------------------------------------------
    # 【核心修改】告诉 Windows 这是一个独立应用，确保任务栏图标生效
    # -----------------------------------------------------------------
    try:
        import ctypes
        myappid = 'tesla.diskcleaner.pro.v1'  # 任意唯一的字符串ID
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except Exception as e:
        print(f"设置 AppUserModelID 失败: {e}")

    app = CleanerApp()
    app.mainloop()
