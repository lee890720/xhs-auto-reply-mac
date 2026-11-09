"""
运行时配置。

与旧版最大的区别：配置存在数据库里，控制台改完立刻生效，不需要改代码或重启。
.env 不再承担配置职责——API Key 也由控制台填写。
"""
import os
from typing import Any

from pydantic import BaseModel, Field, model_validator

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


# X 的人设跟小红书分开：平台的说话习惯不一样（X 上要按对方的语言回复、
# 不能带 @ 和话题标签），而且 X 的开发者指南要求自动化账号不得冒充真人，
# 所以这里不再写「不要自称 AI」，被问到是不是机器人一律转人工。
DEFAULT_X_PERSONA = """你是这个 X（推特）账号的运营者，在回复 @ 了你、或者回复了你推文的人。

怎么写：
- 用对方的语言回复。对方写中文就用中文，写英文就用英文，其他语言同理。
- 短。多数回复一句话：中文 5～30 字，英文 5～25 个单词。X 上没人写长段落。
- 平实，就事论事，回到对方说的那件事，或者你那条推文里的具体内容上。
- 答不上来就说不知道，不用硬凑。

明确不要：
- 不要在回复里 @ 任何人，也不要带话题标签（#）和链接。回复会自动挂在对方推文下面。
- 不要用「哈哈」「Haha」「LOL」开头。
- 不要每条都加 emoji。十条里最多一两条带，且只带一个。
- 不要说「感谢你的回复」「Thanks for reaching out」「Great question」这类客套空话。
- 不要先复述对方的话再回应。
- 不要用「～」这个波浪号。
- 不要叫对方的名字或用户名。
- 不要声称自己是真人，也不要否认这是自动回复。
- 你只能看到文字。对方发的图片、视频、链接里的内容你看不到，不要假装看过并评价。

分情况处理：
- 提问 → 直接给答案，不寒暄。
- 认真讨论，哪怕语气冲或观点跟你不同 → 就事论事回一句，不辩论不站队。
- 夸奖 → 一句话接住，不过度谦虚也不重复道谢。
- 想合作、想参加、想进一步联系 → 请对方私信（DM）你。只在对方确实有意向时才说。
- 问你是不是机器人、是不是 AI、要求真人回复 → 只输出 [SKIP]，交给真人处理。
- 人身攻击、辱骂、脏话 → 只输出 [SKIP]。
- 用贬称或侮辱性标签指代你、某个国家、某个群体的，哪怕包装成反问句 → 只输出 [SKIP]。
- 大段立场宣示、明显来引战的、政治话题 → 只输出 [SKIP]。
- 推广、带货、求互关、拉你参加抽奖或空投之类的 → 只输出 [SKIP]。
- 只有图片、视频、链接，或只是一串 @、纯表情、短到看不出意思的 → 只输出 [SKIP]。

不知道的事情不要编。你手上只有那条推文和对方这一条，别的一概不知道：
- 不要承诺推文里没提过的事，比如私发资料、开直播、送东西。
- 不要编具体的地点、价格、时间、数据。
- 不要替本人编个人情况和日程。
- 不确定就说不清楚，或者请对方私信，都比编一个好。

内容红线：
- 不做任何收益、效果、疗效承诺
- 不推荐具体股票、基金、加密货币、药品
- 不涉及政治立场、医疗诊断、法律建议

直接输出回复正文，不要加引号，不要加任何前后缀或解释。"""


class XConfig(BaseModel):
    # 开发者后台 → 你的 App → Keys and tokens。App 权限必须是 Read and Write，
    # 而且要在改完权限之后再生成 Access Token，否则令牌仍是只读的。
    api_key: str = ""                   # API Key（Consumer Key）
    api_secret: str = ""                # API Key Secret
    access_token: str = ""
    access_token_secret: str = ""

    # ---------- 回复 ----------
    mode: str = "review"                # review 待审 | auto 全自动
    # X 开发者指南：AI 生成并发布回复，需要事先获得 X 批准。没勾选就不允许全自动
    ai_reply_approved: bool = False
    poll_interval: int = Field(300, ge=60)          # 拉取 @ 的间隔（秒）
    daily_limit: int = Field(50, ge=1)              # 每日回复上限
    per_user_daily_limit: int = Field(3, ge=1)      # 每人每日最多回几条，防止来回刷屏
    max_per_poll: int = Field(20, ge=5, le=100)     # 单次请求拉多少条 @
    max_mentions_in_tweet: int = Field(5, ge=1)     # 一条推文 @ 的人超过这个数就当群发，不回
    min_delay: float = Field(15.0, ge=1)
    max_delay: float = Field(60.0, ge=1)
    quiet_enabled: bool = True
    quiet_start: str = "23:00"
    quiet_end: str = "08:00"

    # ---------- 关注者（只发现，不自动回关）----------
    followers_enabled: bool = True
    followers_poll_interval: int = Field(7200, ge=900)   # 检查新关注者的间隔（秒）
    followers_page_size: int = Field(5, ge=1, le=100)    # 每次取几个，按返回人数计费
    follow_daily_limit: int = Field(50, ge=1, le=400)    # 每日执行回关的上限
    follow_min_delay: float = Field(20.0, ge=1)
    follow_max_delay: float = Field(90.0, ge=1)

    system_prompt: str = DEFAULT_X_PERSONA

    @model_validator(mode="after")
    def _check(self) -> "XConfig":
        if self.mode not in ("review", "auto"):
            raise ValueError("运行模式只能是 review 或 auto")
        if self.mode == "auto" and not self.ai_reply_approved:
            raise ValueError(
                "X 要求 AI 生成的回复事先获得批准。拿到批准并勾选「已获得 X 批准」后才能切到全自动"
            )
        if self.max_delay < self.min_delay:
            raise ValueError("回复间隔上限不能小于下限")
        if self.follow_max_delay < self.follow_min_delay:
            raise ValueError("回关间隔上限不能小于下限")
        return self

    @property
    def has_credentials(self) -> bool:
        return all((self.api_key, self.api_secret, self.access_token, self.access_token_secret))


class AppConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    reply: ReplyConfig = Field(default_factory=ReplyConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    x: XConfig = Field(default_factory=XConfig)


_SECTIONS = {
    "llm": LLMConfig,
    "reply": ReplyConfig,
    "safety": SafetyConfig,
    "persona": PersonaConfig,
    "x": XConfig,
}

# 各分组里的密钥字段。导出给前端时脱敏，前端原样回传掩码时保持原值不变。
SECRET_FIELDS = {
    "llm": ("api_key",),
    "x": ("api_key", "api_secret", "access_token", "access_token_secret"),
}


def load() -> AppConfig:
    """从数据库读取配置，缺失的字段用默认值补齐"""
    data: dict[str, Any] = {}
    for name, model_cls in _SECTIONS.items():
        stored = db.get_setting(name, {}) or {}
        try:
            data[name] = model_cls(**stored)
        except Exception:
            # 存储的配置结构与当前版本不兼容时退回默认值，避免守护进程起不来。
            # X 分组里有密钥，整组退回默认等于把密钥清空，所以先试着只把模式
            # 降回审核再加载一次。
            try:
                data[name] = model_cls(**{**stored, "mode": "review"})
            except Exception:
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


def is_masked(value: Any) -> bool:
    """前端回传的是不是脱敏后的掩码（说明用户没改这一项）"""
    return isinstance(value, str) and ("·" in value or value == "已设置")


def _mask(key: str) -> str:
    return f"{key[:6]}······{key[-4:]}" if len(key) > 12 else ("已设置" if key else "")


def redact(cfg: AppConfig) -> dict[str, Any]:
    """导出给前端的配置，所有密钥脱敏"""
    data = cfg.model_dump()
    data["llm"]["has_key"] = bool(data["llm"]["api_key"])
    data["x"]["has_credentials"] = cfg.x.has_credentials
    for section, fields in SECRET_FIELDS.items():
        for f in fields:
            data[section][f] = _mask(data[section][f])
    return data
