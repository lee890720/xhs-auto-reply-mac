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
from .collector import BrowserSession, NotLoggedIn, RawComment, ReplyTargetGone
from .generator import ReplyGenerator, SensitiveHit, SkipReply
from .llm import LLMError

MAX_SEND_ATTEMPTS = 3

# 判定需要登录后，每隔这么多轮自己重新检测一次。
# 无人值守时没人会去点「重新检测」，而这个判定本身可能只是页面加载慢造成的误判，
# 不自动重试的话引擎会一直停在那里，且只有菜单栏上一个「!」提示。
LOGIN_RECHECK_EVERY_TICKS = 5


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
        self._login_wait_ticks = 0      # 处于待登录状态已经空转了几轮

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

    def _mark_need_login(self, reason: str) -> None:
        """
        进入待登录状态。

        集中在一处是因为还要重置自动重检的计数器，散落在四个 except 分支里
        很容易漏掉一个，漏掉的那条路径就会让引擎永远停在待登录。
        """
        self.need_login = True
        self.state = "need_login"
        self.last_error = reason
        self._login_wait_ticks = 0

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
                self._mark_need_login("启动时未检测到登录态")
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
            # 不做采集和发送，但每隔几轮自己试一次登录态。
            # 人工点「重新检测」依然有效，这里只是不指望一定有人在。
            self._login_wait_ticks += 1
            if self._login_wait_ticks >= LOGIN_RECHECK_EVERY_TICKS:
                self._login_wait_ticks = 0
                ok, msg = await self.recheck_login()
                logger.info(f"自动重新检测登录态: {msg}")
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
            self._mark_need_login(str(e))
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
                note_id=c.note_id,
                comment_id=c.comment_id,
                note_xsec_token=c.note_xsec_token,
            )
            if msg_id:                       # None 表示指纹已存在，之前处理过
                fresh.append((msg_id, c))

        if not fresh:
            logger.debug("本轮无新评论")
            return

        logger.info(f"本轮新增 {len(fresh)} 条评论，开始生成草稿")

        # 先补齐笔记正文再生成 —— 没有笔记上下文的回复必然泛泛而谈或跑题
        await self._ensure_notes(fresh)

        generator = ReplyGenerator(cfg)
        for msg_id, c in fresh:
            await self._draft_one(generator, cfg, msg_id, c.author, c.content, c.note_id)

    async def _ensure_notes(self, fresh: list[tuple[int, RawComment]]) -> None:
        """
        把本轮涉及到的笔记正文抓下来存库。

        按 note_id 去重再抓：同一篇笔记下往往有多条评论，一篇只需开一次详情页。
        抓取失败不阻断生成流程，只是这条评论会退化成无上下文回复。
        """
        pending: dict[str, RawComment] = {}   # note_id -> 任一条引用它的评论
        for _, c in fresh:
            if c.note_id and c.note_id not in pending and db.note_needs_fetch(c.note_id):
                pending[c.note_id] = c

        if not pending:
            logger.debug("本轮涉及的笔记都已缓存，无需抓取")
            return

        logger.info(f"需要抓取 {len(pending)} 篇笔记的正文")
        for note_id, ref in pending.items():
            token = ref.note_xsec_token
            try:
                note = await self.browser.fetch_note(note_id, token)
            except NotLoggedIn as e:
                self._mark_need_login(str(e))
                logger.error(f"抓取笔记时登录态失效: {e}")
                return
            except Exception as e:
                # 退而用 mentions 接口顺带给的标题，好过完全没有上下文。
                # 仍记 fetch_error，以便之后重试拿完整正文。
                db.upsert_note(
                    note_id, title=ref.note_title, xsec_token=token, fetch_error=str(e)
                )
                logger.warning(
                    f"抓取笔记 {note_id} 失败: {e}"
                    + (f"（降级使用标题「{ref.note_title}」）" if ref.note_title else "")
                )
                continue

            db.upsert_note(
                note_id=note.note_id,
                title=note.title or ref.note_title,
                body=note.body,
                tags=" ".join(note.tags),
                note_type=note.note_type,
                xsec_token=token,
            )
            if note.is_empty:
                logger.warning(f"笔记 {note_id} 没有任何文字内容（可能是纯图片或视频笔记）")
            else:
                logger.info(f"笔记 {note_id} 内容已缓存: {(note.title or note.body)[:40]}")

        # 这里不必导航回通知页：collect 和 send_reply 都会自己先 goto 通知页

    async def _draft_one(
        self,
        generator: ReplyGenerator,
        cfg: settings_store.AppConfig,
        msg_id: int,
        author: str,
        content: str,
        note_id: str = "",
    ) -> None:
        provider, model = cfg.llm.provider, generator.client.model
        note = db.get_note(note_id) if note_id else None
        note_title = (note or {}).get("title", "")
        note_body = (note or {}).get("body", "")
        has_context = bool(note_title or note_body)

        try:
            draft = await generator.generate(
                author, content, note_title, msg_type="comment",
                note_body=note_body,
                note_tags=(note or {}).get("tags", ""),
            )
        except SensitiveHit as e:
            db.insert_reply(msg_id, "", db.STATUS_BLOCKED, provider, model)
            logger.warning(f"「{author}」的评论命中敏感词「{e.keyword}」，已转人工")
            return
        except SkipReply as e:
            rid = db.insert_reply(msg_id, "", db.STATUS_BLOCKED, provider, model)
            db.update_reply_status(rid, db.STATUS_BLOCKED, error="模型判断不宜回复")
            logger.info(f"「{author}」的评论不宜回复，已跳过: {e}")
            return
        except LLMError as e:
            rid = db.insert_reply(msg_id, "", db.STATUS_PENDING_REVIEW, provider, model)
            db.update_reply_status(rid, db.STATUS_PENDING_REVIEW, error=str(e))
            logger.error(f"生成回复失败（{author}）: {e}")
            return

        # 全自动模式直接进发送队列；审核模式进待审队列
        auto = cfg.reply.mode == "auto"
        if auto and not has_context and cfg.reply.require_note_context:
            # 没有笔记上下文的回复容易跑题，按配置转人工而不是直接发出去
            rid = db.insert_reply(msg_id, draft, db.STATUS_PENDING_REVIEW, provider, model)
            db.update_reply_status(
                rid, db.STATUS_PENDING_REVIEW, error="缺少笔记上下文，已转人工"
            )
            logger.warning(f"「{author}」的评论取不到笔记内容，草稿转人工审核")
            return

        status = db.STATUS_APPROVED if auto else db.STATUS_PENDING_REVIEW
        db.insert_reply(msg_id, draft, status, provider, model)
        logger.info(
            f"草稿已生成（{author}{'' if has_context else '，无笔记上下文'}）: {draft[:40]}"
        )

    async def regenerate(self, reply_id: int) -> tuple[bool, str]:
        """重新生成某条草稿，供控制台「重新生成」按钮调用"""
        row = db.get_reply(reply_id)
        if not row:
            return False, "记录不存在"
        # 重新生成会把状态改回待审。对已发送的记录做这件事等于让它重回队列，
        # 批准后就是对同一条评论回复第二次。
        if row["status"] in (db.STATUS_SENT, db.STATUS_APPROVED):
            return False, "这条已经发送或正在发送队列中，不能重新生成"

        cfg = settings_store.load()

        # 重新生成时补一次笔记抓取：上次可能是网络抖动导致没拿到上下文
        note_id = row.get("note_id") or ""
        if note_id and db.note_needs_fetch(note_id):
            async with self._lock:
                token = ((db.get_note(note_id) or {}).get("xsec_token")
                         or row.get("note_xsec_token") or "")
                try:
                    note = await self.browser.fetch_note(note_id, token)
                    db.upsert_note(
                        note_id=note.note_id, title=note.title, body=note.body,
                        tags=" ".join(note.tags), note_type=note.note_type,
                        xsec_token=token,
                    )
                    row = db.get_reply(reply_id) or row
                except Exception as e:
                    logger.warning(f"重新生成时抓取笔记 {note_id} 失败: {e}")

        generator = ReplyGenerator(cfg)
        try:
            draft = await generator.generate(
                row["author"], row["content"], row["note_title"], row["msg_type"],
                note_body=row.get("note_body", ""),
                note_tags=row.get("note_tags", ""),
            )
        except SensitiveHit as e:
            return False, f"命中敏感词「{e.keyword}」"
        except SkipReply:
            return False, "模型判断这条评论不宜回复"
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
                self._mark_need_login(str(e))
                logger.error(f"发送时发现登录态失效: {e}")
                return
            except ReplyTargetGone as e:
                # 评论已经不在了，重试没有意义，直接判失败继续处理下一条
                db.update_reply_status(row["id"], db.STATUS_FAILED, error=str(e))
                logger.warning(f"放弃回复 {row['author']}: {e}")
                continue
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
        """重新检测登录态。人工点「重新检测」和主循环的自动重试都走这里"""
        if not self.browser.is_open:
            try:
                await self.browser.open()
            except Exception as e:
                return False, f"浏览器启动失败: {e}"

        async with self._lock:
            ok = await self.browser.check_login()

        if ok:
            self.need_login = False
            self._login_wait_ticks = 0
            self.state = "running" if self.running else "stopped"
            self.last_error = ""
            self.wake()
            return True, "登录态正常"
        # 这里不能用 _mark_need_login：它会清零计数器，
        # 自动重试每轮都调用一次，清零后就再也攒不满、退化成永不重试。
        self.need_login = True
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
