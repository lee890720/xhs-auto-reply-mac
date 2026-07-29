"""
回复生成。

职责：敏感词判定 → 组装 prompt → 调模型 → 清洗输出。
人设完全在本地（settings.persona），不再锁死在云端 Bot 里，改完立即生效。
"""
import re

from loguru import logger

from .llm import ChatClient, LLMError
from .settings_store import AppConfig


class SensitiveHit(Exception):
    """命中敏感词，携带命中的关键词"""

    def __init__(self, keyword: str):
        self.keyword = keyword
        super().__init__(f"命中敏感词: {keyword}")


def check_sensitive(content: str, keywords: list[str]) -> str | None:
    for kw in keywords:
        if kw and kw in content:
            return kw
    return None


def build_user_prompt(author: str, content: str, note_title: str, msg_type: str) -> str:
    """把一条评论组装成给模型的输入"""
    label = "评论" if msg_type == "comment" else "私信"
    lines = []
    if note_title:
        lines.append(f"你的笔记标题：{note_title}")
    lines.append(f"用户「{author}」的{label}：{content}")
    lines.append("")
    lines.append("请直接给出你的回复正文，不要加引号，不要加任何解释。")
    return "\n".join(lines)


def clean_output(text: str, max_chars: int = 120) -> str:
    """
    清洗模型输出。

    模型常见的不听话行为：包引号、加「回复：」前缀、输出多段解释。
    这里统一收敛成一句可直接发送的正文。
    """
    text = text.strip()

    # 去掉 <think> 推理块（deepseek-reasoner 等模型会带）
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    # 去掉「回复：」「答：」这类前缀
    text = re.sub(r"^(回复|答复|答|Reply)\s*[:：]\s*", "", text)

    # 去掉整体包裹的引号
    for lq, rq in (("「", "」"), ("“", "”"), ('"', '"'), ("'", "'"), ("《", "》")):
        if text.startswith(lq) and text.endswith(rq) and len(text) > 2:
            text = text[1:-1].strip()

    # 只保留第一段，避免模型输出多个候选
    parts = [p.strip() for p in text.split("\n") if p.strip()]
    if parts:
        text = parts[0]

    return text[:max_chars].strip()


class ReplyGenerator:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.client = ChatClient(cfg.llm)

    async def generate(
        self, author: str, content: str, note_title: str = "", msg_type: str = "comment"
    ) -> str:
        """
        生成回复正文。

        Raises:
            SensitiveHit: 命中敏感词且策略为 block
            LLMError:     模型调用失败
        """
        hit = check_sensitive(content, self.cfg.safety.sensitive_keywords)
        if hit:
            if self.cfg.safety.on_sensitive == "fallback":
                logger.warning(f"命中敏感词「{hit}」，使用兜底话术")
                return self.cfg.safety.fallback_text
            raise SensitiveHit(hit)

        raw = await self.client.complete(
            self.cfg.persona.system_prompt,
            build_user_prompt(author, content, note_title, msg_type),
        )
        cleaned = clean_output(raw)
        if not cleaned:
            raise LLMError("清洗后回复为空")
        return cleaned
