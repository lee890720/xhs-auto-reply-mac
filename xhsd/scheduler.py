"""
调度引擎。

单循环串行执行「采集 → 生成草稿 → 发送已批准的回复」，
因为采集和发送共用同一个浏览器页面，并发会互相打断。

审核动作会触发 wake 事件，让循环提前醒来发送，不必等满一个轮询周期。
"""
import asyncio
import random
from datetime import datetime, time as dtime

from loguru import logger

from . import db, settings_store
from .collector import BrowserSession, NotLoggedIn, RawComment
from .generator import ReplyGenerator, SensitiveHit
from .llm import LLMError

MAX_SEND_ATTEMPTS = 3


def _parse_hhmm(value: str, fallback: dtime) -> dtime:
    try:
        h, m = value.split(":")
        return dtime(int(h), int(m))
    except (ValueError, AttributeError):
        return fallback


def in_quiet_hours(cfg: settings_store.ReplyConfig, now: datetime | None = None) -> bool:
    """判断当前是否处于静默时段（支持跨午夜区间）"""
    if not cfg.quiet_enabled:
        return False
    current = (now or datetime.now()).time()
    start = _parse_hhmm(cfg.quiet_start, dtime(23, 0))
    end = _parse_hhmm(cfg.quiet_end, dtime(8, 0))
    if start <= end:
        return start <= current < end
    return current >= start or current < end   # 跨午夜


class Engine:
    def __init__(self, headless: bool = False):
        self.browser = BrowserSession(headless=headless)
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()

        self.running = False
        self.state = "stopped"          # stopped/starting/running/quiet/need_login/error
        self.last_error = ""
        self.last_poll_at = ""
        self.need_login = False

    # ==================== 生命周期 ====================

    async def start(self) -> tuple[bool, str]:
        if self.running:
            return False, "引擎已在运行"
        self.running = True
        self.state = "starting"
        self.last_error = ""
        self._task = asyncio.create_task(self._run(), name="xhsd-engine")
        return True, "引擎已启动"

    async def stop(self) -> tuple[bool, str]:
        if not self.running:
            return False, "引擎未在运行"
        self.running = False
        self._wake.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.browser.close()
        self.state = "stopped"
        logger.info("引擎已停止")
        return True, "引擎已停止"

    def wake(self) -> None:
        """让主循环提前醒来（审核通过后调用）"""
        self._wake.set()

    def snapshot(self) -> dict:
        cfg = settings_store.load()
        return {
            "running": self.running,
            "state": self.state,
            "need_login": self.need_login,
            "last_error": self.last_error,
            "last_poll_at": self.last_poll_at,
            "browser_open": self.browser.is_open,
            "mode": cfg.reply.mode,
            "quiet_now": in_quiet_hours(cfg.reply),
            "today_sent": db.count_sent_today(),
            "daily_limit": cfg.reply.daily_limit,
            "pending_review": db.count_by_status(db.STATUS_PENDING_REVIEW),
            "approved_waiting": db.count_by_status(db.STATUS_APPROVED),
            "llm_configured": bool(cfg.llm.api_key),
        }

    # ==================== 主循环 ====================

    async def _run(self) -> None:
        try:
            await self.browser.open()
            if not await self.browser.check_login():
                self.need_login = True
                self.state = "need_login"
                logger.error("小红书未登录，请在弹出的浏览器窗口中扫码登录后点击「重新检测」")
            else:
                self.need_login = False
                self.state = "running"
                logger.info("登录态正常，开始监控")
        except Exception as e:
            self.state = "error"
            self.last_error = f"浏览器启动失败: {e}"
            logger.error(self.last_error)
            self.running = False
            return

        while self.running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.state = "error"
                self.last_error = str(e)
                logger.exception(f"轮询异常: {e}")

            cfg = settings_store.load()
            await self._sleep_interruptible(cfg.reply.poll_interval)

    async def _sleep_interruptible(self, seconds: float) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _tick(self) -> None:
        cfg = settings_store.load()

        if self.need_login:
            # 登录态失效时不做任何页面操作，等人工处理后调用 recheck_login()
            return

        if in_quiet_hours(cfg.reply):
            self.state = "quiet"
            logger.debug("处于静默时段，跳过本轮")
            return

        self.state = "running"
        async with self._lock:
            await self._collect_and_draft(cfg)
            await self._drain_send_queue(cfg)
        self.last_poll_at = db.now()

    # ==================== 采集与生成 ====================

    async def _collect_and_draft(self, cfg: settings_store.AppConfig) -> None:
        try:
            comments = await self.browser.collect(limit=cfg.reply.max_per_poll)
        except NotLoggedIn as e:
            self.need_login = True
            self.state = "need_login"
            self.last_error = str(e)
            logger.error(f"登录态失效: {e}")
            return

        fresh: list[tuple[int, RawComment]] = []
        for c in comments:
            msg_id = db.insert_message(
                fingerprint=c.fingerprint,
                msg_type=c.msg_type,
                author=c.author,
                content=c.content,
                note_image_url=c.note_image_url,
                raw_time=c.raw_time,
            )
            if msg_id:                       # None 表示指纹已存在，之前处理过
                fresh.append((msg_id, c))

        if not fresh:
            logger.debug("本轮无新评论")
            return

        logger.info(f"本轮新增 {len(fresh)} 条评论，开始生成草稿")
        generator = ReplyGenerator(cfg)

        for msg_id, c in fresh:
            await self._draft_one(generator, cfg, msg_id, c.author, c.content)

    async def _draft_one(
        self,
        generator: ReplyGenerator,
        cfg: settings_store.AppConfig,
        msg_id: int,
        author: str,
        content: str,
    ) -> None:
        provider, model = cfg.llm.provider, generator.client.model
        try:
            draft = await generator.generate(author, content, msg_type="comment")
        except SensitiveHit as e:
            db.insert_reply(msg_id, "", db.STATUS_BLOCKED, provider, model)
            logger.warning(f"「{author}」的评论命中敏感词「{e.keyword}」，已转人工")
            return
        except LLMError as e:
            rid = db.insert_reply(msg_id, "", db.STATUS_PENDING_REVIEW, provider, model)
            db.update_reply_status(rid, db.STATUS_PENDING_REVIEW, error=str(e))
            logger.error(f"生成回复失败（{author}）: {e}")
            return

        # 全自动模式直接进发送队列；审核模式进待审队列
        status = db.STATUS_APPROVED if cfg.reply.mode == "auto" else db.STATUS_PENDING_REVIEW
        db.insert_reply(msg_id, draft, status, provider, model)
        logger.info(f"草稿已生成（{author}）: {draft[:40]}")

    async def regenerate(self, reply_id: int) -> tuple[bool, str]:
        """重新生成某条草稿，供控制台「重新生成」按钮调用"""
        row = db.get_reply(reply_id)
        if not row:
            return False, "记录不存在"

        cfg = settings_store.load()
        generator = ReplyGenerator(cfg)
        try:
            draft = await generator.generate(
                row["author"], row["content"], row["note_title"], row["msg_type"]
            )
        except SensitiveHit as e:
            return False, f"命中敏感词「{e.keyword}」"
        except LLMError as e:
            return False, str(e)

        db.update_reply_status(
            reply_id, db.STATUS_PENDING_REVIEW, final_text=draft, error=""
        )
        db.execute("UPDATE replies SET draft = ? WHERE id = ?", (draft, reply_id))
        return True, draft

    # ==================== 发送 ====================

    async def _drain_send_queue(self, cfg: settings_store.AppConfig) -> None:
        while self.running:
            sent_today = db.count_sent_today()
            if sent_today >= cfg.reply.daily_limit:
                logger.warning(f"今日已发送 {sent_today} 条，达到上限 {cfg.reply.daily_limit}")
                return

            row = db.next_approved_reply()
            if not row:
                return

            text = row["final_text"] or row["draft"]
            if not text:
                db.update_reply_status(row["id"], db.STATUS_FAILED, error="回复内容为空")
                continue

            db.bump_attempts(row["id"])
            try:
                await self.browser.send_reply(row["author"], row["content"], text)
                db.update_reply_status(row["id"], db.STATUS_SENT, mark_sent=True, error="")
            except NotLoggedIn as e:
                self.need_login = True
                self.state = "need_login"
                logger.error(f"发送时发现登录态失效: {e}")
                return
            except Exception as e:
                attempts = (row["attempts"] or 0) + 1
                if attempts >= MAX_SEND_ATTEMPTS:
                    db.update_reply_status(row["id"], db.STATUS_FAILED, error=str(e))
                    logger.error(f"回复 {row['author']} 失败（已重试 {attempts} 次）: {e}")
                else:
                    # 留在 approved 队列，下轮再试
                    db.update_reply_status(row["id"], db.STATUS_APPROVED, error=str(e))
                    logger.warning(f"回复 {row['author']} 失败，稍后重试: {e}")
                    return

            # 两条回复之间随机停顿，避免固定节奏被识别
            delay = random.uniform(cfg.reply.min_delay, cfg.reply.max_delay)
            logger.debug(f"等待 {delay:.1f}s 后处理下一条")
            await asyncio.sleep(delay)

    # ==================== 登录 ====================

    async def recheck_login(self) -> tuple[bool, str]:
        """人工扫码后调用，重新检测登录态"""
        if not self.browser.is_open:
            try:
                await self.browser.open()
            except Exception as e:
                return False, f"浏览器启动失败: {e}"

        async with self._lock:
            ok = await self.browser.check_login()

        self.need_login = not ok
        if ok:
            self.state = "running" if self.running else "stopped"
            self.last_error = ""
            self.wake()
            return True, "登录态正常"
        self.state = "need_login"
        return False, "仍未检测到登录状态，请在浏览器窗口中完成扫码"

    async def open_browser_for_login(self) -> tuple[bool, str]:
        """打开浏览器窗口供人工扫码"""
        try:
            await self.browser.open()
            if self.browser.page:
                await self.browser.page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded")
            return True, "已打开浏览器窗口，请扫码登录后点击「重新检测」"
        except Exception as e:
            return False, f"打开失败: {e}"
