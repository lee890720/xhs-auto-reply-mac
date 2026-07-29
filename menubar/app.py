#!/usr/bin/env python3
"""
菜单栏 App。

它只是守护进程的遥控器：进程本身由 launchd 托管（开机自启、崩溃自愈），
这里通过 HTTP 控制「监控引擎」的启停并显示状态。

这样即使关掉菜单栏 App，自动回复也照常运行；反过来菜单栏 App 崩了
也不会带走守护进程。
"""
import fcntl
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import rumps

APP_NAME = "XHSAutoReply"
DATA_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
RUNTIME_FILE = DATA_DIR / "runtime.json"
LOG_FILE = DATA_DIR / "logs" / "xhsd.log"
LOCK_FILE = DATA_DIR / "menubar.lock"
LAUNCH_LABEL = "com.xhs.autoreply.daemon"

REQUEST_TIMEOUT = 4

# 必须保持全局引用，否则文件描述符被回收会释放锁
_lock_fd = None


def acquire_lock() -> bool:
    """
    单实例检查。

    用文件锁而不是 pgrep 匹配命令行——后者会被任何命令行里出现过
    本脚本路径的无关进程（终端、编辑器）误伤，导致 App 点了没反应。
    """
    global _lock_fd
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _lock_fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    os.ftruncate(_lock_fd, 0)
    os.write(_lock_fd, str(os.getpid()).encode())
    return True


def notify_already_running() -> None:
    """已经在运行时给用户一个可见的反馈，而不是静默退出"""
    subprocess.run(
        ["osascript", "-e",
         'display notification "图标就在屏幕右上角的菜单栏里" '
         'with title "小红书自动回复" subtitle "已经在运行了"'],
        check=False, capture_output=True,
    )


def read_base_url() -> str | None:
    """守护进程启动时会把实际端口写进 runtime.json"""
    try:
        return json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))["url"]
    except Exception:
        return None


def http(method: str, path: str, timeout: int = REQUEST_TIMEOUT) -> dict | None:
    base = read_base_url()
    if not base:
        return None
    req = urllib.request.Request(base + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None


class MenuBarApp(rumps.App):
    def __init__(self):
        # name 必须是 ASCII —— 用中文时 rumps 会静默地不创建菜单栏项，
        # 进程正常运行但图标永远不出现。中文只放在 title 里。
        super().__init__("XHSAutoReply", title="书 ○", quit_button=None)
        self.status = None

        self.item_state = rumps.MenuItem("守护进程未运行")
        self.item_state.set_callback(None)          # 纯展示

        self.item_toggle = rumps.MenuItem("开始监控", callback=self.toggle_engine)
        self.item_console = rumps.MenuItem("打开控制台", callback=self.open_console)
        self.item_review = rumps.MenuItem("待审核 0 条", callback=self.open_review)
        self.item_login = rumps.MenuItem("扫码登录小红书", callback=self.do_login)

        self.menu = [
            self.item_state,
            None,
            self.item_toggle,
            self.item_console,
            self.item_review,
            None,
            self.item_login,
            None,
            rumps.MenuItem("重启守护进程", callback=self.restart_daemon),
            rumps.MenuItem("查看日志文件", callback=self.open_logs),
            None,
            rumps.MenuItem("退出菜单栏（不影响后台运行）", callback=self.quit_app),
        ]
        self.refresh(None)

    # ==================== 状态刷新 ====================

    @rumps.timer(5)
    def refresh(self, _):
        s = http("GET", "/api/status")
        self.status = s

        if s is None:
            self.title = "书 ✕"
            self.item_state.title = "守护进程未运行"
            self.item_toggle.title = "启动守护进程"
            self.item_review.title = "待审核 —"
            self.item_login.hidden = True
            return

        running = s.get("running")
        pending = s.get("pending_review", 0)
        today = s.get("today_sent", 0)
        limit = s.get("daily_limit", 0)

        if s.get("need_login"):
            mark = "!"
            state_text = "需要扫码登录"
        elif s.get("state") == "error":
            mark = "✕"
            state_text = "运行异常"
        elif s.get("state") == "quiet":
            mark = "◐"
            state_text = "静默时段"
        elif running:
            mark = "●"
            state_text = "监控中"
        else:
            mark = "○"
            state_text = "已停止"

        self.title = f"书 {mark}" + (f" {pending}" if pending else "")
        self.item_state.title = f"{state_text} · 今日 {today}/{limit}"
        self.item_toggle.title = "停止监控" if running else "开始监控"
        self.item_review.title = f"待审核 {pending} 条" if pending else "待审核 0 条"
        self.item_login.hidden = not s.get("need_login")

        if not s.get("llm_configured"):
            self.item_state.title += " · 未配置模型"

    # ==================== 菜单动作 ====================

    def toggle_engine(self, _):
        if self.status is None:
            self.restart_daemon(None)
            return
        action = "stop" if self.status.get("running") else "start"
        result = http("POST", f"/api/engine/{action}", timeout=20)
        if result:
            rumps.notification("小红书自动回复", "", result.get("message", ""))
        self.refresh(None)

    def open_console(self, _):
        base = read_base_url()
        if base:
            webbrowser.open(base)
        else:
            rumps.notification("小红书自动回复", "", "守护进程未运行，正在尝试启动")
            self.restart_daemon(None)

    def open_review(self, _):
        self.open_console(None)

    def do_login(self, _):
        result = http("POST", "/api/login/open", timeout=30)
        rumps.notification(
            "小红书自动回复", "",
            (result or {}).get("message", "无法连接守护进程"),
        )

    def restart_daemon(self, _):
        try:
            subprocess.run(
                ["launchctl", "kickstart", "-k", f"gui/{os_uid()}/{LAUNCH_LABEL}"],
                check=True, capture_output=True, timeout=15,
            )
            rumps.notification("小红书自动回复", "", "守护进程已重启")
        except subprocess.CalledProcessError as e:
            rumps.notification(
                "小红书自动回复", "重启失败",
                (e.stderr or b"").decode("utf-8")[:120] or "请检查是否已运行 install.sh",
            )
        except Exception as e:
            rumps.notification("小红书自动回复", "重启失败", str(e)[:120])

    def open_logs(self, _):
        subprocess.run(["open", "-R", str(LOG_FILE)], check=False)

    def quit_app(self, _):
        rumps.quit_application()


def os_uid() -> int:
    import os
    return os.getuid()


def become_accessory_app() -> None:
    """
    切换成 accessory 模式：只驻留菜单栏，不占 Dock、不抢焦点、不显示应用菜单。

    Info.plist 里的 LSUIElement 管不到这个进程——真正运行的是 venv 里的
    python 二进制，它读的是 Python.framework 自带的 Info.plist，所以要在
    运行时显式设置。
    """
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory
        )
    except Exception:
        pass          # 设置失败只是多占一个 Dock 图标，不影响功能


if __name__ == "__main__":
    if not acquire_lock():
        notify_already_running()
        sys.exit(0)
    become_accessory_app()
    MenuBarApp().run()
