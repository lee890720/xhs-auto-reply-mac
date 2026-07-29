"""
SQLite 数据层。

设计要点：
  - WAL 模式，读写并发安全（Web 请求与调度器同时访问）
  - 每次操作开短连接，避免长连接跨线程的坑
  - messages.fingerprint 唯一索引承担去重职责，替代旧版内存字典
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from .paths import DB_PATH, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint   TEXT    NOT NULL UNIQUE,
    msg_type      TEXT    NOT NULL DEFAULT 'comment',
    author        TEXT    NOT NULL DEFAULT '',
    content       TEXT    NOT NULL DEFAULT '',
    note_title    TEXT    NOT NULL DEFAULT '',
    note_image_url TEXT   NOT NULL DEFAULT '',
    raw_time      TEXT    NOT NULL DEFAULT '',
    collected_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS replies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    draft       TEXT    NOT NULL DEFAULT '',
    final_text  TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL,
    provider    TEXT    NOT NULL DEFAULT '',
    model       TEXT    NOT NULL DEFAULT '',
    error       TEXT    NOT NULL DEFAULT '',
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL,
    reviewed_at TEXT    NOT NULL DEFAULT '',
    sent_at     TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_replies_status  ON replies(status);
CREATE INDEX IF NOT EXISTS idx_replies_created ON replies(created_at);
CREATE INDEX IF NOT EXISTS idx_replies_sent    ON replies(sent_at);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    level      TEXT NOT NULL DEFAULT 'INFO',
    source     TEXT NOT NULL DEFAULT '',
    message    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
"""

# 回复状态机
STATUS_PENDING_REVIEW = "pending_review"  # 待人工审核
STATUS_APPROVED = "approved"              # 已批准，等待发送
STATUS_REJECTED = "rejected"              # 人工拒绝，不发送
STATUS_SENT = "sent"                      # 发送成功
STATUS_FAILED = "failed"                  # 发送失败
STATUS_BLOCKED = "blocked"                # 命中敏感词，不自动发送


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """打开一个短连接，正常退出时提交，异常时回滚"""
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """建表。幂等，每次启动都会调用"""
    with connect() as conn:
        conn.executescript(SCHEMA)


def query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def query_one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple = ()) -> int:
    """执行写操作，返回 lastrowid"""
    with connect() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid or 0


def scalar(sql: str, params: tuple = ()) -> Any:
    with connect() as conn:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None


# ==================== 消息 ====================

def insert_message(
    fingerprint: str,
    msg_type: str,
    author: str,
    content: str,
    note_title: str = "",
    note_image_url: str = "",
    raw_time: str = "",
) -> int | None:
    """
    插入消息。若指纹已存在返回 None（表示此消息之前已处理过）。
    去重由数据库唯一约束保证，进程重启不会失效。
    """
    with connect() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO messages
                   (fingerprint, msg_type, author, content, note_title,
                    note_image_url, raw_time, collected_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (fingerprint, msg_type, author, content, note_title,
                 note_image_url, raw_time, now()),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


# ==================== 回复 ====================

def insert_reply(
    message_id: int,
    draft: str,
    status: str,
    provider: str = "",
    model: str = "",
) -> int:
    return execute(
        """INSERT INTO replies
           (message_id, draft, final_text, status, provider, model, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (message_id, draft, draft, status, provider, model, now()),
    )


def update_reply_status(
    reply_id: int,
    status: str,
    final_text: str | None = None,
    error: str | None = None,
    mark_reviewed: bool = False,
    mark_sent: bool = False,
) -> None:
    fields = ["status = ?"]
    params: list[Any] = [status]

    if final_text is not None:
        fields.append("final_text = ?")
        params.append(final_text)
    if error is not None:
        fields.append("error = ?")
        params.append(error)
    if mark_reviewed:
        fields.append("reviewed_at = ?")
        params.append(now())
    if mark_sent:
        fields.append("sent_at = ?")
        params.append(now())

    params.append(reply_id)
    execute(f"UPDATE replies SET {', '.join(fields)} WHERE id = ?", tuple(params))


def bump_attempts(reply_id: int) -> None:
    execute("UPDATE replies SET attempts = attempts + 1 WHERE id = ?", (reply_id,))


REPLY_JOIN = """
SELECT r.id, r.message_id, r.draft, r.final_text, r.status, r.provider,
       r.model, r.error, r.attempts, r.created_at, r.reviewed_at, r.sent_at,
       m.author, m.content, m.msg_type, m.note_title, m.note_image_url, m.raw_time
FROM replies r
JOIN messages m ON m.id = r.message_id
"""


def list_replies(status: str | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
    if status:
        return query(
            REPLY_JOIN + " WHERE r.status = ? ORDER BY r.id DESC LIMIT ? OFFSET ?",
            (status, limit, offset),
        )
    return query(
        REPLY_JOIN + " ORDER BY r.id DESC LIMIT ? OFFSET ?", (limit, offset)
    )


def get_reply(reply_id: int) -> dict | None:
    return query_one(REPLY_JOIN + " WHERE r.id = ?", (reply_id,))


def next_approved_reply() -> dict | None:
    """取一条待发送的回复（FIFO）"""
    return query_one(
        REPLY_JOIN + " WHERE r.status = ? ORDER BY r.id ASC LIMIT 1",
        (STATUS_APPROVED,),
    )


def count_sent_today() -> int:
    """今日已成功发送数，用于日限额判断"""
    today = datetime.now().date().isoformat()
    return scalar(
        "SELECT COUNT(*) FROM replies WHERE status = ? AND sent_at LIKE ?",
        (STATUS_SENT, f"{today}%"),
    ) or 0


def count_by_status(status: str) -> int:
    return scalar("SELECT COUNT(*) FROM replies WHERE status = ?", (status,)) or 0


# ==================== 配置 ====================

def get_setting(key: str, default: Any = None) -> Any:
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return default


def set_setting(key: str, value: Any) -> None:
    execute(
        """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                          updated_at = excluded.updated_at""",
        (key, json.dumps(value, ensure_ascii=False), now()),
    )


# ==================== 事件 ====================

def log_event(level: str, source: str, message: str) -> None:
    execute(
        "INSERT INTO events (level, source, message, created_at) VALUES (?, ?, ?, ?)",
        (level, source, message[:2000], now()),
    )


def list_events(limit: int = 200) -> list[dict]:
    return query(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
    )


def cleanup(keep_days: int = 90) -> int:
    """清理过期数据，返回删除的消息数"""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat(timespec="seconds")
    with connect() as conn:
        cur = conn.execute("DELETE FROM messages WHERE collected_at < ?", (cutoff,))
        deleted = cur.rowcount
        conn.execute("DELETE FROM events WHERE created_at < ?", (cutoff,))
    return deleted
