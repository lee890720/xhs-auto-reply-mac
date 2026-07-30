"""
运行时配置。

与旧版最大的区别：配置存在数据库里，控制台改完立刻生效，不需要改代码或重启。
.env 不再承担配置职责——API Key 也由控制台填写。
"""
import os
from typing import Any

from pydantic import BaseModel, Field

from . import db

# 措辞刻意具体到「不准以哈哈开头」这种程度。写「语气自然」这类抽象要求时，
# 模型给出的是它以为的自然 —— 实测十条里八条以「哈哈」开头、条条带 emoji。
# 负向约束必须点名到具体词，正向要求必须给字数。
DEFAULT_PERSONA = """你是这个小红书账号的博主本人，在回复自己笔记下面的评论。

怎么写：
- 短。多数回复 5～20 字，一句话说完就停。评论区没人写长句子。
- 平实。像随手回一句，不是在服务客户，也不需要热络。
- 就事论事，回到笔记里那件具体的事情上。
- 答不上来就说不知道，不用硬凑。

明确不要：
- 不要用「哈哈」「哈哈哈」开头，这是最容易暴露自动回复的习惯。
- 不要每条都加 emoji。十条里最多一两条带，且只带一个。
- 不要说「谢谢你的评论」「感谢支持」「说得对」这类空话。
- 不要先复述对方的话再回应。
- 不要每句都用「呀」「啦」「哦」「呢」结尾卖乖。
- 不要用「～」这个波浪号，这是自动回复最明显的特征之一。
- 回复里不要出现「群」这个字，不说「拉你进群」「进群聊」「群里说」。
  笔记正文里写了要拉群也不要搬过来 —— 评论区只说私信，群的事私信里再讲。
- 不要在回复里叫对方的昵称。真人回评论不会带称呼，而且小红书上大量
  用户就叫「momo」「小红薯123456」这种默认名，叫出来反而更假。
- 昵称也不能影响你的语气。昵称叫「临终关怀」不代表对方需要安慰，
  只看评论本身说了什么。
- 不要自称 AI，也不要暗示这是自动回复。
- 你只能看到文字。评论里出现 [图片][视频] 这类占位符时，说明对方发的是
  你看不到的内容，绝对不要假装看过并评价它。

分情况处理：
- 提问 → 直接给答案，不寒暄。
- 认真讨论，哪怕语气冲或观点跟你不同 → 就事论事回一句，不辩论不站队。
- 夸奖 → 一句话接住，不过度谦虚也不重复道谢。
- 调侃、开玩笑、问你是不是 AI → 轻松接一句就行。
- 想参加、想加入、想约、问怎么联系报名 → 请对方私信你。
  这是真实的下一步，评论区里敲定不了细节。但只在对方确实有意向时才说，
  不要变成口头禅。
  **一律只说私信，绝对不要在回复里提群。** 不写「拉你进群」「进群聊」
  「群里说」这类话。即使笔记内容里写了要拉群，评论区也不提 ——
  群的事等对方私信过来再讲。
- 人身攻击、辱骂、脏话 → 只输出 [SKIP]，不要试图回复。
- 用贬称或侮辱性标签指代你、某个国家、某个群体的，哪怕包装成反问句
  → 只输出 [SKIP]。顺着这种话往下接，等于默认了对方的说法。
  注意这跟普通调侃不一样，普通调侃照常回。
- 大段立场宣示、明显来引战的 → 只输出 [SKIP]。哪怕对方的立场听起来跟你
  一致，也不要附和 —— 替账号站队是有风险的。
- 怀疑或抗议你是机器人的，比如「转人工」「你是不是机器人」「别用AI回我」
  → 只输出 [SKIP]。对方已经在质疑了，再热情寒暄等于当场自证。
- 评论里只有 [图片][视频] 这类你看不到的内容 → 只输出 [SKIP]。
- 纯表情、纯 @ 别人、纯数字、短到看不出意思的 → 只输出 [SKIP]。

不知道的事情不要编。你手上只有笔记内容和这条评论，别的一概不知道：

- 不要承诺笔记里没写过的事。没提过发资料就不要说私发资料，没提过开直播
  就不要说到时候直播讲。对方当真来找你，你交不出来就是失信。
- 不要编具体的店名、场馆名、地址、价格、营业时间、门票、进出规定。
  这些你并不知道，编出来别人照着去会白跑一趟。
- 不要替本人编个人情况和日程，比如水平高低、职业、住哪、用什么设备、
  今天有没有安排。笔记里没写的就不要具体化。
- 不确定的时候别给理由。说「不太清楚」就够了，不要补上「我不是院里的」
  「我平时都没被拦过」这种解释 —— 用来解释的那半句往往就是编的。
- 遇到这类问题，要么说不清楚/还没定，要么让对方私信聊，都比编一个好。

内容红线：
- 不做任何收益、效果、疗效承诺
- 不推荐具体股票、基金、药品
- 不涉及政治立场、医疗诊断、法律建议

直接输出回复正文，不要加引号，不要加任何前后缀或解释。"""

DEFAULT_SENSITIVE = [
    "投诉", "律师", "法院", "报警", "举报", "退款", "维权",
    "生病", "医生", "吃药", "抑郁", "自杀", "诈骗",
]


class LLMConfig(BaseModel):
    provider: str = "deepseek"          # 见 llm/registry.py
    api_key: str = ""
    base_url: str = ""                  # 留空则用 provider 预设端点
    model: str = ""                     # 留空则用 provider 预设模型
    temperature: float = 0.9
    max_tokens: int = 300
    timeout: int = 60


class ReplyConfig(BaseModel):
    mode: str = "review"                # review 待审 | auto 全自动
    poll_interval: int = 120            # 采集轮询间隔（秒）
    daily_limit: int = 50               # 每日发送上限
    max_per_poll: int = 5               # 单轮最多采集条数
    min_delay: float = 8.0              # 发送间隔下限（秒）
    max_delay: float = 30.0             # 发送间隔上限（秒）
    quiet_enabled: bool = True          # 夜间静默
    quiet_start: str = "23:00"
    quiet_end: str = "08:00"
    note_context_chars: int = 600        # 笔记正文进 prompt 的截断长度
    require_note_context: bool = False   # 取不到笔记内容时转人工，不自动发送


# 绝不允许出现在回复里的词。人设里写了禁令也只是概率约束——温度 0.9 下
# 同一条评论重跑几次，总有一次会漏出来。硬规则交给代码兜底。
# 用整词而不是单个「群」字，少误伤正常用词——「群体」「一群人」都不会命中。
# 「群里」仍会匹配到「人群里」这种组合，但评论回复里极少这么说，
# 命中的代价只是多生成一次，比漏掉「群里聊」这类说法划算。
DEFAULT_FORBIDDEN = ["拉群", "进群", "建群", "加群", "群里", "群聊"]


class SafetyConfig(BaseModel):
    sensitive_keywords: list[str] = Field(default_factory=lambda: list(DEFAULT_SENSITIVE))
    on_sensitive: str = "block"         # block 转人工 | fallback 用兜底话术
    fallback_text: str = "看到你的消息啦～这个我想认真回你，稍后单独找你聊🙏"
    # 命中则重新生成一次，再命中就转人工
    forbidden_in_reply: list[str] = Field(default_factory=lambda: list(DEFAULT_FORBIDDEN))


class PersonaConfig(BaseModel):
    system_prompt: str = DEFAULT_PERSONA


class AppConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    reply: ReplyConfig = Field(default_factory=ReplyConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    persona: PersonaConfig = Field(default_factory=PersonaConfig)


_SECTIONS = {
    "llm": LLMConfig,
    "reply": ReplyConfig,
    "safety": SafetyConfig,
    "persona": PersonaConfig,
}


def load() -> AppConfig:
    """从数据库读取配置，缺失的字段用默认值补齐"""
    data: dict[str, Any] = {}
    for name, model_cls in _SECTIONS.items():
        stored = db.get_setting(name, {}) or {}
        try:
            data[name] = model_cls(**stored)
        except Exception:
            # 存储的配置结构与当前版本不兼容时退回默认值，避免守护进程起不来
            data[name] = model_cls()
    cfg = AppConfig(**data)

    # 环境变量兜底：首次启动时可用 XHS_API_KEY 预置，省去手动填写
    if not cfg.llm.api_key:
        cfg.llm.api_key = os.environ.get("XHS_API_KEY", "")

    return cfg


def save_section(name: str, values: dict[str, Any]) -> BaseModel:
    """保存一个配置分组，返回校验后的模型"""
    if name not in _SECTIONS:
        raise ValueError(f"未知配置分组: {name}")
    model_cls = _SECTIONS[name]
    current = db.get_setting(name, {}) or {}
    merged = {**current, **values}
    validated = model_cls(**merged)          # 校验失败会抛出，不写库
    db.set_setting(name, validated.model_dump())
    return validated


def redact(cfg: AppConfig) -> dict[str, Any]:
    """导出给前端的配置，API Key 脱敏"""
    data = cfg.model_dump()
    key = data["llm"]["api_key"]
    data["llm"]["api_key"] = f"{key[:6]}······{key[-4:]}" if len(key) > 12 else ("已设置" if key else "")
    data["llm"]["has_key"] = bool(key)
    return data
