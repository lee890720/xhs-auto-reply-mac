"""
OpenAI 兼容的 Chat Completions 客户端。

相比旧版直连 Coze 的三步轮询（发起 → 轮询状态 → 拉消息列表），
这里是一次 POST 拿结果，链路短、失败面小、延迟低一个数量级。
"""
import httpx
from loguru import logger

from ..settings_store import LLMConfig
from . import registry


class LLMError(Exception):
    """调用失败，调用方决定是降级还是转人工"""


class ChatClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.base_url, self.model = registry.resolve(
            cfg.provider, cfg.base_url, cfg.model
        )

    def _validate(self) -> None:
        if not self.cfg.api_key:
            raise LLMError("尚未配置 API Key，请在控制台「模型设置」中填写")
        if not self.base_url:
            raise LLMError(f"服务商 {self.cfg.provider} 需要手动填写 Base URL")
        if not self.model:
            raise LLMError("尚未填写模型名，请在控制台「模型 API → 模型」中填写")

    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        """发起一次对话补全，返回纯文本回复"""
        self._validate()

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "stream": False,
        }

        try:
            async with httpx.AsyncClient(timeout=self.cfg.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.cfg.api_key}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.TimeoutException as e:
            raise LLMError(f"请求超时（{self.cfg.timeout}s）") from e
        except httpx.RequestError as e:
            raise LLMError(f"网络请求失败: {e}") from e

        if resp.status_code == 401:
            raise LLMError("API Key 无效或已过期（401）")
        if resp.status_code == 429:
            raise LLMError("触发服务商限流或余额不足（429）")
        if resp.status_code >= 400:
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as e:
            raise LLMError(f"响应格式异常: {resp.text[:200]}") from e

        if not content or not content.strip():
            raise LLMError("模型返回了空内容")

        return content.strip()

    async def test(self) -> tuple[bool, str]:
        """连通性测试，供控制台「测试连接」按钮调用"""
        try:
            reply = await self.complete(
                "你是一个测试助手，只回复用户要求的内容。",
                "请只回复两个字：正常",
            )
            return True, f"连接成功 · {self.model} · 返回「{reply[:20]}」"
        except LLMError as e:
            return False, str(e)
        except Exception as e:
            logger.exception("连接测试异常")
            return False, f"未知错误: {e}"
