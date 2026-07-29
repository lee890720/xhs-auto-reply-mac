"""
日志配置。

日志同时写三处：
  - 终端（前台调试时看）
  - 文件（launchd 托管时排障，按天轮转）
  - events 表（控制台直接查库展示，不必解析日志文本）
"""
import sys

from loguru import logger

from . import db
from .paths import LOG_FILE, ensure_dirs

_configured = False


def _db_sink(message) -> None:
    record = message.record
    try:
        db.log_event(
            level=record["level"].name,
            source=record["name"].replace("xhsd.", ""),
            message=record["message"],
        )
    except Exception:
        # 日志写库失败不能影响主流程
        pass


def setup(console_level: str = "INFO") -> None:
    global _configured
    if _configured:
        return

    ensure_dirs()
    logger.remove()

    logger.add(
        sys.stderr,
        level=console_level,
        format="{time:HH:mm:ss} | {level:<7} | {message}",
    )
    logger.add(
        LOG_FILE,
        level="DEBUG",
        rotation="1 day",
        retention="14 days",
        encoding="utf-8",
        enqueue=True,
    )
    logger.add(_db_sink, level="INFO")

    _configured = True
