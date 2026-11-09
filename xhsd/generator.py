"""
回复生成。

职责：敏感词判定 → 组装 prompt → 调模型 → 清洗输出。
人设完全在本地（settings.persona），不再锁死在云端 Bot 里，改完立即生效。
"""
import re

from loguru import logger

from .llm import ChatClient, LLMError
from .settings_store import AppConfig
from .x_api import X_MAX_WEIGHT, truncate_weighted


class SensitiveHit(Exception):
    """命中敏感词，携带命中的关键词"""

    def __init__(self, keyword: str):
        self.keyword = keyword
        super().__init__(f"命中敏感词: {keyword}")


class SkipReply(Exception):
    """
    模型判断这条评论不该回复（挑衅、辱骂、立场争论、纯表情等）。

    敏感词表列不完这些情况，而强迫模型对一句辱骂编出得体回复，
    结果只会是「哈哈，欢迎你呀」这种自取其辱的答复。不如让它有权弃答。
    """


# 模型弃答时输出的标记。宽松匹配：模型可能带引号、方括号或多余空白。
_SKIP_PATTERN = re.compile(r"^\W*skip\W*$", re.IGNORECASE)


def check_sensitive(content: str, keywords: list[str]) -> str | None:
    for kw in keywords:
        if kw and kw in content:
            return kw
    return None


def check_forbidden(reply: str, forbidden: list[str]) -> str:
    """检查生成的回复里有没有禁用词，返回命中的词，没有则返回空串"""
    for word in forbidden:
        if word and word in reply:
            return word
    return ""


def build_user_prompt(
    author: str,
    content: str,
    note_title: str,
    msg_type: str,
    note_body: str = "",
    note_tags: str = "",
    max_note_chars: int = 600,
) -> str:
    """
    把一条评论组装成给模型的输入。

    笔记内容是回复质量的关键：评论往往是接着笔记的话说的（笔记在约跑步，
    评论「约起来，我可以」），没有笔记上下文时模型只能泛泛而谈甚至跑题。
    这里传笔记原文而不是提取关键词 —— 小红书正文本身就短，再提炼只会丢信息。
    """
    label = "评论" if msg_type == "comment" else "私信"
    lines = []

    note_parts = []
    if note_title:
        note_parts.append(f"标题：{note_title}")
    if note_body:
        body = note_body[:max_note_chars]
        if len(note_body) > max_note_chars:
            body += "…"
        note_parts.append(f"正文：{body}")
    if note_tags:
        note_parts.append(f"话题标签：{note_tags}")

    if note_parts:
        lines.append("【你发布的笔记】")
        lines.extend(note_parts)
        lines.append("")
        lines.append(f"【一位昵称为「{author}」的用户在这篇笔记下的{label}】")
        lines.append(content)
        lines.append("")
        lines.append(
            "请结合笔记内容回复。评论多半是接着笔记的话题说的，"
            "要回到具体这件事上，不要说放之四海皆可的泛泛之词。"
        )
    else:
        # 拿不到笔记内容时如实说明，避免模型脑补出一个不存在的话题
        lines.append(f"一位昵称为「{author}」的用户发来{label}：{content}")
        lines.append("")
        lines.append(
            "（这条评论所属的笔记内容暂时取不到。）"
            "请只针对评论本身简短回应，宁可含糊也不要猜测笔记讲了什么。"
        )

    # 昵称只用于让模型知道是谁在说话，不能出现在回复里
    lines.append("上面的昵称只是告诉你这条是谁发的，回复里不要提到这个昵称。")
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


# 推文开头那串 @。回复别人时 X 会把对话里的人自动加在正文最前面，
# 这些不是对方要说的话，喂给模型只会干扰它判断。
_LEADING_MENTIONS = re.compile(r"^(?:\s*@\w{1,15})+\s*")


def strip_leading_mentions(text: str) -> str:
    return _LEADING_MENTIONS.sub("", text or "").strip()


def build_x_prompt(
    content: str,
    context_text: str = "",
    context_author: str = "",
    context_is_mine: bool = False,
    relation: str = "",
    max_context_chars: int = 600,
) -> str:
    """
    把一条 @ 我的推文组装成给模型的输入。

    对应小红书的「笔记 + 评论」：对方回复或引用的那条推文就是上下文，
    很多时候正是你自己发的那条。用户名不放进 prompt —— 模型知道了就容易
    在回复里叫出来，而 X 会自动把回复挂到对方推文下面，根本不需要称呼。
    """
    lines = []
    if context_text:
        who = "你自己" if context_is_mine else (f"@{context_author}" if context_author else "另一位用户")
        ctx = context_text[:max_context_chars]
        if len(context_text) > max_context_chars:
            ctx += "…"
        if relation == "quoted":
            lines.append(f"【对方引用的推文，作者是{who}】")
        else:
            lines.append(f"【对方回复的推文，作者是{who}】")
        lines.append(ctx)
        lines.append("")
        lines.append("【对方的推文】")
        lines.append(content)
        lines.append("")
        lines.append(
            "请结合上面那条推文回复。对方多半是接着那条推文说的，"
            "要回到具体这件事上，不要说放之四海皆可的泛泛之词。"
        )
    else:
        lines.append("【一条 @ 了你的推文】")
        lines.append(content)
        lines.append("")
        if relation:
            lines.append(
                "（它所回复的那条推文取不到，可能已被删除。）"
                "请只针对这条推文本身简短回应，宁可含糊也不要猜测上下文。"
            )
    lines.append("回复会直接挂在对方这条推文下面，不需要 @ 任何人。")
    lines.append("请直接给出你的回复正文，不要加引号，不要加任何解释。")
    return "\n".join(lines)


def clean_x_output(text: str) -> str:
    """在通用清洗之外去掉开头的 @，并按 X 的加权长度截断"""
    text = clean_output(text, max_chars=1000)
    text = strip_leading_mentions(text)
    return truncate_weighted(text, X_MAX_WEIGHT)


class ReplyGenerator:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.client = ChatClient(cfg.llm)

    async def generate(
        self,
        author: str,
        content: str,
        note_title: str = "",
        msg_type: str = "comment",
        note_body: str = "",
        note_tags: str = "",
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

        prompt = build_user_prompt(
            author, content, note_title, msg_type,
            note_body=note_body,
            note_tags=note_tags,
            max_note_chars=self.cfg.reply.note_context_chars,
        )
        return await self._complete_with_rules(
            content, prompt, self.cfg.persona.system_prompt, clean_output
        )

    async def generate_x(
        self,
        content: str,
        context_text: str = "",
        context_author: str = "",
        context_is_mine: bool = False,
        relation: str = "",
    ) -> str:
        """
        为一条 @ 我的推文生成回复。敏感词、弃答、禁用词规则与小红书共用，
        人设用 X 自己的那份。

        Raises:
            SensitiveHit / SkipReply / LLMError，含义同 generate
        """
        hit = check_sensitive(content, self.cfg.safety.sensitive_keywords)
        if hit:
            if self.cfg.safety.on_sensitive == "fallback":
                logger.warning(f"命中敏感词「{hit}」，使用兜底话术")
                return truncate_weighted(self.cfg.safety.fallback_text, X_MAX_WEIGHT)
            raise SensitiveHit(hit)

        prompt = build_x_prompt(
            content,
            context_text=context_text,
            context_author=context_author,
            context_is_mine=context_is_mine,
            relation=relation,
            max_context_chars=self.cfg.reply.note_context_chars,
        )
        return await self._complete_with_rules(
            content, prompt, self.cfg.x.system_prompt, clean_x_output
        )

    async def _complete_with_rules(self, content, prompt, system_prompt, clean) -> str:
        """调模型，然后执行弃答判定和禁用词重试。两个平台共用这一段"""
        # 禁用词命中就重来一次。人设里的禁令是概率约束，温度 0.9 下压不住——
        # 尤其笔记正文自己写着「拉群约球」时，模型很容易把原话搬进回复。
        hit = ""
        for attempt in range(2):
            user_prompt = prompt
            if hit:
                user_prompt += (
                    f"\n\n注意：刚才那版回复里出现了「{hit}」，这是不允许的。"
                    f"重写一条，不要提到{hit}，需要进一步沟通就只说私信。"
                )
            raw = await self.client.complete(system_prompt, user_prompt)
            cleaned = clean(raw)

            if _SKIP_PATTERN.match(cleaned):
                raise SkipReply(f"模型判断这条不宜回复: {content[:40]}")
            if not cleaned:
                raise LLMError("清洗后回复为空")

            hit = check_forbidden(cleaned, self.cfg.safety.forbidden_in_reply)
            if not hit:
                return cleaned
            logger.warning(f"回复含禁用词「{hit}」，重新生成: {cleaned}")

        raise SkipReply(f"重写后仍含禁用词「{hit}」，转人工")
