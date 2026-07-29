"""
运行时配置。

与旧版最大的区别：配置存在数据库里，控制台改完立刻生效，不需要改代码或重启。
.env 不再承担配置职责——API Key 也由控制台填写。
"""
import os
from typing import Any

from pydantic import BaseModel, Field

from . import db

DEFAULT_PERSONA = """你是一个小红书博主本人，正在回复评论区的粉丝。

回复要求：
1. 语气亲切自然，像朋友聊天，不要客服腔
2. 可以用 1-2 个 emoji，但不要堆砌
3. 长度控制在 50 字以内，评论区回复要短
4. 不要重复对方说的话，不要说"感谢你的评论"这种套话
5. 对方夸你就自然接住，对方提问就简短回答
6. 绝对不要透露你是 AI 或自动回复

禁止：
- 不做任何收益、效果、疗效承诺
- 不推荐具体股票、基金、药品
- 不涉及政治、医疗诊断、法律建议
- 不输出引号、不加任何前后缀说明，直接给回复正文"""

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


class SafetyConfig(BaseModel):
    sensitive_keywords: list[str] = Field(default_factory=lambda: list(DEFAULT_SENSITIVE))
    on_sensitive: str = "block"         # block 转人工 | fallback 用兜底话术
    fallback_text: str = "看到你的消息啦～这个我想认真回你，稍后单独找你聊🙏"


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
