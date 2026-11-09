"""
测试环境隔离。

数据目录是按 Path.home() 算出来的模块级常量，所以必须在导入 xhsd 之前
把 HOME 指到临时目录，否则测试会写进真实的 ~/Library/Application Support。
"""
import os
import sys
import tempfile
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="xhsd-test-home-")
os.environ["HOME"] = _TMP_HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from xhsd import db  # noqa: E402
from xhsd.paths import DB_PATH  # noqa: E402

assert str(DB_PATH).startswith(_TMP_HOME), "数据库路径没有被隔离到临时目录"


@pytest.fixture(autouse=True)
def fresh_db():
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            p.unlink()
    db.init_db()
    yield
