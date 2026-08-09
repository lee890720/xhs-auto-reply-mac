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

笔记归属信息不在 DOM 里 —— 通知条目只有封面图和用户主页链接，没有笔记链接。
note_id 只能从通知页自己请求的 mentions 接口响应里拿，所以这里顺带拦截它。
拿到 note_id 后再开详情页读 __INITIAL_STATE__ 取正文，供生成回复时理解上下文。
"""
import asyncio
import hashlib
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from loguru import logger
from playwright.async_api import async_playwright, BrowserContext, Page

from .paths import BROWSER_DIR, SCREENSHOT_DIR, ensure_dirs

NOTIFICATION_URL = "https://www.xiaohongshu.com/notification"
HOME_URL = "https://www.xiaohongshu.com"
NOTE_URL = "https://www.xiaohongshu.com/explore/{note_id}?xsec_token={token}&xsec_source=pc_notice"
# 通知页拉取「评论和@」列表的接口，响应里带 note_id 与访问笔记所需的 xsec_token
MENTIONS_API = "/api/sns/web/v1/you/mentions"

# 评论被删除后，通知页会留下一条占位记录。它没有回复按钮，内容是下面这些
# 固定文案。真正的过滤依据是「有没有 .action-reply」，这里只作二次确认。
PLACEHOLDER_CONTENTS = {
    "原评论已删除",
    "该评论已删除",
    "评论已删除",
    "笔记已删除",
    "该内容已删除",
}


# 表情在接口里是文字代码（[笑哭R]），在页面上被渲染成 <img>，
# 于是同一条评论两边取到的文本不一样。比对前统一剥掉。
_EMOJI_TAG = re.compile(r"\[[^\[\]]{1,12}\]")


def _normalize_content(text: str) -> str:
    return _EMOJI_TAG.sub("", text).strip()


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
class NoteContent:
    """一篇笔记的文字内容"""
    note_id: str
    title: str = ""
    body: str = ""
    tags: list[str] = field(default_factory=list)
    note_type: str = ""

    @property
    def is_empty(self) -> bool:
        return not (self.title.strip() or self.body.strip())


@dataclass
class RawComment:
    author: str
    content: str
    note_image_url: str
    raw_time: str
    msg_type: str = "comment"
    # 以下几项来自 mentions 接口，DOM 里没有。拿不到时为空字符串。
    note_id: str = ""
    comment_id: str = ""
    note_xsec_token: str = ""
    note_title: str = ""

    @property
    def fingerprint(self) -> str:
        """
        去重指纹。

        用「类型+作者+内容+封面图」哈希。虽然现在能从 mentions 接口拿到真实的
        comment_id，但指纹算法刻意不改 —— 换算法会让库里所有历史评论的指纹失配，
        被当成新评论重复回复一遍。comment_id 另存一列备用。
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
        # 最近一次通知页请求解析出的元数据。列表保持接口原始顺序（与页面渲染
        # 顺序一致，用于按位置配对），字典按「作者+规范化内容」索引作为兜底。
        self._mentions_list: list[dict] = []
        self._mentions_index: dict[tuple[str, str], dict] = {}
        self._mentions_tasks: set[asyncio.Task] = set()
        # 本轮导航有没有收到过 mentions 接口的正常响应。
        # 它是比 DOM 更可靠的登录判据：页面渲染慢时 DOM 是空的，但接口已经回了。
        self._mentions_ok = False

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
        self.page.on("response", self._on_response)
        logger.info(f"浏览器已启动（headless={self.headless}）")

    # ==================== mentions 接口拦截 ====================

    def _on_response(self, response) -> None:
        """
        response 事件回调必须是同步的，而读取响应体要 await，所以另起任务。
        任务句柄要留着强引用，否则可能在读完之前被 GC 掉。
        """
        if MENTIONS_API not in response.url:
            return
        task = asyncio.create_task(self._absorb_mentions(response))
        self._mentions_tasks.add(task)
        task.add_done_callback(self._mentions_tasks.discard)

    async def _absorb_mentions(self, response) -> None:
        try:
            data = await response.json()
        except Exception as e:
            logger.debug(f"mentions 响应解析失败: {e}")
            return

        # 未登录时这个接口不会给出 data，所以能拿到就说明登录态有效，
        # 哪怕一条通知都没有。
        if isinstance(data.get("data"), dict):
            self._mentions_ok = True

        rows: list[dict] = []
        for msg in (data.get("data") or {}).get("message_list") or []:
            comment = msg.get("comment_info") or {}
            item = msg.get("item_info") or {}
            user = msg.get("user_info") or {}
            author = (user.get("nickname") or "").strip()
            content = (comment.get("content") or "").strip()
            rows.append({
                "author": author,
                "content": content,
                "note_id": item.get("id") or "",
                "note_xsec_token": item.get("xsec_token") or "",
                "comment_id": comment.get("id") or "",
                # 接口顺带给了笔记标题（标题为空的笔记不带这个字段）。
                # 零成本，抓详情页失败时用它兜底，好过完全没有上下文。
                "note_title": (item.get("content") or "").strip(),
            })

        if not rows:
            return

        self._mentions_list = rows
        self._mentions_index = {
            (r["author"], _normalize_content(r["content"])): r
            for r in rows
            if r["author"] and r["content"]
        }
        logger.debug(f"从 mentions 接口解析到 {len(rows)} 条元数据")

    def _match_meta(self, idx: int, author: str, content: str) -> dict:
        """
        把一个 DOM 条目对上 mentions 接口里的元数据。

        优先按位置配对：两边顺序一致，而且这样完全绕开了表情文本差异。
        位置对不上（接口分页、页面结构调整）时再退回内容匹配。
        """
        if idx < len(self._mentions_list):
            row = self._mentions_list[idx]
            if row["author"] == author:
                return row
        return self._mentions_index.get((author, _normalize_content(content)), {})

    async def _drain_mentions_tasks(self) -> None:
        """等待在飞的响应解析任务收尾，确保 collect 读到的是本轮数据"""
        if self._mentions_tasks:
            await asyncio.gather(*list(self._mentions_tasks), return_exceptions=True)

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
        self._mentions_ok = False        # 只认本次导航收到的响应
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
            # 别静默吞掉。冷启动（浏览器刚装好、profile 无缓存）时确实会超时，
            # 而下游一旦把「DOM 是空的」当成掉登录，引擎就停摆了。
            logger.warning("等待通知条目渲染超时，页面可能还在加载")
        await asyncio.sleep(3)
        await self._drain_mentions_tasks()

    async def collect(self, limit: int = 5) -> list[RawComment]:
        """采集通知页上的评论"""
        if not self.page:
            raise BrowserUnavailable("浏览器未启动")

        await self._goto_notifications()

        items = await self.page.query_selector_all(".notification-page .container")
        if not items:
            # 接口正常回了就是登录着的，DOM 空只是没渲染完。这时候判掉登录会让
            # 引擎停摆等人来扫码，而实际上什么事都没有——冷启动时真的发生过。
            if self._mentions_ok:
                logger.warning("通知页 DOM 尚未渲染出条目，但接口有响应，本轮跳过")
                return []
            # 页面上一条通知都没有时，先确认不是登录掉了
            if not await self._looks_logged_in():
                raise NotLoggedIn("通知页无内容且未检测到登录态")
            logger.debug("通知页暂无评论")
            return []

        logger.info(f"通知页发现 {len(items)} 条通知")
        out: list[RawComment] = []
        skipped = 0
        unmatched = 0

        # 注意先过滤再计数：如果先按 limit 截断，开头连续几条删除残骸会把
        # 真正需要回复的评论全挤出这一轮。
        # 用 enumerate 而不是只遍历可回复条目：位置配对要求索引与接口顺序对齐，
        # 所以被跳过的条目也必须占掉它的索引位。
        for idx, item in enumerate(items):
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

                # 对上 mentions 接口的元数据，拿到 DOM 里根本没有的 note_id
                meta = self._match_meta(idx, author, content)
                if not meta.get("note_id"):
                    unmatched += 1

                out.append(RawComment(
                    author=author,
                    content=content,
                    note_image_url=note_image_url,
                    raw_time=raw_time,
                    note_id=meta.get("note_id", ""),
                    comment_id=meta.get("comment_id", ""),
                    note_xsec_token=meta.get("note_xsec_token", ""),
                    note_title=meta.get("note_title", ""),
                ))
            except Exception as e:
                logger.debug(f"解析通知条目失败: {e}")
                continue

        if skipped:
            logger.info(f"跳过 {skipped} 条不可回复的通知（已删除评论等）")
        if unmatched:
            logger.warning(
                f"{unmatched} 条评论未能对上 mentions 接口数据，这些评论拿不到笔记上下文"
            )
        return out

    async def fetch_note(self, note_id: str, xsec_token: str) -> NoteContent:
        """
        抓一篇笔记的文字内容。

        走 __INITIAL_STATE__ 而不是 DOM：explore 页右侧有推荐笔记流，
        .title 这类选择器会命中别人的笔记，而 noteDetailMap 是按 note_id 精确索引的。
        """
        if not self.page:
            raise BrowserUnavailable("浏览器未启动")
        if not note_id:
            raise ValueError("note_id 为空")

        url = NOTE_URL.format(note_id=note_id, token=xsec_token)
        await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(random.uniform(2.0, 3.5))

        data = await self.page.evaluate(
            """(nid) => {
                const map = window.__INITIAL_STATE__?.note?.noteDetailMap;
                if (!map) return null;
                const note = map[nid]?.note;
                if (!note) return null;
                return {
                    title: note.title || '',
                    body: note.desc || '',
                    tags: (note.tagList || []).map(t => t.name).filter(Boolean),
                    note_type: note.type || '',
                };
            }""",
            note_id,
        )

        if not data:
            # 笔记被删、设为私密，或页面结构变了
            if not await self._looks_logged_in():
                raise NotLoggedIn("抓取笔记时未检测到登录态")
            raise ReplyTargetGone(f"读不到笔记 {note_id} 的内容，可能已删除或不可见")

        return NoteContent(
            note_id=note_id,
            title=(data.get("title") or "").strip(),
            body=(data.get("body") or "").strip(),
            tags=data.get("tags") or [],
            note_type=data.get("note_type") or "",
        )

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
