"""
服务商预设。

这些服务商都提供 OpenAI 兼容的 /chat/completions 接口，
所以一套客户端就能覆盖，切换只是换 base_url + key + model。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    """
    只预设 base_url —— 端点是固定的、可验证的，填错立刻报错。

    刻意不预设模型名：各家模型列表更新频繁，且能用哪些取决于账号开通情况。
    硬编码一份清单迟早过时，反而会诱导用户填一个并不存在的模型。
    模型名一律由用户从服务商控制台查到后自行填写。
    """
    key: str
    label: str
    base_url: str                       # 空字符串表示必须由用户填写
    console_url: str = ""               # 控制台链接，用于申请 Key 和查可用模型
    note: str = ""


PROVIDERS: dict[str, Provider] = {
    "deepseek": Provider(
        key="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        console_url="https://platform.deepseek.com",
        note="性价比高，中文口语化表达自然，适合评论区短回复。模型名见控制台的接口文档。",
    ),
    "dashscope": Provider(
        key="dashscope",
        label="阿里百炼（通义千问）",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        console_url="https://bailian.console.aliyun.com/",
        note="国内延迟低。使用 OpenAI 兼容模式端点，模型名见控制台的模型广场。",
    ),
    "tokenhub": Provider(
        key="tokenhub",
        label="TokenHub",
        base_url="",
        note="请填写 TokenHub 控制台给出的 Base URL 与模型名（需为 OpenAI 兼容端点）。",
    ),
    "custom": Provider(
        key="custom",
        label="自定义 OpenAI 兼容端点",
        base_url="",
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
            "console_url": p.console_url,
            "note": p.note,
            "needs_base_url": not p.base_url,
        }
        for p in PROVIDERS.values()
    ]
