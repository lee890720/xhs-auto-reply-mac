"""
X（推特）引擎。

跟小红书引擎并列、互不影响：各自启停、各自的限额和静默时段。X 走官方 API，
不碰浏览器，所以只开 X 时不会启动 Chromium。

每轮做四件事，前两件和小红书一样，后两件是 X 独有的：
  1. 拉取 @ 我的推文 → 生成草稿（审核模式进待审队列）
  2. 发送已批准的回复
  3. 每隔一段时间检查新关注者，放进「待回关」列表
  4. 执行人已经点过「回关」的那些关注

合规边界写死在代码里，不做成开关（依据 X 开发者指南）：
  - 只回复 @ 了我的推文，没有任何关键词搜索
  - 每条推文最多回一次（数据库指纹去重），每人每天有上限
  - 没有勾选「已获得 X 批准」时不允许全自动回复
  - 不会自动回关：新关注者只进列表，人点了「回关」才执行
"""
import asyncio
import random
import re
import time
from datetime import datetime, timedelta, timezone

from loguru import logger

from . import db, settings_store
from .generator import ReplyGenerator, SensitiveHit, SkipReply, strip_leading_mentions
from .llm import LLMError
from .scheduler import in_quiet_hours
from .x_api import (
    XAPIError, XAuthError, XClient, XForbidden, XNotFound, XPaymentRequired,
    XRateLimited, XUser, weighted_length, X_MAX_WEIGHT,
)

MAX_SEND_ATTEMPTS = 3
MAX_MENTION_PAGES = 3         # 一轮最多翻几页 @，再多说明积压太久，旧的直接放弃
MAX_FOLLOWER_PAGES = 20       # 一轮最多翻几页关注者
NO_CREDIT_PAUSE = 3600        # 额度用完后隔多久再试（秒）
STATE_KEY = "x_state"         # 运行时状态（自己的用户 ID、游标），不属于用户配置


def suspicious_flags(u: XUser, now: datetime | None = None) -> list[str]:
    """
    疑似机器人或营销号的特征，只用来在列表里提醒，不会据此自动做任何事。
    回关一个机器人号会拉低账号质量，所以值得让人看一眼。
    """
    flags = []
    if not u.profile_image_url or "default_profile_images" in u.profile_image_url:
        flags.append("默认头像")
    if u.tweet_count == 0:
        flags.append("没发过帖")
    if u.created_at:
        try:
            created = datetime.fromisoformat(u.created_at.replace("Z", "+00:00"))
            if (now or datetime.now(timezone.utc)) - created < timedelta(days=30):
                flags.append("注册不到 30 天")
        except ValueError:
            pass
    if u.following_count >= 1000 and u.following_count > 20 * max(1, u.followers_count):
        flags.append("关注数远多于粉丝")
    if re.search(r"\d{6,}$", u.username or ""):
        flags.append("用户名带长串数字")
    if not (u.description or "").strip():
        flags.append("无简介")
    return flags


def _local_time(iso: str) -> str:
    """X 返回 UTC 时间，转成本地时间方便在控制台看"""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


class XEngine:
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        # 测试时注入假的 X 服务
        self.transport = None

        self.running = False
        self.state = "stopped"   # stopped/starting/running/quiet/auth_error/no_credit/rate_limited/error
        self.last_error = ""
        self.last_poll_at = ""
        self.last_followers_poll_at = ""
        self._paused_until = 0.0
        self._next_mentions_at = 0.0
        self._next_followers_at = 0.0

    # ==================== 状态 ====================

    @staticmethod
    def load_state() -> dict:
        return db.get_setting(STATE_KEY, {}) or {}

    @staticmethod
    def save_state(**values) -> None:
        st = db.get_setting(STATE_KEY, {}) or {}
        st.update(values)
        db.set_setting(STATE_KEY, st)

    def client(self, cfg: settings_store.AppConfig) -> XClient:
        x = cfg.x
        return XClient(
            x.api_key, x.api_secret, x.access_token, x.access_token_secret,
            transport=self.transport,
        )

    def snapshot(self) -> dict:
        cfg = settings_store.load()
        st = self.load_state()
        followers = db.count_followers_by_status()
        return {
            "running": self.running,
            "state": self.state,
            "last_error": self.last_error,
            "last_poll_at": self.last_poll_at,
            "last_followers_poll_at": self.last_followers_poll_at,
            "paused_until": (
                datetime.fromtimestamp(self._paused_until).isoformat(timespec="seconds")
                if self._paused_until > time.time() else ""
            ),
            "account": st.get("username", ""),
            "configured": cfg.x.has_credentials,
            "mode": cfg.x.mode,
            "ai_reply_approved": cfg.x.ai_reply_approved,
            "quiet_now": in_quiet_hours(cfg.x),
            "today_sent": db.count_sent_today(db.PLATFORM_X),
            "daily_limit": cfg.x.daily_limit,
            "pending_review": db.count_by_status(db.STATUS_PENDING_REVIEW, db.PLATFORM_X),
            "approved_waiting": db.count_by_status(db.STATUS_APPROVED, db.PLATFORM_X),
            "followers_enabled": cfg.x.followers_enabled,
            "followers_pending": followers.get(db.FOLLOWER_PENDING, 0),
            "follow_queue": followers.get(db.FOLLOWER_APPROVED, 0),
            "followed_today": db.count_followed_today(),
            "follow_daily_limit": cfg.x.follow_daily_limit,
        }

    # ==================== 生命周期 ====================

    async def start(self) -> tuple[bool, str]:
        if self.running:
            return False, "X 引擎已在运行"
        if not settings_store.load().x.has_credentials:
            return False, "请先在「设置 → X 账号」里填好 4 个密钥"
        self.running = True
        self.state = "starting"
        self.last_error = ""
        self._paused_until = 0.0
        self._next_mentions_at = 0.0
        self._next_followers_at = 0.0
        self._task = asyncio.create_task(self._run(), name="x-engine")
        return True, "X 引擎已启动"

    async def stop(self) -> tuple[bool, str]:
        if not self.running and not self._task:
            return False, "X 引擎未在运行"
        self.running = False
        self._wake.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self.state = "stopped"
        logger.info("X 引擎已停止")
        return True, "X 引擎已停止"

    def wake(self) -> None:
        """审核通过或点了回关后调用，让队列马上开始执行"""
        self._wake.set()

    def check_followers_now(self) -> None:
        self._next_followers_at = 0.0
        self._wake.set()

    async def test(self) -> tuple[bool, str]:
        """验证密钥。只能验证读权限 —— 写权限要等第一次真正发送才知道"""
        cfg = settings_store.load()
        if not cfg.x.has_credentials:
            return False, "4 个密钥没有填全"
        try:
            me = await self.client(cfg).me()
        except XAPIError as e:
            return False, str(e)
        return True, f"连接成功 · @{me.username} · 粉丝 {me.followers_count}"

    # ==================== 主循环 ====================

    async def _run(self) -> None:
        while self.running:
            cfg = settings_store.load()
            try:
                await self._tick(cfg)
            except asyncio.CancelledError:
                raise
            except XAuthError as e:
                # 密钥失效重试也没用，停下来等人处理，免得每轮都打一次 401
                self.state = "auth_error"
                self.last_error = str(e)
                self.running = False
                logger.error(f"X 认证失败，引擎已停止: {e}")
                return
            except XPaymentRequired as e:
                self.state = "no_credit"
                self.last_error = str(e)
                self._paused_until = time.time() + NO_CREDIT_PAUSE
                logger.error(f"X API 额度用完，暂停 1 小时: {e}")
            except XRateLimited as e:
                self.state = "rate_limited"
                self.last_error = str(e)
                self._paused_until = e.reset_at
                logger.warning(
                    f"触发 X 频率限制，暂停到 {datetime.fromtimestamp(e.reset_at):%H:%M:%S}"
                )
            except Exception as e:
                self.state = "error"
                self.last_error = str(e)
                logger.exception(f"X 轮询异常: {e}")

            await self._sleep_interruptible(self._seconds_until_next(cfg))

    def _seconds_until_next(self, cfg: settings_store.AppConfig) -> float:
        now = time.time()
        if self._paused_until > now:
            return self._paused_until - now
        due = [self._next_mentions_at]
        if cfg.x.followers_enabled:
            due.append(self._next_followers_at)
        return max(5.0, min(due) - now)

    async def _sleep_interruptible(self, seconds: float) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _tick(self, cfg: settings_store.AppConfig) -> None:
        if self._paused_until > time.time():
            return
        if in_quiet_hours(cfg.x):
            self.state = "quiet"
            # 每分钟看一次是否已出静默时段，结束后立刻恢复，不要再多睡一个轮询周期。
            # 两个时间点都要推后，否则过期的那个会让主循环每 5 秒空转一次
            soon = time.time() + 60
            self._next_mentions_at = max(self._next_mentions_at, soon)
            self._next_followers_at = max(self._next_followers_at, soon)
            return

        self.state = "running"
        self.last_error = ""
        client = self.client(cfg)
        uid = await self._ensure_me(client)
        now = time.time()

        # 被「批准」或「回关」唤醒时只处理队列，不额外拉一次 @ —— 每次读取都要计费
        if now >= self._next_mentions_at:
            self._next_mentions_at = now + cfg.x.poll_interval
            await self._collect_and_draft(cfg, client, uid)
            self.last_poll_at = db.now()

        await self._drain_send_queue(cfg, client)

        if cfg.x.followers_enabled and now >= self._next_followers_at:
            self._next_followers_at = now + cfg.x.followers_poll_interval
            await self._poll_followers(cfg, client, uid)
            self.last_followers_poll_at = db.now()

        await self._drain_follow_queue(cfg, client, uid)

    async def _ensure_me(self, client: XClient) -> str:
        """拿到自己的用户 ID。换了账号就把游标清掉，免得拿旧账号的进度套新账号"""
        st = self.load_state()
        if st.get("user_id") and st.get("key_fingerprint") == client.access_token[:12]:
            return st["user_id"]
        me = await client.me()
        if st.get("user_id") and st.get("user_id") != me.id:
            logger.warning(f"X 账号变了（现在是 @{me.username}），重新记录起点")
            db.set_setting(STATE_KEY, {})
        self.save_state(
            user_id=me.id, username=me.username, key_fingerprint=client.access_token[:12]
        )
        logger.info(f"X 账号已连接: @{me.username}")
        return me.id

    # ==================== @ 回复 ====================

    async def _collect_and_draft(
        self, cfg: settings_store.AppConfig, client: XClient, uid: str
    ) -> None:
        st = self.load_state()

        if not st.get("mentions_baselined"):
            # 第一次运行只记起点。不然会把几个月前的 @ 全翻出来回一遍
            page = await client.mentions(uid, max_results=5)
            self.save_state(mentions_baselined=True, since_id=page.newest_id or "")
            logger.info("X 首次运行：已记录 @ 的起点，之前的 @ 不会补回复")
            return

        since_id = st.get("since_id", "")
        page = await client.mentions(uid, since_id=since_id, max_results=cfg.x.max_per_poll)
        newest = page.newest_id
        mentions = list(page.mentions)
        pages = 1
        while page.next_token and pages < MAX_MENTION_PAGES:
            page = await client.mentions(
                uid, since_id=since_id, max_results=cfg.x.max_per_poll,
                pagination_token=page.next_token,
            )
            mentions += page.mentions
            pages += 1
        if page.next_token:
            logger.warning(f"新的 @ 太多，本轮只处理最近 {len(mentions)} 条，更早的跳过")

        # 游标先推进再处理：万一处理过程中出错，也不会下一轮把同一批重新拉一遍
        # （重复拉取要计费；漏处理的那几条已经入库，不会丢）
        if newest:
            self.save_state(since_id=newest)

        if not mentions:
            logger.debug("X 本轮没有新的 @")
            return

        generator = ReplyGenerator(cfg)
        fresh = 0
        for m in reversed(mentions):              # 从旧到新处理，回复顺序跟时间一致
            if m.author_id == uid or m.is_retweet:
                continue
            ctx = m.context
            note_id = f"x:{ctx.id}" if ctx else ""
            if ctx and ctx.text:
                mine = ctx.author_id == uid
                db.upsert_note(
                    note_id,
                    title="" if mine else (f"@{ctx.author_username}" if ctx.author_username else ""),
                    body=ctx.text,
                    note_type=f"{ctx.relation}:{'own' if mine else 'other'}",
                )

            content = strip_leading_mentions(m.text)
            msg_id = db.insert_message(
                fingerprint=f"x:{m.id}",
                msg_type="mention",
                author=f"@{m.author_username}" if m.author_username else (m.author_name or m.author_id),
                content=content or m.text,
                raw_time=_local_time(m.created_at),
                note_id=note_id,
                platform=db.PLATFORM_X,
                external_id=m.id,
                parent_id=ctx.id if ctx else "",
                author_id=m.author_id,
            )
            if not msg_id:
                continue                          # 处理过了
            fresh += 1
            await self._draft_one(generator, cfg, msg_id, m, content)

        if fresh:
            logger.info(f"X 本轮新增 {fresh} 条 @")

    def _block(self, msg_id: int, reason: str, provider: str = "", model: str = "") -> None:
        rid = db.insert_reply(msg_id, "", db.STATUS_BLOCKED, provider, model)
        db.update_reply_status(rid, db.STATUS_BLOCKED, error=reason)

    async def _draft_one(self, generator, cfg, msg_id: int, m, content: str) -> None:
        provider, model = cfg.llm.provider, generator.client.model
        who = f"@{m.author_username}" if m.author_username else m.author_id

        # 硬规则：先于模型执行，命中就不花模型的钱
        others = [u for u in m.mentioned_usernames if u.lower() != self.load_state().get("username", "").lower()]
        if len(others) + 1 > cfg.x.max_mentions_in_tweet:
            self._block(msg_id, f"这条推文 @ 了 {len(others) + 1} 个账号，疑似群发，不自动回复")
            logger.info(f"{who} 的推文 @ 了太多人，跳过")
            return
        if not content:
            self._block(msg_id, "推文里只有 @，没有内容")
            return
        n = db.count_replies_to_author_today(db.PLATFORM_X, m.author_id)
        if n >= cfg.x.per_user_daily_limit:
            self._block(msg_id, f"今天已经给这个人回了 {n} 条，达到每人每日上限")
            logger.info(f"{who} 今天已回复 {n} 条，跳过")
            return

        ctx = m.context
        try:
            draft = await generator.generate_x(
                content,
                context_text=ctx.text if ctx else "",
                context_author=ctx.author_username if ctx else "",
                context_is_mine=bool(ctx and ctx.author_id == self.load_state().get("user_id")),
                relation=ctx.relation if ctx else "",
            )
        except SensitiveHit as e:
            self._block(msg_id, f"命中敏感词「{e.keyword}」", provider, model)
            logger.warning(f"{who} 的推文命中敏感词「{e.keyword}」，已转人工")
            return
        except SkipReply as e:
            self._block(msg_id, "模型判断不宜回复", provider, model)
            logger.info(f"{who} 的推文不宜回复，已跳过: {e}")
            return
        except LLMError as e:
            rid = db.insert_reply(msg_id, "", db.STATUS_PENDING_REVIEW, provider, model)
            db.update_reply_status(rid, db.STATUS_PENDING_REVIEW, error=str(e))
            logger.error(f"生成 X 回复失败（{who}）: {e}")
            return

        # 全自动必须同时满足两个条件；配置校验已经拦过一次，这里再兜一道
        auto = cfg.x.mode == "auto" and cfg.x.ai_reply_approved
        status = db.STATUS_APPROVED if auto else db.STATUS_PENDING_REVIEW
        db.insert_reply(msg_id, draft, status, provider, model)
        logger.info(f"X 草稿已生成（{who}）: {draft[:40]}")

    async def regenerate(self, reply_id: int) -> tuple[bool, str]:
        row = db.get_reply(reply_id)
        if not row:
            return False, "记录不存在"
        if row["status"] in (db.STATUS_SENT, db.STATUS_APPROVED):
            return False, "这条已经发送或正在发送队列中，不能重新生成"

        relation, _, owner = (row.get("note_type") or "").partition(":")
        if not relation and row.get("parent_id"):
            relation = "replied_to"
        generator = ReplyGenerator(settings_store.load())
        try:
            draft = await generator.generate_x(
                row["content"],
                context_text=row.get("note_body", ""),
                context_author=(row.get("note_title") or "").lstrip("@"),
                context_is_mine=owner == "own",
                relation=relation,
            )
        except SensitiveHit as e:
            return False, f"命中敏感词「{e.keyword}」"
        except SkipReply:
            return False, "模型判断这条推文不宜回复"
        except LLMError as e:
            return False, str(e)

        db.update_reply_status(reply_id, db.STATUS_PENDING_REVIEW, final_text=draft, error="")
        db.execute("UPDATE replies SET draft = ? WHERE id = ?", (draft, reply_id))
        return True, draft

    async def _drain_send_queue(self, cfg: settings_store.AppConfig, client: XClient) -> None:
        while self.running:
            sent_today = db.count_sent_today(db.PLATFORM_X)
            if sent_today >= cfg.x.daily_limit:
                logger.warning(f"X 今日已回复 {sent_today} 条，达到上限 {cfg.x.daily_limit}")
                return

            row = db.next_approved_reply(db.PLATFORM_X)
            if not row:
                return

            text = (row["final_text"] or row["draft"] or "").strip()
            if not text:
                db.update_reply_status(row["id"], db.STATUS_FAILED, error="回复内容为空")
                continue
            if weighted_length(text) > X_MAX_WEIGHT:
                db.update_reply_status(
                    row["id"], db.STATUS_FAILED,
                    error=f"超过 X 的 {X_MAX_WEIGHT} 字符上限（中文一个字算 2），请改短后重新排队",
                )
                continue
            if not row.get("external_id"):
                db.update_reply_status(row["id"], db.STATUS_FAILED, error="缺少推文 ID")
                continue

            try:
                new_id = await client.reply(row["external_id"], text)
            except (XForbidden, XNotFound) as e:
                # 对方删了推文、限制了回复、屏蔽了你，或不满足 X 对 API 回复的限制。
                # 重试多少次结果都一样
                db.bump_attempts(row["id"])
                db.update_reply_status(row["id"], db.STATUS_FAILED, error=str(e))
                logger.warning(f"放弃回复 {row['author']}: {e}")
                continue
            except (XAuthError, XPaymentRequired, XRateLimited):
                raise                             # 交给主循环处理，这条留在队列里
            except XAPIError as e:
                db.bump_attempts(row["id"])
                attempts = (row["attempts"] or 0) + 1
                if attempts >= MAX_SEND_ATTEMPTS:
                    db.update_reply_status(row["id"], db.STATUS_FAILED, error=str(e))
                    logger.error(f"回复 {row['author']} 失败（已重试 {attempts} 次）: {e}")
                else:
                    db.update_reply_status(row["id"], db.STATUS_APPROVED, error=str(e))
                    logger.warning(f"回复 {row['author']} 失败，稍后重试: {e}")
                    return
                continue

            db.bump_attempts(row["id"])
            db.update_reply_status(row["id"], db.STATUS_SENT, mark_sent=True, error="")
            logger.info(f"已回复 X 用户 {row['author']}（新推文 {new_id}）")

            delay = random.uniform(cfg.x.min_delay, cfg.x.max_delay)
            await asyncio.sleep(delay)

    # ==================== 关注者 ====================

    async def _poll_followers(
        self, cfg: settings_store.AppConfig, client: XClient, uid: str
    ) -> None:
        """
        找出新关注者，放进「待回关」列表。这里只记录，绝不关注任何人。

        依赖关注者接口「最近关注的排在最前」：从头往后看，碰到见过的人就停。
        这样平时每次只读几个人，按返回人数计费时成本很低。
        """
        size = cfg.x.followers_page_size
        page = await client.followers(uid, max_results=size)

        if not self.load_state().get("followers_baselined"):
            # 第一次只把最前面几个记成「老关注者」作为分界线，不进待回关列表。
            # 不需要把几千个老关注者全读一遍：之后碰到分界线上的人就会停下。
            for u in page.users:
                db.insert_follower(u.id, db.FOLLOWER_BASELINE, **_profile(u))
            self.save_state(followers_baselined=True)
            logger.info("X 首次运行：已记录关注者的起点，之后新增的关注者才会进入待回关列表")
            return

        new: list[XUser] = []
        pages = 1
        while True:
            hit_known = False
            for u in page.users:
                if db.follower_exists(u.id):
                    hit_known = True
                    break
                new.append(u)
            if hit_known or not page.next_token or pages >= MAX_FOLLOWER_PAGES:
                break
            page = await client.followers(uid, max_results=size, pagination_token=page.next_token)
            pages += 1

        if not hit_known and page.next_token:
            logger.warning(f"新关注者太多，本轮只记录了最近 {len(new)} 个")

        added = 0
        for u in reversed(new):
            if "following" in u.connection_status:
                status = db.FOLLOWER_FOLLOWED          # 本来就关注着，不用再处理
            else:
                status = db.FOLLOWER_PENDING
            if db.insert_follower(u.id, status, flags=",".join(suspicious_flags(u)), **_profile(u)):
                added += status == db.FOLLOWER_PENDING
        if added:
            logger.info(f"发现 {added} 个新关注者，等你在控制台决定是否回关")

    async def _drain_follow_queue(
        self, cfg: settings_store.AppConfig, client: XClient, uid: str
    ) -> None:
        """执行人已经点了「回关」的关注。每个都是人逐个决定的，这里只负责按节奏执行"""
        while self.running:
            done = db.count_followed_today()
            if done >= cfg.x.follow_daily_limit:
                logger.warning(f"今日已回关 {done} 人，达到上限 {cfg.x.follow_daily_limit}，剩下的明天继续")
                return

            f = db.next_approved_follow()
            if not f:
                return

            who = f"@{f['username']}" if f["username"] else f["user_id"]
            try:
                result = await client.follow(uid, f["user_id"])
            except (XForbidden, XNotFound) as e:
                db.bump_follow_attempts(f["user_id"])
                db.update_follower_status(f["user_id"], db.FOLLOWER_FAILED, error=str(e), mark_acted=True)
                logger.warning(f"回关 {who} 失败: {e}")
                continue
            except (XAuthError, XPaymentRequired, XRateLimited):
                raise
            except XAPIError as e:
                db.bump_follow_attempts(f["user_id"])
                if (f["attempts"] or 0) + 1 >= MAX_SEND_ATTEMPTS:
                    db.update_follower_status(f["user_id"], db.FOLLOWER_FAILED, error=str(e), mark_acted=True)
                    logger.error(f"回关 {who} 失败（已重试 {MAX_SEND_ATTEMPTS} 次）: {e}")
                else:
                    db.update_follower_status(f["user_id"], db.FOLLOWER_APPROVED, error=str(e))
                    logger.warning(f"回关 {who} 失败，稍后重试: {e}")
                    return
                continue

            db.bump_follow_attempts(f["user_id"])
            note = "对方是受保护账号，已发送关注请求，等对方同意" if result == "pending" else ""
            db.update_follower_status(f["user_id"], db.FOLLOWER_FOLLOWED, error=note, mark_acted=True)
            logger.info(f"已回关 {who}")

            await asyncio.sleep(random.uniform(cfg.x.follow_min_delay, cfg.x.follow_max_delay))


def _profile(u: XUser) -> dict:
    return {
        "username": u.username,
        "name": u.name,
        "description": u.description,
        "profile_image_url": u.profile_image_url,
        "followers_count": u.followers_count,
        "following_count": u.following_count,
        "tweet_count": u.tweet_count,
        "account_created_at": u.created_at,
        "protected": u.protected,
    }
