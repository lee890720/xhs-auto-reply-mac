"""
小红书自动回复工作流 - 状态管理
记录已处理消息，避免重复回复。支持文件持久化。
"""
import json
from pathlib import Path
from datetime import datetime, date, timedelta
from collections import Counter
from loguru import logger

from config import settings


class StateManager:
    """消息处理状态管理器"""

    def __init__(self):
        self.processed: dict[str, dict] = {}
        self._state_path = Path(settings.state_file)
        self._load()

    def _load(self):
        """从文件加载状态，兼容旧格式数据"""
        if self._state_path.exists():
            try:
                data = json.loads(self._state_path.read_text(encoding="utf-8"))
                raw = data.get("processed", {})
                # 兼容旧格式：旧数据只有 timestamp 和 reply 字段
                for msg_id, record in raw.items():
                    self.processed[msg_id] = {
                        "timestamp": record.get("timestamp", ""),
                        "reply": record.get("reply", ""),
                        "msg_type": record.get("msg_type", "comment"),
                        "author": record.get("author", ""),
                        "content": record.get("content", ""),
                        "status": record.get("status", "success"),
                        "mode": record.get("mode", "auto"),
                        "note_image_url": record.get("note_image_url", ""),
                    }
                logger.info(f"已加载 {len(self.processed)} 条处理记录")
            except Exception as e:
                logger.warning(f"状态文件加载失败: {e}")
                self.processed = {}

    def _save(self):
        """保存状态到文件"""
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "processed": self.processed,
            "last_updated": datetime.now().isoformat(),
        }
        self._state_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def is_processed(self, msg_id: str) -> bool:
        """检查消息是否已处理"""
        return msg_id in self.processed

    def mark_processed(
        self,
        msg_id: str,
        reply_text: str = "",
        msg_type: str = "",
        author: str = "",
        content: str = "",
        status: str = "success",
        mode: str = "auto",
        note_image_url: str = "",
    ):
        """标记消息已处理，保存完整记录"""
        self.processed[msg_id] = {
            "timestamp": datetime.now().isoformat(),
            "reply": reply_text[:200],
            "msg_type": msg_type,
            "author": author,
            "content": content[:100],
            "status": status,
            "mode": mode,
            "note_image_url": note_image_url,
        }
        self._save()

    def get_today_count(self) -> int:
        """获取今日已回复数量"""
        today = date.today().isoformat()
        return sum(
            1 for record in self.processed.values()
            if record.get("timestamp", "").startswith(today)
        )

    def get_stats(self) -> dict:
        """返回统计数据"""
        today = date.today().isoformat()
        records = list(self.processed.values())

        # 今日回复数
        today_records = [r for r in records if r.get("timestamp", "").startswith(today)]
        today_count = len(today_records)

        # 按状态分组（今日）
        today_status = Counter(r.get("status", "success") for r in today_records)

        # 按类型分组（今日）
        today_type = Counter(r.get("msg_type", "comment") for r in today_records)

        # 历史每日回复数（最近30天）
        daily_counts: dict[str, int] = {}
        daily_success: dict[str, int] = {}
        for i in range(30):
            d = (date.today() - timedelta(days=i)).isoformat()
            day_records = [r for r in records if r.get("timestamp", "").startswith(d)]
            daily_counts[d] = len(day_records)
            daily_success[d] = sum(
                1 for r in day_records if r.get("status") == "success"
            )

        # 按类型分组（全部）
        all_type = Counter(r.get("msg_type", "comment") for r in records)

        # 按状态分组（全部）
        all_status = Counter(r.get("status", "success") for r in records)

        # Top 评论者（按 author 统计，最近30天）
        recent_records = []
        cutoff = (date.today() - timedelta(days=30)).isoformat()
        for r in records:
            ts = r.get("timestamp", "")
            if ts >= cutoff:
                recent_records.append(r)
        author_counts = Counter(r.get("author", "") for r in recent_records)
        top_authors = author_counts.most_common(10)

        return {
            "today_date": today,
            "today_count": today_count,
            "today_status": dict(today_status),
            "today_type": dict(today_type),
            "daily_counts": daily_counts,
            "daily_success": daily_success,
            "all_type": dict(all_type),
            "all_status": dict(all_status),
            "top_authors": top_authors,
            "total": len(records),
            "daily_limit": settings.daily_reply_limit,
            "remaining": max(0, settings.daily_reply_limit - today_count),
        }

    def cleanup_old(self, keep_days: int = 7):
        """清理过期记录（默认保留7天）"""
        cutoff = datetime.now().timestamp() - keep_days * 86400
        to_remove = [
            k for k, v in self.processed.items()
            if datetime.fromisoformat(v["timestamp"]).timestamp() < cutoff
        ]
        for k in to_remove:
            del self.processed[k]
        if to_remove:
            self._save()
            logger.info(f"清理了 {len(to_remove)} 条过期记录")
