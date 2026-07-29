"""
服务商预设。

这些服务商都提供 OpenAI 兼容的 /chat/completions 接口，
所以一套客户端就能覆盖，切换只是换 base_url + key + model。
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    base_url: str                       # 空字符串表示必须由用户填写
    models: list[str] = field(default_factory=list)
    default_model: str = ""
    console_url: str = ""               # 控制台链接，方便用户去申请 Key
    note: str = ""


PROVIDERS: dict[str, Provider] = {
    "deepseek": Provider(
        key="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        models=["deepseek-chat", "deepseek-reasoner"],
        default_model="deepseek-chat",
        console_url="https://platform.deepseek.com/api_keys",
        note="性价比高，中文口语化表达自然，适合评论区短回复。推荐 deepseek-chat。",
    ),
    "dashscope": Provider(
        key="dashscope",
        label="阿里百炼（通义千问）",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        models=["qwen-plus", "qwen-turbo", "qwen-max"],
        default_model="qwen-plus",
        console_url="https://bailian.console.aliyun.com/",
        note="国内延迟低，qwen-turbo 便宜、qwen-plus 质量更好。使用 OpenAI 兼容模式端点。",
    ),
    "tokenhub": Provider(
        key="tokenhub",
        label="TokenHub",
        base_url="",
        models=[],
        default_model="",
        console_url="",
        note="请在下方填写 TokenHub 控制台给出的 Base URL 与模型名（需为 OpenAI 兼容端点）。",
    ),
    "custom": Provider(
        key="custom",
        label="自定义 OpenAI 兼容端点",
        base_url="",
        models=[],
        default_model="",
        note="任何兼容 /chat/completions 的服务都可以，包括本地 Ollama（http://localhost:11434/v1）。",
    ),
}


def get(key: str) -> Provider:
    return PROVIDERS.get(key, PROVIDERS["custom"])


def resolve(provider_key: str, base_url: str, model: str) -> tuple[str, str]:
    """
    合并用户配置与预设，得出实际使用的 base_url 和 model。

    base_url 可以回退到预设（端点是固定的，且填错会立刻报错）；
    模型名不回退——各家可用模型跟账号权限、开通情况有关，静默替换成预设值
    会让人以为在用 A 模型、实际跑的是 B。留空时直接报错要求填写。
    """
    preset = get(provider_key)
    final_base = (base_url or preset.base_url).rstrip("/")
    return final_base, model.strip()


def as_dict() -> list[dict]:
    """供前端渲染服务商选择器"""
    return [
        {
            "key": p.key,
            "label": p.label,
            "base_url": p.base_url,
            "models": p.models,
            "default_model": p.default_model,
            "console_url": p.console_url,
            "note": p.note,
            "needs_base_url": not p.base_url,
        }
        for p in PROVIDERS.values()
    ]
