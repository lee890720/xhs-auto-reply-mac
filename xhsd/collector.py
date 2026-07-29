"""
小红书页面采集与回复执行。

与旧版的区别：
  - 浏览器常驻，不是每轮重启，减少启动开销和自动化特征
  - 登录态失效会抛出 NotLoggedIn，由调度器上报给菜单栏提醒扫码
  - 定位回复按钮时用「作者 + 内容」双重匹配，避免同一用户多条评论错配

DOM 结构（沿用旧版实测结论）：
  通知页 /notification
    每条通知   .notification-page .container
    用户名     .user-info a
    评论内容   .interaction-content
    时间       .interaction-time
    笔记封面   .extra img.extra-image
    回复按钮   .action-reply
"""
import asyncio
import hashlib
import random
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from loguru import logger
from playwright.async_api import async_playwright, BrowserContext, Page

from .paths import BROWSER_DIR, SCREENSHOT_DIR, ensure_dirs

NOTIFICATION_URL = "https://www.xiaohongshu.com/notification"
HOME_URL = "https://www.xiaohongshu.com"

# 评论被删除后，通知页会留下一条占位记录。它没有回复按钮，内容是下面这些
# 固定文案。真正的过滤依据是「有没有 .action-reply」，这里只作二次确认。
PLACEHOLDER_CONTENTS = {
    "原评论已删除",
    "该评论已删除",
    "评论已删除",
    "笔记已删除",
    "该内容已删除",
}


class NotLoggedIn(Exception):
    """浏览器里的小红书登录态失效，需要人工扫码"""


class BrowserUnavailable(Exception):
    """浏览器未启动或已崩溃"""


class ReplyTargetGone(Exception):
    """
    目标评论在页面上已经找不到了——通常是评论被作者删除。
    这种失败重试多少次都一样，调度器会直接判失败，不占用重试次数。
    """


@dataclass
class RawComment:
    author: str
    content: str
    note_image_url: str
    raw_time: str
    msg_type: str = "comment"

    @property
    def fingerprint(self) -> str:
        """
        去重指纹。

        小红书通知页不暴露稳定的评论 ID，因此用「类型+作者+内容+封面图」哈希。
        同一人在同一篇笔记下发相同内容视为同一条，这是可接受的近似。
        """
        img_key = self.note_image_url.split("/")[-1].split("?")[0][:24]
        raw = f"{self.msg_type}|{self.author}|{self.content}|{img_key}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class BrowserSession:
    """常驻的浏览器会话"""

    def __init__(self, headless: bool = False):
        self.headless = headless
        self._pw = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    @property
    def is_open(self) -> bool:
        return self.context is not None and self.page is not None

    async def open(self) -> None:
        if self.is_open:
            return
        ensure_dirs()
        self._pw = await async_playwright().start()

        # 先自己检查，否则 Playwright 抛出的是一大段 ASCII 边框提示，
        # 混在控制台错误里既难读也说不清该在哪个环境执行修复命令。
        exe = Path(self._pw.chromium.executable_path)
        if not exe.exists():
            raise BrowserUnavailable(
                "Chromium 浏览器内核未安装。请在项目目录下执行："
                "  .venv/bin/python -m playwright install chromium"
            )

        self.context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_DIR),
            headless=self.headless,
            no_viewport=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.set_default_timeout(30000)
        logger.info(f"浏览器已启动（headless={self.headless}）")

    async def close(self) -> None:
        if self.context:
            try:
                await self.context.close()
            except Exception as e:
                logger.debug(f"关闭浏览器上下文异常: {e}")
        if self._pw:
            try:
                await self._pw.stop()
            except Exception as e:
                logger.debug(f"停止 playwright 异常: {e}")
        self.context = None
        self.page = None
        self._pw = None
        logger.info("浏览器已关闭")

    async def screenshot(self, name: str) -> str:
        """调试截图，返回文件路径"""
        if not self.page:
            return ""
        ensure_dirs()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = SCREENSHOT_DIR / f"{name}-{stamp}.png"
        try:
            await self.page.screenshot(path=str(path))
            return str(path)
        except Exception as e:
            logger.debug(f"截图失败: {e}")
            return ""

    async def check_login(self) -> bool:
        """检查小红书登录态"""
        if not self.page:
            raise BrowserUnavailable("浏览器未启动")
        try:
            await self.page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)
            return await self.page.evaluate(
                """() => [...document.querySelectorAll('a')]
                       .some(a => a.href && a.href.includes('/user/profile/'))"""
            )
        except Exception as e:
            logger.warning(f"登录态检查失败: {e}")
            return False

    async def _goto_notifications(self) -> None:
        assert self.page is not None
        await self.page.goto(NOTIFICATION_URL, wait_until="domcontentloaded", timeout=30000)

        # 切到「评论和@」tab
        try:
            tab = await self.page.query_selector("text=评论和@")
            if tab:
                await tab.click()
        except Exception:
            pass

        # 通知列表是异步渲染的，固定 sleep 会读到只加载了一两条的中间态。
        # 先等第一条出现，再留一点时间让剩余条目渲染完。
        try:
            await self.page.wait_for_selector(
                ".notification-page .container", timeout=15000
            )
        except Exception:
            pass
        await asyncio.sleep(3)

    async def collect(self, limit: int = 5) -> list[RawComment]:
        """采集通知页上的评论"""
        if not self.page:
            raise BrowserUnavailable("浏览器未启动")

        await self._goto_notifications()

        items = await self.page.query_selector_all(".notification-page .container")
        if not items:
            # 页面上一条通知都没有时，先确认不是登录掉了
            if not await self._looks_logged_in():
                raise NotLoggedIn("通知页无内容且未检测到登录态")
            logger.debug("通知页暂无评论")
            return []

        logger.info(f"通知页发现 {len(items)} 条通知")
        out: list[RawComment] = []
        skipped = 0

        # 注意先过滤再计数：如果先按 limit 截断，开头连续几条删除残骸会把
        # 真正需要回复的评论全挤出这一轮。
        for item in items:
            if len(out) >= limit:
                break
            try:
                # 没有回复按钮就是不可回复的条目——评论被删后的占位记录、
                # 点赞收藏类通知都属于这种。这是最可靠的判据。
                if not await item.query_selector(".action-reply"):
                    skipped += 1
                    continue

                author_el = await item.query_selector(".user-info a")
                if not author_el:
                    continue
                author = (await author_el.inner_text()).strip()[:40]
                if not author:
                    continue

                content_el = await item.query_selector(".interaction-content")
                content = (await content_el.inner_text()).strip()[:500] if content_el else ""
                if not content or content in PLACEHOLDER_CONTENTS:
                    skipped += 1
                    continue

                time_el = await item.query_selector(".interaction-time")
                raw_time = (await time_el.inner_text()).strip() if time_el else ""

                img_el = await item.query_selector(".extra img.extra-image")
                note_image_url = (await img_el.get_attribute("src") or "") if img_el else ""

                out.append(RawComment(author, content, note_image_url, raw_time))
            except Exception as e:
                logger.debug(f"解析通知条目失败: {e}")
                continue

        if skipped:
            logger.info(f"跳过 {skipped} 条不可回复的通知（已删除评论等）")
        return out

    async def _looks_logged_in(self) -> bool:
        assert self.page is not None
        try:
            return await self.page.evaluate(
                """() => [...document.querySelectorAll('a')]
                       .some(a => a.href && a.href.includes('/user/profile/'))"""
            )
        except Exception:
            return False

    async def send_reply(self, author: str, content: str, reply_text: str) -> None:
        """
        对指定评论发送回复。失败时抛异常，异常信息会记进 replies.error。
        """
        if not self.page:
            raise BrowserUnavailable("浏览器未启动")

        await self._goto_notifications()

        # 用作者 + 内容前缀双重匹配定位条目，再点它的回复按钮。
        # 走 JS 而不是 Playwright click，是为了避免点到笔记链接弹出遮罩层。
        clicked = await self.page.evaluate(
            """([authorName, contentPrefix]) => {
                const containers = document.querySelectorAll('.notification-page .container');
                for (const c of containers) {
                    const link = c.querySelector('.user-info a');
                    if (!link || link.textContent.trim() !== authorName) continue;

                    const body = c.querySelector('.interaction-content');
                    const text = body ? body.textContent.trim() : '';
                    if (contentPrefix && !text.startsWith(contentPrefix)) continue;

                    const btn = c.querySelector('.action-reply');
                    if (btn) { btn.click(); return true; }

                    for (const el of c.querySelectorAll('.action-text')) {
                        if (el.textContent.trim() === '回复') { el.click(); return true; }
                    }
                }
                return false;
            }""",
            [author, content[:20]],
        )

        if not clicked:
            await self.screenshot("reply-target-not-found")
            raise ReplyTargetGone(
                f"页面上已找不到 {author} 的这条评论，可能已被对方删除"
            )

        await asyncio.sleep(random.uniform(1.0, 2.0))

        # 误触弹出笔记详情时按 Esc 关掉
        try:
            if await self.page.query_selector(".note-detail-mask"):
                await self.page.keyboard.press("Escape")
                await asyncio.sleep(1)
        except Exception:
            pass

        box = None
        for sel in (
            'textarea.reply-input',
            'textarea[class*="reply"]',
            'textarea[class*="comment"]',
            'textarea[class*="input"]',
            '[contenteditable="true"]',
            "textarea",
        ):
            try:
                box = await self.page.wait_for_selector(sel, timeout=3000)
                if box:
                    break
            except Exception:
                continue

        if not box:
            shot = await self.screenshot("reply-input-not-found")
            raise RuntimeError(f"未找到回复输入框（截图: {shot}）")

        await box.click()
        await asyncio.sleep(0.4)
        # 逐字输入，模拟真人打字节奏
        await box.type(reply_text, delay=random.randint(40, 110))
        await asyncio.sleep(random.uniform(0.6, 1.4))

        sent = await self.page.evaluate(
            """() => {
                const nodes = document.querySelectorAll('button, [class*="submit"], [class*="send"]');
                for (const n of nodes) {
                    const t = (n.textContent || '').trim();
                    if (t === '回复' || t === '发送' || t === 'Send') { n.click(); return true; }
                }
                return false;
            }"""
        )

        if not sent:
            await self.page.keyboard.press("Enter")

        await asyncio.sleep(random.uniform(1.5, 3.0))
        logger.info(f"已发送回复 → {author}")
