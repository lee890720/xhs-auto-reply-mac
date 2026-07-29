"""
小红书自动回复工作流 - 主程序
工作流程：
  1. 连接已登录的 Chrome 浏览器
  2. 轮询小红书评论和私信页面
  3. 识别新消息 → 生成智能回复 → 发送
  4. 记录已处理消息，避免重复

使用方式：
  1. 复制 .env.example 为 .env，填入 Coze API 配置
  2. 以调试模式启动 Chrome:
     macOS: /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --remote-debugging-port=9222
  3. 在 Chrome 中手动登录小红书
  4. 运行: python main.py
"""
import asyncio
import signal
import sys
import random
from pathlib import Path

from loguru import logger

from config import settings
from browser_ctrl import BrowserController
from reply_generator import ReplyGenerator
from state_manager import StateManager


# ============ 日志配置 ============
log_dir = Path(settings.log_file).parent
log_dir.mkdir(parents=True, exist_ok=True)
logger.remove()
logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level:<7} | {message}")
logger.add(
    settings.log_file,
    rotation="1 day",
    retention="30 days",
    level="DEBUG",
    encoding="utf-8",
)


class AutoReplyWorker:
    """自动回复工作器"""

    def __init__(self):
        self.browser = BrowserController()
        self.generator = ReplyGenerator()
        self.state = StateManager()
        self._running = False

    async def start(self):
        """启动工作流"""
        logger.info("=" * 50)
        logger.info("小红书自动回复工作流启动")
        logger.info(f"回复模式: {'全自动' if settings.reply_mode == 'auto' else '审核模式'}")
        logger.info(f"轮询间隔: {settings.poll_interval}s")
        logger.info(f"监控评论: {settings.monitor_comments} | 监控私信: {settings.monitor_messages}")
        logger.info("=" * 50)

        # 连接浏览器
        if not await self.browser.connect():
            logger.error("Chrome for Testing 启动失败")
            logger.info("请确认已安装 Playwright 浏览器: python3 -m playwright install chromium")
            return

        # 检查登录状态
        if not await self.browser.check_login_status():
            logger.error("未检测到小红书登录状态，请在浏览器中手动登录后重试")
            await self.browser.disconnect()
            return

        # 清理旧记录
        self.state.cleanup_old()

        # 进入主循环
        self._running = True
        logger.info("开始监控消息...")

        try:
            while self._running:
                await self._poll_once()
                await asyncio.sleep(settings.poll_interval)
        except asyncio.CancelledError:
            logger.info("收到停止信号")
        finally:
            await self.browser.disconnect()
            await self.generator.close()
            logger.info("工作流已停止")

    async def _poll_once(self):
        """执行一次轮询"""
        today_count = self.state.get_today_count()
        if today_count >= settings.daily_reply_limit:
            logger.warning(
                f"今日已回复 {today_count} 条，达到上限 {settings.daily_reply_limit}，跳过本轮"
            )
            return

        all_messages: list = []

        # 获取评论
        if settings.monitor_comments:
            comments = await self.browser.fetch_new_comments()
            all_messages.extend(comments)

        # 获取私信
        if settings.monitor_messages:
            messages = await self.browser.fetch_new_messages()
            all_messages.extend(messages)

        if not all_messages:
            logger.debug("本轮无新消息")
            return

        logger.info(f"本轮发现 {len(all_messages)} 条新消息")

        for msg in all_messages:
            if self.state.is_processed(msg.msg_id):
                continue
            if self.state.get_today_count() >= settings.daily_reply_limit:
                break
            await self._process_message(msg)

    async def _process_message(self, msg):
        """处理单条消息"""
        type_label = "评论" if msg.msg_type == "comment" else "私信"
        logger.info(f"[{type_label}] {msg.author}: {msg.content[:50]}")

        # 生成回复
        reply_text = await self.generator.generate(msg)
        if not reply_text:
            logger.warning("回复为空，跳过")
            return

        logger.info(f"生成的回复: {reply_text[:80]}")

        if settings.reply_mode == "auto":
            # 全自动模式：直接发送
            success = await self.browser.send_reply(msg, reply_text)
            self.state.mark_processed(
                msg.msg_id, reply_text,
                msg_type=msg.msg_type, author=msg.author,
                content=msg.content, status="success" if success else "failed",
                mode="auto", note_image_url=msg.note_image_url
            )
            if success:
                logger.info(f"[自动] 已回复 {msg.author}")
            else:
                logger.error(f"[自动] 回复 {msg.author} 失败")
        else:
            # 审核模式：只记录，不发送
            logger.info(
                f"[审核模式] {msg.author} 的{type_label} → 回复草稿: {reply_text}"
            )
            self.state.mark_processed(
                msg.msg_id, reply_text,
                msg_type=msg.msg_type, author=msg.author,
                content=msg.content, status="skipped", mode="review",
                note_image_url=msg.note_image_url
            )
            # 后续可对接飞书/企业微信推送通知

        # 模拟人类延迟
        delay = random.uniform(settings.reply_min_delay, settings.reply_max_delay)
        await asyncio.sleep(delay)

    def stop(self):
        """停止工作流"""
        self._running = False


async def main():
    worker = AutoReplyWorker()

    # 注册信号处理，优雅退出
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: worker.stop())

    await worker.start()


if __name__ == "__main__":
    asyncio.run(main())
