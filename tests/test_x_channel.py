"""
X 渠道的端到端测试：用一个假的 X 服务替代真实 API，模型调用也打桩。

    .venv/bin/pip install pytest
    .venv/bin/python -m pytest tests -q
"""
import asyncio
import json
import re
import sqlite3
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from xhsd import db, settings_store, x_engine as x_engine_mod
from xhsd.llm import client as llm_client
from xhsd.paths import DB_PATH
from xhsd.x_api import XClient, XRateLimited, oauth1_header, truncate_weighted, weighted_length
from xhsd.x_engine import XEngine, suspicious_flags

ME = {"id": "1000", "username": "me_account", "name": "Me"}


# ==================== 假的 X 服务 ====================

class FakeX:
    def __init__(self):
        self.mentions: list[dict] = []        # 从新到旧
        self.users: dict[str, dict] = {}
        self.tweets: dict[str, dict] = {}     # 可被引用的推文
        self.followers: list[dict] = []       # 最近关注的在前
        self.posted: list[dict] = []
        self.followed: list[str] = []
        self.requests: list[str] = []
        self.reply_status = 201
        self.mentions_status = 200

    def add_user(self, uid, username):
        self.users[uid] = {"id": uid, "username": username, "name": username}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(f"{request.method} {path}")
        assert request.headers["Authorization"].startswith("OAuth "), "缺少 OAuth 签名"

        if path == "/2/users/me":
            return httpx.Response(200, json={"data": ME})

        if path == f"/2/users/{ME['id']}/mentions":
            if self.mentions_status == 429:
                return httpx.Response(
                    429, json={"title": "Too Many Requests"},
                    headers={"x-rate-limit-reset": str(int(time.time()) + 600)},
                )
            since = request.url.params.get("since_id")
            items = [m for m in self.mentions if not since or int(m["id"]) > int(since)]
            items = items[: int(request.url.params.get("max_results", 20))]
            if not items:
                return httpx.Response(200, json={"meta": {"result_count": 0}})
            refs = {r["id"] for m in items for r in m.get("referenced_tweets", [])}
            inc_tweets = [self.tweets[i] for i in refs if i in self.tweets]
            uids = {m["author_id"] for m in items} | {t["author_id"] for t in inc_tweets}
            return httpx.Response(200, json={
                "data": items,
                "includes": {
                    "users": [self.users[u] for u in uids if u in self.users],
                    "tweets": inc_tweets,
                },
                "meta": {"newest_id": items[0]["id"], "result_count": len(items)},
            })

        if path == "/2/tweets" and request.method == "POST":
            body = json.loads(request.content)
            if self.reply_status != 201:
                return httpx.Response(self.reply_status, json={
                    "title": "Forbidden",
                    "detail": "You are not permitted to reply to this conversation.",
                })
            self.posted.append(body)
            return httpx.Response(201, json={"data": {"id": str(9000 + len(self.posted))}})

        if path == f"/2/users/{ME['id']}/followers":
            size = int(request.url.params.get("max_results", 5))
            start = int(request.url.params.get("pagination_token") or 0)
            page = self.followers[start:start + size]
            meta = {"result_count": len(page)}
            if start + size < len(self.followers):
                meta["next_token"] = str(start + size)
            return httpx.Response(200, json={"data": page, "meta": meta})

        if path == f"/2/users/{ME['id']}/following" and request.method == "POST":
            target = json.loads(request.content)["target_user_id"]
            self.followed.append(target)
            return httpx.Response(200, json={"data": {"following": True, "pending_follow": False}})

        return httpx.Response(404, json={"title": "Not Found", "detail": path})


def mention(tid, author_id, text, replied_to=None, mentions=("me_account",)):
    m = {
        "id": tid, "author_id": author_id, "text": text,
        "created_at": "2026-11-09T04:00:00.000Z", "conversation_id": tid,
        "entities": {"mentions": [{"username": u} for u in mentions]},
    }
    if replied_to:
        m["referenced_tweets"] = [{"type": "replied_to", "id": replied_to}]
    return m


def follower(uid, username, **kw):
    return {
        "id": uid, "username": username, "name": username,
        "description": kw.get("description", "做设计的"),
        "profile_image_url": kw.get("image", "https://pbs.twimg.com/profile_images/1/a.jpg"),
        "created_at": kw.get("created_at", "2015-01-01T00:00:00.000Z"),
        "public_metrics": {
            "followers_count": kw.get("followers", 200),
            "following_count": kw.get("following", 180),
            "tweet_count": kw.get("tweets", 500),
        },
        "connection_status": kw.get("connection", ["followed_by"]),
    }


@pytest.fixture
def fake_x():
    return FakeX()


@pytest.fixture
def engine(fake_x, monkeypatch):
    settings_store.save_section("x", {
        "api_key": "ck", "api_secret": "cs",
        "access_token": "1000-token", "access_token_secret": "ts",
        "quiet_enabled": False,
    })
    settings_store.save_section("llm", {"api_key": "sk-test", "model": "fake-model"})

    async def fake_complete(self, system_prompt, user_prompt):
        # 人设里写了的弃答规则，这里用最简单的方式模拟
        if "你是不是机器人" in user_prompt:
            return "[SKIP]"
        assert "X（推特）账号" in system_prompt, "X 必须用自己的人设"
        return "可以，私信我细说"

    monkeypatch.setattr(llm_client.ChatClient, "complete", fake_complete)
    monkeypatch.setattr(x_engine_mod.random, "uniform", lambda a, b: 0)

    eng = XEngine()
    eng.transport = httpx.MockTransport(fake_x.handler)
    eng.running = True
    return eng


def tick(eng: XEngine, force_mentions=True, force_followers=True):
    if force_mentions:
        eng._next_mentions_at = 0
    if force_followers:
        eng._next_followers_at = 0
    asyncio.run(eng._tick(settings_store.load()))


def replies_by_tweet() -> dict[str, dict]:
    return {r["external_id"]: r for r in db.list_replies(platform=db.PLATFORM_X, limit=200)}


# ==================== 基础工具 ====================

def test_oauth_signature_matches_x_doc_example():
    """X 官方文档「Creating a signature」里的示例"""
    h = oauth1_header(
        "POST", "https://api.twitter.com/1.1/statuses/update.json",
        {"include_entities": "true", "status": "Hello Ladies + Gentlemen, a signed OAuth request!"},
        "xvz1evFS4wEEPTGEFPHBog", "kAcSOqF21Fu85e7zjz7ZN2U4ZRhfV3WpwPAoE3Z7kBw",
        "370773112-GmHxMAgYyLbNEtIKZeRNFsMKPR9EyMZeS9weJAEb",
        "LswwdoUaIvS8ltyTt5jkRh4J50vUPVVHtR2YPi5kE",
        nonce="kYjzVBB8Y0ZFabxSWbWovY3uYSQ2pTgmZeNu2VS4cg", timestamp=1318622958,
    )
    assert 'oauth_signature="hCtSmYh%2BiHYCEqBWrE7C7hYmtUk%3D"' in h


def test_weighted_length():
    assert weighted_length("hello") == 5
    assert weighted_length("你好") == 4
    assert len(truncate_weighted("中" * 200)) == 140
    assert truncate_weighted("short") == "short"


def test_suspicious_flags():
    from xhsd.x_api import XUser
    bot = XUser(id="1", username="user84736251", profile_image_url="https://x/default_profile_images/a.png",
                tweet_count=0, following_count=4000, followers_count=3,
                created_at="2026-11-01T00:00:00.000Z")
    flags = suspicious_flags(bot)
    for f in ("默认头像", "没发过帖", "注册不到 30 天", "关注数远多于粉丝", "用户名带长串数字", "无简介"):
        assert f in flags
    normal = XUser(id="2", username="designer", profile_image_url="https://x/p.jpg",
                   tweet_count=300, following_count=200, followers_count=180,
                   description="做设计的", created_at="2015-01-01T00:00:00.000Z")
    assert suspicious_flags(normal) == []


# ==================== 配置 ====================

def test_auto_mode_requires_x_approval():
    with pytest.raises(Exception, match="批准"):
        settings_store.save_section("x", {"mode": "auto"})
    settings_store.save_section("x", {"mode": "auto", "ai_reply_approved": True})
    assert settings_store.load().x.mode == "auto"


def test_x_defaults_are_off_and_review():
    cfg = settings_store.load()
    assert cfg.x.mode == "review"
    assert not cfg.x.ai_reply_approved
    assert not db.get_setting("autostart", False)       # 小红书默认不自动运行
    assert not db.get_setting("x_autostart", False)     # X 默认不自动运行


# ==================== 数据库迁移 ====================

def test_migration_keeps_old_xhs_data():
    """已经在跑的小红书库升级后，历史消息归为小红书，指纹不变"""
    for suffix in ("", "-wal", "-shm"):
        p = type(DB_PATH)(str(DB_PATH) + suffix)
        if p.exists():
            p.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL UNIQUE,
            msg_type TEXT NOT NULL DEFAULT 'comment', author TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '', note_title TEXT NOT NULL DEFAULT '',
            note_image_url TEXT NOT NULL DEFAULT '', raw_time TEXT NOT NULL DEFAULT '',
            collected_at TEXT NOT NULL, note_id TEXT NOT NULL DEFAULT '',
            comment_id TEXT NOT NULL DEFAULT '', note_xsec_token TEXT NOT NULL DEFAULT '');
        CREATE TABLE replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            draft TEXT NOT NULL DEFAULT '', final_text TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL, provider TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, reviewed_at TEXT NOT NULL DEFAULT '', sent_at TEXT NOT NULL DEFAULT '');
        INSERT INTO messages (fingerprint, author, content, collected_at)
            VALUES ('abc123', '小红薯', '约起来', '2026-08-01T10:00:00');
        INSERT INTO replies (message_id, draft, status, created_at)
            VALUES (1, '可以，私信我', 'approved', '2026-08-01T10:00:00');
    """)
    conn.commit()
    conn.close()

    db.init_db()
    row = db.get_reply(1)
    assert row["platform"] == "xhs"
    assert db.next_approved_reply(db.PLATFORM_XHS)["id"] == 1
    assert db.next_approved_reply(db.PLATFORM_X) is None
    assert db.insert_message("abc123", "comment", "小红薯", "约起来") is None   # 去重仍然有效


# ==================== @ 回复 ====================

def test_first_run_only_records_baseline(engine, fake_x):
    fake_x.add_user("2001", "alice")
    fake_x.mentions = [mention("100", "2001", "@me_account 很久以前的问题")]
    tick(engine)
    assert db.list_replies(platform=db.PLATFORM_X) == []
    assert engine.load_state()["since_id"] == "100"


def test_mentions_are_drafted_and_sent_after_approval(engine, fake_x):
    fake_x.add_user("2001", "alice")
    fake_x.add_user("2002", "bob")
    fake_x.add_user("2003", "spammer")
    fake_x.mentions = [mention("100", "2001", "@me_account 老的")]
    tick(engine)                                          # 记起点

    fake_x.tweets["50"] = {"id": "50", "author_id": ME["id"], "text": "周末组织一次城市徒步"}
    fake_x.mentions = [
        mention("105", "2002", "@me_account 你是不是机器人"),
        mention("104", "2003", "@me_account @a @b @c @d @e @f 关注送币",
                mentions=("me_account", "a", "b", "c", "d", "e", "f")),
        mention("103", ME["id"], "@alice 我自己发的"),
        mention("102", "2001", "@me_account"),
        mention("101", "2001", "@me_account 我想参加，怎么报名？", replied_to="50"),
    ] + fake_x.mentions
    tick(engine)

    r = replies_by_tweet()
    assert "103" not in r                                  # 自己的推文不处理
    assert r["101"]["status"] == db.STATUS_PENDING_REVIEW  # 默认审核模式
    assert r["101"]["draft"] == "可以，私信我细说"
    assert r["101"]["content"] == "我想参加，怎么报名？"   # 开头的 @ 去掉了
    assert r["101"]["note_body"] == "周末组织一次城市徒步"
    assert r["101"]["note_type"] == "replied_to:own"
    assert r["101"]["author"] == "@alice"
    assert r["102"]["status"] == db.STATUS_BLOCKED          # 只有 @
    assert "群发" in r["104"]["error"]                      # @ 了 7 个人
    assert r["105"]["status"] == db.STATUS_BLOCKED          # 模型弃答
    assert fake_x.posted == []                             # 没批准之前什么都不发
    assert engine.load_state()["since_id"] == "105"

    db.update_reply_status(r["101"]["id"], db.STATUS_APPROVED, mark_reviewed=True)
    tick(engine, force_mentions=False)
    assert fake_x.posted == [
        {"text": "可以，私信我细说", "reply": {"in_reply_to_tweet_id": "101"}}
    ]
    assert replies_by_tweet()["101"]["status"] == db.STATUS_SENT

    # 同一批 @ 再拉一次也不会重复处理
    tick(engine)
    assert len(fake_x.posted) == 1


def test_per_user_daily_limit(engine, fake_x):
    fake_x.add_user("2001", "alice")
    tick(engine)                                           # 没有任何 @ 时也会记起点
    fake_x.mentions = [mention(str(200 + i), "2001", f"@me_account 第 {i} 个问题") for i in range(5)][::-1]
    tick(engine)
    statuses = [r["status"] for r in sorted(replies_by_tweet().values(), key=lambda r: r["external_id"])]
    assert statuses.count(db.STATUS_PENDING_REVIEW) == 3   # 默认每人每天 3 条
    assert statuses.count(db.STATUS_BLOCKED) == 2


def test_auto_mode_sends_without_review(engine, fake_x):
    settings_store.save_section("x", {"mode": "auto", "ai_reply_approved": True})
    fake_x.add_user("2001", "alice")
    tick(engine)
    fake_x.mentions = [mention("300", "2001", "@me_account 在吗")]
    tick(engine)
    assert replies_by_tweet()["300"]["status"] == db.STATUS_SENT
    assert len(fake_x.posted) == 1


def test_reply_forbidden_fails_without_retry(engine, fake_x):
    fake_x.add_user("2001", "alice")
    tick(engine)
    fake_x.mentions = [mention("400", "2001", "@me_account 问个事")]
    tick(engine)
    rid = replies_by_tweet()["400"]["id"]
    db.update_reply_status(rid, db.STATUS_APPROVED)
    fake_x.reply_status = 403
    tick(engine, force_mentions=False)
    row = db.get_reply(rid)
    assert row["status"] == db.STATUS_FAILED
    assert "403" in row["error"]


def test_rate_limit_is_reported(engine, fake_x):
    tick(engine)
    fake_x.mentions_status = 429
    with pytest.raises(XRateLimited) as e:
        tick(engine)
    assert e.value.reset_at > time.time()


def test_xhs_and_x_queues_are_separate(engine, fake_x):
    xhs_id = db.insert_message("fp-xhs", "comment", "小红薯", "约起来")
    db.insert_reply(xhs_id, "好的", db.STATUS_APPROVED)
    tick(engine)                                           # X 引擎不能碰小红书的队列
    assert db.next_approved_reply(db.PLATFORM_XHS) is not None
    assert fake_x.posted == []


# ==================== 关注者 ====================

def test_followers_are_never_followed_automatically(engine, fake_x):
    fake_x.followers = [follower(f"u{i}", f"old{i}") for i in range(8)]
    tick(engine)                                           # 第一次只记分界线
    assert db.count_followers_by_status() == {db.FOLLOWER_BASELINE: 5}
    assert fake_x.followed == []

    fake_x.followers = [
        follower("n1", "newbie"),
        follower("n2", "user84736251", image="https://x/default_profile_images/x.png",
                 tweets=0, following=5000, followers=2, description=""),
        follower("n3", "friend", connection=["following", "followed_by"]),
    ] + fake_x.followers
    tick(engine)

    pending = {f["user_id"]: f for f in db.list_followers(db.FOLLOWER_PENDING)}
    assert set(pending) == {"n1", "n2"}
    assert "默认头像" in pending["n2"]["flags"]
    assert pending["n1"]["flags"] == ""
    assert db.get_follower("n3")["status"] == db.FOLLOWER_FOLLOWED    # 本来就关注着
    assert fake_x.followed == []                                       # 发现了也不会自动关注

    db.update_follower_status("n1", db.FOLLOWER_APPROVED, mark_acted=True)
    tick(engine, force_mentions=False, force_followers=False)
    assert fake_x.followed == ["n1"]                                   # 只关注人点过的那个
    assert db.get_follower("n1")["status"] == db.FOLLOWER_FOLLOWED
    assert db.get_follower("n2")["status"] == db.FOLLOWER_PENDING
    assert db.count_followed_today() == 1


def test_follow_daily_limit(engine, fake_x):
    settings_store.save_section("x", {"follow_daily_limit": 2})
    tick(engine)
    fake_x.followers = [follower(f"n{i}", f"new{i}") for i in range(4)]
    tick(engine)
    for f in db.list_followers(db.FOLLOWER_PENDING):
        db.update_follower_status(f["user_id"], db.FOLLOWER_APPROVED, mark_acted=True)
    tick(engine, force_mentions=False, force_followers=False)
    assert len(fake_x.followed) == 2
    assert db.count_followers_by_status()[db.FOLLOWER_APPROVED] == 2


# ==================== 控制台接口 ====================

def test_api_masks_secrets_and_keeps_them_on_save(engine):
    from xhsd import api
    with TestClient(api.app) as c:
        cfg = c.get("/api/config").json()
        assert cfg["config"]["x"]["access_token_secret"] != "ts"
        assert cfg["config"]["x"]["has_credentials"] is True
        assert cfg["x_autostart"] is False
        # 前端把掩码原样传回来，不能把密钥覆盖成掩码
        values = {k: cfg["config"]["x"][k] for k in ("api_key", "api_secret", "access_token", "access_token_secret")}
        values["daily_limit"] = 20
        assert c.put("/api/config/x", json={"values": values}).status_code == 200
    x = settings_store.load().x
    assert (x.api_key, x.access_token_secret, x.daily_limit) == ("ck", "ts", 20)


def test_api_status_has_both_platforms_and_nothing_running():
    from xhsd import api
    with TestClient(api.app) as c:
        s = c.get("/api/status").json()
    assert s["running"] is False and s["x"]["running"] is False
    assert s["x"]["configured"] is False


def test_api_rejects_too_long_x_reply_and_has_no_follow_all(engine, fake_x):
    from xhsd import api
    fake_x.add_user("2001", "alice")
    tick(engine)
    fake_x.mentions = [mention("500", "2001", "@me_account 你好")]
    tick(engine)
    rid = replies_by_tweet()["500"]["id"]
    with TestClient(api.app) as c:
        r = c.post(f"/api/replies/{rid}/approve", json={"text": "中" * 141})
        assert r.status_code == 400 and "280" in r.json()["detail"]
        assert c.post(f"/api/replies/{rid}/approve", json={"text": "中" * 140}).status_code == 200
        routes = {getattr(rt, "path", "") for rt in api.app.routes}
        assert not any(re.search(r"follow.*all|all.*follow", p) for p in routes)


def test_api_validation_errors_are_readable(engine):
    from xhsd import api
    with TestClient(api.app) as c:
        r = c.put("/api/config/x", json={"values": {"mode": "auto"}})
        assert r.status_code == 422 and "批准" in r.json()["detail"]
        r = c.put("/api/config/x", json={"values": {"poll_interval": 10}})
        assert r.json()["detail"] == "配置校验失败：poll_interval 不能小于 60"
    assert settings_store.load().x.mode == "review"


def test_engine_loop_starts_and_stops_cleanly(engine, fake_x):
    """真正跑一下主循环：启动、拉取、停止，过程中不能抛异常"""
    async def run():
        eng = XEngine()
        eng.transport = httpx.MockTransport(fake_x.handler)
        ok, _ = await eng.start()
        assert ok
        for _ in range(50):
            await asyncio.sleep(0.02)
            if any("followers" in r for r in fake_x.requests):
                break
        snap = eng.snapshot()
        ok, _ = await eng.stop()
        return ok, snap

    ok, snap = asyncio.run(run())
    assert ok
    assert snap["state"] == "running" and snap["account"] == "me_account"
    assert "GET /2/users/me" in fake_x.requests
    assert any("mentions" in r for r in fake_x.requests)


def test_x_engine_refuses_to_start_without_keys():
    ok, msg = asyncio.run(XEngine().start())
    assert not ok and "密钥" in msg


def test_quiet_hours_do_not_spin(engine, fake_x):
    settings_store.save_section("x", {"quiet_enabled": True, "quiet_start": "00:00", "quiet_end": "23:59"})
    tick(engine)
    assert engine.state == "quiet"
    assert fake_x.requests == []                               # 静默时段不请求 X
    assert engine._seconds_until_next(settings_store.load()) >= 55
