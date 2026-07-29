"""
运行时路径。

所有持久化数据放在 macOS 标准的 Application Support 目录下，
而不是 /tmp —— 后者会被系统清理，重启后去重记录丢失会导致重复回复。
"""
from pathlib import Path

APP_NAME = "XHSAutoReply"

DATA_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
DB_PATH = DATA_DIR / "data.db"
LOG_DIR = DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "xhsd.log"
# Playwright 持久化用户数据目录，保存小红书登录态
BROWSER_DIR = DATA_DIR / "browser"
SCREENSHOT_DIR = DATA_DIR / "screenshots"
# 守护进程写入，供菜单栏 App 读取实际监听端口
RUNTIME_FILE = DATA_DIR / "runtime.json"
# 单实例锁：两个守护进程同时操作 BROWSER_DIR 会损坏浏览器配置
LOCK_FILE = DATA_DIR / "xhsd.lock"

# 项目内静态资源
PROJECT_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = PROJECT_DIR / "web"


def ensure_dirs() -> None:
    """创建所有运行时目录"""
    for d in (DATA_DIR, LOG_DIR, BROWSER_DIR, SCREENSHOT_DIR):
        d.mkdir(parents=True, exist_ok=True)
