"""
守护进程入口。

    python -m xhsd              # 默认 127.0.0.1:8787
    python -m xhsd --port 9000
    python -m xhsd --headless   # 无头浏览器（登录态已就绪后可用）
"""
import argparse
import fcntl
import json
import os
import socket
import sys

import uvicorn
from loguru import logger

from . import api, db
from .logging_setup import setup as setup_logging
from .paths import LOCK_FILE, RUNTIME_FILE, ensure_dirs

DEFAULT_PORT = 8787

# 必须保持全局引用：文件描述符被回收会连带释放锁
_lock_fd = None


def acquire_single_instance_lock() -> bool:
    """
    保证同时只有一个守护进程。

    两个实例会同时打开同一个 Playwright 用户数据目录，轻则登录态错乱，
    重则浏览器配置损坏需要重新扫码。
    """
    global _lock_fd
    _lock_fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    os.ftruncate(_lock_fd, 0)
    os.write(_lock_fd, str(os.getpid()).encode())
    return True


def read_lock_pid() -> str:
    try:
        return LOCK_FILE.read_text(encoding="utf-8").strip() or "未知"
    except Exception:
        return "未知"


def find_port(start: int, attempts: int = 20) -> int | None:
    for port in range(start, start + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return None


def write_runtime(port: int) -> None:
    """把实际端口写到磁盘，菜单栏 App 据此连接"""
    RUNTIME_FILE.write_text(
        json.dumps({"port": port, "url": f"http://127.0.0.1:{port}"}, ensure_ascii=False),
        encoding="utf-8",
    )


def read_runtime() -> dict:
    try:
        return json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> None:
    parser = argparse.ArgumentParser(description="小红书自动回复守护进程")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--headless", action="store_true", help="无头模式运行浏览器")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    ensure_dirs()
    setup_logging(args.log_level)

    if not acquire_single_instance_lock():
        logger.error(
            f"守护进程已在运行（PID {read_lock_pid()}），本次启动退出。"
            f"如需重启请执行: launchctl kickstart -k gui/{os.getuid()}/com.xhs.autoreply.daemon"
        )
        sys.exit(0)

    db.init_db()

    port = find_port(args.port)
    if port is None:
        logger.error(f"端口 {args.port}-{args.port + 19} 全部被占用")
        sys.exit(1)
    if port != args.port:
        logger.warning(f"端口 {args.port} 被占用，改用 {port}")

    api.engine.browser.headless = args.headless
    write_runtime(port)

    logger.info(f"控制台地址: http://127.0.0.1:{port}")
    uvicorn.run(api.app, host="127.0.0.1", port=port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
