"""
小红书自动回复工作流 - AI 回复生成器
对接 Coze API v3，根据账号人设生成智能回复。

Coze v3 非流式对话流程：
  1. POST /v3/bot/chat        发起对话，获取 chat_id + conversation_id
  2. GET  /v3/chat/retrieve   轮询对话状态，直到 status=completed
  3. GET  /v3/chat/message/list  获取回复消息列表，取 type=answer
"""
import httpx
import asyncio
from loguru import logger

from config import settings
from browser_ctrl import Message


class ReplyGenerator:
    """基于 Coze API v3 的回复生成器"""

    def __init__(self):
        self.client = httpx.AsyncClient(
            base_url=settings.coze_api_base,
            headers={
                "Authorization": f"Bearer {settings.coze_api_token}",
                "Content-Type": "application/json",
            },
            timeout=settings.coze_timeout,
        )

    async def generate(self, msg: Message) -> str:
        """
        根据消息内容生成回复。

        Args:
            msg: 待回复的消息对象

        Returns:
            生成的回复文本
        """
        # ---- 敏感词检查 ----
        for keyword in settings.sensitive_keywords:
            if keyword in msg.content:
                logger.warning(
                    f"检测到敏感词 '{keyword}'，使用兜底回复"
                )
                return settings.sensitive_fallback

        # ---- 构建对话上下文 ----
        type_label = "评论" if msg.msg_type == "comment" else "私信"
        context = f"一位小红书用户{type_label}你"
        if msg.msg_type == "comment" and msg.note_title:
            context += f"的笔记《{msg.note_title}》"
        context += f"：\n{msg.content}"

        # ---- 调用 Coze v3 对话 API ----
        try:
            # 步骤1：发起对话
            payload = {
                "bot_id": settings.coze_bot_id,
                "user_id": "xhs_auto_reply_bot",
                "stream": False,
                "auto_save_history": True,
                "additional_messages": [
                    {
                        "role": "user",
                        "content": f"{context}\n\n请生成一条回复。",
                        "content_type": "text",
                    }
                ],
            }

            resp = await self.client.post("/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()

            if data.get("code") != 0:
                logger.error(f"Coze API 发起对话失败: {data.get('msg')}")
                return await self._fallback_reply(msg)

            chat_data = data.get("data", {})
            chat_id = chat_data.get("id", "")
            conversation_id = chat_data.get("conversation_id", "")

            if not chat_id or not conversation_id:
                logger.error(f"Coze API 返回缺少 chat_id 或 conversation_id: {data}")
                return await self._fallback_reply(msg)

            logger.info(f"对话已创建: chat_id={chat_id}")

            # 步骤2：轮询对话状态
            status = await self._poll_chat_status(chat_id, conversation_id)
            if status != "completed":
                logger.error(f"对话未正常完成，状态: {status}")
                return await self._fallback_reply(msg)

            # 步骤3：获取回复消息
            reply = await self._fetch_answer(chat_id, conversation_id)
            if reply:
                logger.info(f"生成回复 (对 {msg.author}): {reply[:50]}...")
                return reply.strip()

            logger.error("Coze API 未返回 answer 类型消息")
            return await self._fallback_reply(msg)

        except httpx.TimeoutException:
            logger.error("Coze API 请求超时")
        except httpx.HTTPStatusError as e:
            logger.error(f"Coze API HTTP 错误: {e.response.status_code} - {e.response.text}")
        except Exception as e:
            logger.error(f"Coze API 调用失败: {e}")

        # ---- 降级：使用本地模板兜底 ----
        return await self._fallback_reply(msg)

    async def _poll_chat_status(self, chat_id: str, conversation_id: str) -> str:
        """轮询对话状态，直到终态"""
        max_retries = 30
        for i in range(max_retries):
            await asyncio.sleep(1)
            try:
                resp = await self.client.get(
                    "/chat/retrieve",
                    params={
                        "chat_id": chat_id,
                        "conversation_id": conversation_id,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

                if data.get("code") == 0:
                    status = data.get("data", {}).get("status", "")
                    if status in ("completed", "failed", "canceled"):
                        return status
                    logger.debug(f"对话状态: {status} (第 {i+1} 次轮询)")
                else:
                    logger.warning(f"轮询返回异常: {data.get('msg')}")
            except Exception as e:
                logger.warning(f"轮询请求失败: {e}")

        return "timeout"

    async def _fetch_answer(self, chat_id: str, conversation_id: str) -> str:
        """获取对话的回复消息"""
        resp = await self.client.get(
            "/chat/message/list",
            params={
                "chat_id": chat_id,
                "conversation_id": conversation_id,
            },
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") == 0:
            messages = data.get("data", [])
            for m in messages:
                if m.get("role") == "assistant" and m.get("type") == "answer":
                    return m.get("content", "")

        return ""

    async def _fallback_reply(self, msg: Message) -> str:
        """Coze 不可用时的本地兜底回复"""
        fallbacks = [
            "收到啦～感谢你的关注，稍后回复你哦🙏",
            "看到你的消息了，太忙了晚点回你～",
            "谢谢你的留言，我回头仔细看看！",
        ]
        import random
        return random.choice(fallbacks)

    async def close(self):
        """关闭 HTTP 客户端"""
        await self.client.aclose()
