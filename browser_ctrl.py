"""
小红书自动回复工作流 - 浏览器控制器
负责连接已登录的 Chrome 浏览器，操控小红书网页版。

DOM 结构（通过实际页面检查确认）：
  评论通知页 /notification:
    - 每条通知: div.notification-page > div.container
    - 用户名:   div.container > div.main > div.info > div.user-info > a
    - 评论内容: div.container div.interaction-content
    - 时间:     div.container span.interaction-time
    - 回复按钮: div.container div.action-reply
    - 回复输入框: 点击回复按钮后出现的输入区域
"""
import asyncio
import random
from pathlib import Path
from dataclasses import dataclass

from playwright.async_api import async_playwright, Browser, Page, BrowserContext
from loguru import logger

from config import settings


@dataclass
class Message:
    """一条待处理的消息"""
    msg_id: str             # 唯一标识（用于去重）
    msg_type: str           # "comment" | "message"
    author: str             # 发送者昵称
    content: str            # 消息正文
    note_title: str = ""      # 关联笔记标题（评论场景）
    note_image_url: str = ""  # 笔记封面图 URL
    raw_selector: str = ""    # 页面元素定位，用于回复


class BrowserController:
    """小红书浏览器控制器"""

    def __init__(self):
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._playwright = None

    async def connect(self) -> bool:
        """通过 Playwright 启动 Chrome for Testing（隔离模式）"""
        try:
            self._playwright = await async_playwright().start()

            # 创建用户数据目录
            user_data_dir = Path(settings.chrome_user_data_dir)
            user_data_dir.mkdir(parents=True, exist_ok=True)

            # 使用 launch_persistent_context 启动 Chrome for Testing
            # （Playwright 不允许在 launch() 中使用 --user-data-dir 参数）
            self.context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                headless=settings.headless,
                no_viewport=True,
            )

            self.page = (
                self.context.pages[0]
                if self.context.pages
                else await self.context.new_page()
            )
            self.page.set_default_timeout(30000)
            logger.info("浏览器连接成功 (Chrome for Testing, 隔离模式)")
            return True
        except Exception as e:
            logger.error(f"浏览器连接失败: {e}")
            return False

    async def check_login_status(self) -> bool:
        """检查小红书是否已登录"""
        try:
            await self.page.goto(
                "https://www.xiaohongshu.com", wait_until="domcontentloaded"
            )
            await asyncio.sleep(3)
            # 检查侧边栏是否有"通知"或"我"的链接（表示已登录）
            logged_in = await self.page.evaluate("""() => {
                const links = document.querySelectorAll('a');
                for (const a of links) {
                    if (a.href && a.href.includes('/user/profile/')) return true;
                }
                return false;
            }""")
            if logged_in:
                logger.info("已检测到登录状态")
                return True
            else:
                logger.warning("未检测到登录状态，请先在浏览器中手动登录小红书")
                return False
        except Exception as e:
            logger.warning(f"登录状态检查失败: {e}")
            return False

    async def _screenshot(self, name: str):
        """调试截图"""
        path = Path(settings.screenshot_dir)
        path.mkdir(parents=True, exist_ok=True)
        await self.page.screenshot(path=str(path / f"{name}.png"))

    # ==================== 评论监控 ====================

    async def fetch_new_comments(self) -> list[Message]:
        """
        获取评论通知页面中的未回复评论。
        页面: https://www.xiaohongshu.com/notification
        DOM:  div.notification-page > div.container（每条通知）
        """
        messages: list[Message] = []
        try:
            logger.info("正在检查评论通知...")
            await self.page.goto(
                settings.comments_url, wait_until="domcontentloaded", timeout=30000
            )
            await asyncio.sleep(4)

            # 确保在"评论和@"tab
            try:
                comment_tab = await self.page.query_selector('text=评论和@')
                if comment_tab:
                    await comment_tab.click()
                    await asyncio.sleep(3)
            except Exception:
                pass

            # 使用真实 DOM 结构的选择器
            # 每条通知是 .notification-page 下的 .container
            comment_items = await self.page.query_selector_all(
                '.notification-page .container'
            )

            logger.info(f"发现 {len(comment_items)} 条评论通知")

            for item in comment_items[: settings.max_messages_per_poll]:
                try:
                    # 提取评论者昵称: div.user-info > a
                    author_el = await item.query_selector('.user-info a')
                    author = (
                        (await author_el.inner_text()).strip()[:30]
                        if author_el
                        else "未知用户"
                    )

                    # 跳过没有用户名的条目（可能是其他类型通知）
                    if author == "未知用户":
                        continue

                    # 提取评论内容: div.interaction-content
                    content_el = await item.query_selector('.interaction-content')
                    content = (
                        (await content_el.inner_text()).strip()[:200]
                        if content_el
                        else ""
                    )

                    if not content:
                        continue

                    # 提取时间（用于日志）
                    time_el = await item.query_selector('.interaction-time')
                    time_str = (
                        (await time_el.inner_text()).strip()
                        if time_el
                        else ""
                    )

                    # 提取笔记封面图: div.extra > img.extra-image
                    note_img_el = await item.query_selector('.extra img.extra-image')
                    note_image_url = ""
                    if note_img_el:
                        note_image_url = await note_img_el.get_attribute('src') or ""

                    # 生成唯一 ID 时包含笔记图片 URL 的一部分（帮助去重）
                    img_hash = note_image_url.split('/')[-1].split('?')[0][:20] if note_image_url else ""
                    msg_id = f"comment_{author}_{content[:20]}_{img_hash}"

                    messages.append(
                        Message(
                            msg_id=msg_id,
                            msg_type="comment",
                            author=author,
                            content=content,
                            note_image_url=note_image_url,
                            raw_selector=str(id(item)),
                        )
                    )
                    logger.debug(f"  → [{time_str}] {author}: {content[:40]}")

                except Exception as e:
                    logger.debug(f"解析评论条目失败: {e}")
                    continue

        except Exception as e:
            logger.error(f"获取评论失败: {e}")
            await self._screenshot("error_comments")

        return messages

    # ==================== 私信监控 ====================
    # 注意：小红书网页版 /message 返回 404
    # 私信功能需要通过其他入口，暂时禁用

    async def fetch_new_messages(self) -> list[Message]:
        """
        获取私信页面的未回复消息。
        当前小红书网页版私信入口不可用（/message 返回 404），
        暂时返回空列表。
        """
        logger.debug("私信监控暂未启用（网页版私信入口不可用）")
        return []

    # ==================== 回复发送 ====================

    async def send_reply(self, msg: Message, reply_text: str) -> bool:
        """
        向指定的评论/私信发送回复。
        """
        try:
            if msg.msg_type == "comment":
                return await self._reply_to_comment(msg, reply_text)
            else:
                return await self._reply_to_message(msg, reply_text)
        except Exception as e:
            logger.error(f"回复发送失败: {e}")
            await self._screenshot("error_reply")
            return False

    async def _reply_to_comment(self, msg: Message, reply_text: str) -> bool:
        """
        回复评论流程：
        1. 导航到通知页
        2. 用 JavaScript 精确定位并点击"回复"按钮（避免误触笔记链接弹出遮罩层）
        3. 在出现的输入框中输入回复
        4. 点击发送或按回车
        """
        try:
            # 导航到评论通知页
            await self.page.goto(
                settings.comments_url, wait_until="domcontentloaded", timeout=30000
            )
            await asyncio.sleep(3)

            # 确保在"评论和@"tab
            try:
                comment_tab = await self.page.query_selector('text=评论和@')
                if comment_tab:
                    await comment_tab.click()
                    await asyncio.sleep(2)
            except Exception:
                pass

            # 用 JavaScript 找到目标条目并点击回复按钮
            # 这样可以避免 Playwright click 时误触其他元素
            clicked = await self.page.evaluate("""(authorName) => {
                const containers = document.querySelectorAll('.notification-page .container');
                for (const c of containers) {
                    const userLink = c.querySelector('.user-info a');
                    if (userLink && userLink.textContent.trim() === authorName) {
                        const replyBtn = c.querySelector('.action-reply');
                        if (replyBtn) {
                            replyBtn.click();
                            return true;
                        }
                        // 尝试 action-text
                        const actionText = c.querySelector('.action-text');
                        if (actionText && actionText.textContent.trim() === '回复') {
                            actionText.click();
                            return true;
                        }
                    }
                }
                return false;
            }""", msg.author)

            if not clicked:
                logger.error(f"未找到 {msg.author} 的回复按钮")
                await self._screenshot("error_find_reply_btn")
                return False

            logger.info(f"已点击回复按钮: {msg.author}")
            await asyncio.sleep(random.uniform(1, 2))

            # 如果弹出了笔记详情遮罩层，先关闭它
            try:
                mask = await self.page.query_selector('.note-detail-mask, .close-button')
                if mask:
                    # 按 Escape 关闭遮罩
                    await self.page.keyboard.press("Escape")
                    await asyncio.sleep(1)
                    logger.debug("已关闭笔记详情遮罩层")
            except Exception:
                pass

            # 查找回复输入框
            # 小红书通知页的回复输入框是 textarea
            reply_input = None
            selectors = [
                'textarea.reply-input',
                'textarea[class*="reply"]',
                'textarea[class*="input"]',
                'textarea[class*="comment"]',
                '[contenteditable="true"]',
                'textarea',
            ]
            for sel in selectors:
                try:
                    reply_input = await self.page.wait_for_selector(sel, timeout=3000)
                    if reply_input:
                        break
                except Exception:
                    continue

            if not reply_input:
                logger.error(f"未找到回复输入框: {msg.author}")
                await self._screenshot("error_no_input")
                return False

            await reply_input.click()
            await asyncio.sleep(0.3)

            # 模拟人类打字
            await reply_input.type(reply_text, delay=random.randint(30, 80))
            await asyncio.sleep(random.uniform(0.5, 1))

            # 查找发送按钮
            send_clicked = await self.page.evaluate("""() => {
                const btns = document.querySelectorAll('button, [class*="submit"], [class*="send"]');
                for (const b of btns) {
                    const text = (b.textContent || '').trim();
                    if (text === '回复' || text === '发送' || text === 'Send') {
                        b.click();
                        return true;
                    }
                }
                return false;
            }""")

            if send_clicked:
                logger.info(f"已回复评论: {msg.author}")
                await asyncio.sleep(random.uniform(1, 3))
                return True
            else:
                # 尝试 Ctrl+Enter 或 Enter 发送
                await self.page.keyboard.press("Enter")
                await asyncio.sleep(1)
                logger.info(f"已回复评论(回车发送): {msg.author}")
                return True

        except Exception as e:
            logger.error(f"回复评论失败: {e}")
            await self._screenshot("error_reply_comment")
            return False

    async def _reply_to_message(self, msg: Message, reply_text: str) -> bool:
        """回复私信（当前不可用）"""
        logger.warning("私信回复功能暂未启用")
        return False

    async def disconnect(self):
        """断开浏览器连接"""
        if self.context:
            try:
                await self.context.close()
            except Exception as e:
                logger.debug(f"context.close() 异常: {e}")
            logger.info("浏览器已断开")
        if self._playwright:
            await self._playwright.stop()
