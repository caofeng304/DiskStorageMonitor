import os
import sys
import json
import base64
import shutil
import threading
import tkinter as tk
from tkinter import filedialog, ttk, messagebox, simpledialog
from pathlib import Path
from datetime import datetime
from typing import List, Dict

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

CONFIG_FILE = Path("config.json")


class Assets:
    """资源管理类"""
    # 注意：为了代码简洁，这里保留了你原本的截断 Base64。
    # 如果你本地没有 tesla.ico 文件，请将此处替换回完整的 Base64 字符串，否则回退图标会加载失败。
    TESLA_ICON_B64 = """iVBORw0KGgoAAAANSUhEUgAAADAAAAAwCAYAAABXAvmHAAAABGdB..."""


# ==========================================
# 核心逻辑类
# ==========================================
class DiskCleanerCore:
    def __init__(self):
        self.scheduler = None
        self.task_running = False
        if HAS_SCHEDULER:
            self.scheduler = BackgroundScheduler()
        else:
            self.scheduler = None

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
        self.minsize(720, 720)
        self.center_window(720, 720)

        self.core = DiskCleanerCore()
        self.config = self.load_config()

        self.setup_icon()

        if THEME_AVAILABLE:
            sv_ttk.set_theme(self.config.get("theme", "dark"))
        self.attributes("-alpha", self.config.get("alpha", 0.98))

        self.setup_ui_variables()
        self.build_ui()
        self.load_settings_to_ui()

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
            "alpha": 0.98, "theme": "dark"
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
        }

    def update_status_safe(self, text):
        self.after(0, lambda: self.status_var.set(text))

    def on_run_now(self):
        self.cancel_auto_start()

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