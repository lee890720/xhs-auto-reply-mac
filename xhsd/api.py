"""
控制台后端。

只监听 127.0.0.1，不对外暴露；引擎与 Web 服务在同一进程内，
菜单栏 App 通过 HTTP 控制它。
"""
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date, timedelta

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel, ValidationError

from . import db, settings_store
from .llm import ChatClient, registry
from .paths import WEB_DIR
from .scheduler import Engine
from .x_api import X_MAX_WEIGHT, weighted_length
from .x_engine import XEngine

# 两个平台各一个引擎，各自启停，互不影响。
# 小红书引擎会开浏览器，X 引擎只走 API；只开 X 时不会启动 Chromium。
engine = Engine()          # 小红书
x_engine = XEngine()       # X

PLATFORMS = (db.PLATFORM_XHS, db.PLATFORM_X)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    logger.info("控制台已就绪")
    # 两个平台默认都不自动运行，各自在设置里勾选「守护进程启动时自动开始」
    if db.get_setting("autostart", False):
        logger.info("检测到小红书自动启动配置，正在启动小红书引擎")
        await engine.start()
    if db.get_setting("x_autostart", False):
        logger.info("检测到 X 自动启动配置，正在启动 X 引擎")
        ok, msg = await x_engine.start()
        if not ok:
            logger.warning(f"X 引擎没有启动: {msg}")
    yield
    if engine.running:
        await engine.stop()
    if x_engine.running:
        await x_engine.stop()


app = FastAPI(title="小红书 / X 自动回复", lifespan=lifespan, docs_url=None, redoc_url=None)


def wake_all() -> None:
    engine.wake()
    x_engine.wake()


def _check_platform(platform: str | None) -> str | None:
    if platform and platform not in PLATFORMS:
        raise HTTPException(400, f"未知平台: {platform}")
    return platform or None


# ==================== 状态与控制 ====================

def full_status() -> dict:
    """
    顶层字段保持是小红书引擎的状态（菜单栏旧版本按这个格式读），
    X 的状态放在 x 字段里。
    """
    data = engine.snapshot()
    data["x"] = x_engine.snapshot()
    data["pending_review_total"] = db.count_by_status(db.STATUS_PENDING_REVIEW)
    return data


@app.get("/api/status")
async def get_status():
    return full_status()


@app.post("/api/engine/{action}")
async def control_engine(action: str):
    if action == "start":
        ok, msg = await engine.start()
    elif action == "stop":
        ok, msg = await engine.stop()
    elif action == "restart":
        await engine.stop()
        await asyncio.sleep(1)
        ok, msg = await engine.start()
    else:
        raise HTTPException(400, "未知操作")
    return {"success": ok, "message": msg}


@app.post("/api/x/engine/{action}")
async def control_x_engine(action: str):
    if action == "start":
        ok, msg = await x_engine.start()
    elif action == "stop":
        ok, msg = await x_engine.stop()
    elif action == "restart":
        await x_engine.stop()
        ok, msg = await x_engine.start()
    else:
        raise HTTPException(400, "未知操作")
    return {"success": ok, "message": msg}


@app.post("/api/login/open")
async def login_open():
    ok, msg = await engine.open_browser_for_login()
    return {"success": ok, "message": msg}


@app.post("/api/login/recheck")
async def login_recheck():
    ok, msg = await engine.recheck_login()
    return {"success": ok, "message": msg}


# ==================== 配置 ====================

@app.get("/api/config")
async def get_config():
    cfg = settings_store.load()
    return {
        "config": settings_store.redact(cfg),
        "providers": registry.as_dict(),
        "autostart": db.get_setting("autostart", False),
        "x_autostart": db.get_setting("x_autostart", False),
        "x_max_weight": X_MAX_WEIGHT,
    }


class SectionUpdate(BaseModel):
    values: dict


def _validation_message(e: ValidationError) -> str:
    """把 pydantic 的英文校验错误翻成控制台上能直接看懂的话"""
    parts = []
    for err in e.errors():
        field = ".".join(str(x) for x in err.get("loc", ()))
        kind, ctx = err.get("type", ""), err.get("ctx") or {}
        if kind == "value_error":
            msg = str(ctx.get("error", err.get("msg", "")))
        elif kind == "greater_than_equal":
            msg = f"不能小于 {ctx.get('ge')}"
        elif kind == "less_than_equal":
            msg = f"不能大于 {ctx.get('le')}"
        elif kind in ("int_type", "int_parsing", "int_from_float", "float_type", "float_parsing"):
            msg = "必须填写数字"
        else:
            msg = err.get("msg", "")
        parts.append(f"{field} {msg}" if field else msg)
    return "配置校验失败：" + "；".join(parts)


@app.put("/api/config/{section}")
async def update_config(section: str, body: SectionUpdate):
    values = dict(body.values)
    # 前端回传的是脱敏后的 Key，出现掩码字符说明用户没改，保持原值
    for field in settings_store.SECRET_FIELDS.get(section, ()):
        if settings_store.is_masked(values.get(field)):
            values.pop(field, None)
    try:
        settings_store.save_section(section, values)
    except ValidationError as e:           # 必须在 ValueError 之前：它是 ValueError 的子类
        raise HTTPException(422, _validation_message(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(422, f"配置校验失败: {e}")

    wake_all()
    logger.info(f"配置已更新: {section}")
    return {"success": True, "config": settings_store.redact(settings_store.load())}


@app.put("/api/autostart")
async def set_autostart(body: SectionUpdate):
    value = bool(body.values.get("enabled", False))
    db.set_setting("autostart", value)
    return {"success": True, "autostart": value}


@app.put("/api/x/autostart")
async def set_x_autostart(body: SectionUpdate):
    value = bool(body.values.get("enabled", False))
    db.set_setting("x_autostart", value)
    return {"success": True, "autostart": value}


@app.post("/api/x/test")
async def test_x():
    ok, msg = await x_engine.test()
    return {"success": ok, "message": msg}


@app.post("/api/llm/test")
async def test_llm():
    cfg = settings_store.load()
    ok, msg = await ChatClient(cfg.llm).test()
    return {"success": ok, "message": msg}


# ==================== 审核队列 ====================

@app.get("/api/replies")
async def get_replies(
    status: str | None = None, limit: int = 50, offset: int = 0, platform: str | None = None
):
    platform = _check_platform(platform)
    statuses = (db.STATUS_PENDING_REVIEW, db.STATUS_APPROVED, db.STATUS_SENT,
                db.STATUS_FAILED, db.STATUS_REJECTED, db.STATUS_BLOCKED)
    return {
        "items": db.list_replies(status, min(limit, 200), offset, platform),
        "counts": {st: db.count_by_status(st, platform) for st in statuses},
        "platform_pending": {
            p: db.count_by_status(db.STATUS_PENDING_REVIEW, p) for p in PLATFORMS
        },
    }


def _check_x_length(row: dict, text: str) -> None:
    """X 的回复有长度上限，超了在批准时就拦下，别等发送失败才发现"""
    if row.get("platform") == db.PLATFORM_X and weighted_length(text) > X_MAX_WEIGHT:
        raise HTTPException(
            400,
            f"超过 X 的 {X_MAX_WEIGHT} 字符上限（中文一个字算 2，"
            f"当前 {weighted_length(text)}），请改短一些",
        )


class ApproveBody(BaseModel):
    text: str | None = None


@app.post("/api/replies/{reply_id}/approve")
async def approve_reply(reply_id: int, body: ApproveBody):
    row = db.get_reply(reply_id)
    if not row:
        raise HTTPException(404, "记录不存在")

    text = (body.text or row["final_text"] or row["draft"] or "").strip()
    if not text:
        raise HTTPException(400, "回复内容为空，无法批准")
    _check_x_length(row, text)

    db.update_reply_status(
        reply_id, db.STATUS_APPROVED, final_text=text, error="", mark_reviewed=True
    )
    # 让对应平台的引擎立刻醒来发送，不必等下一轮
    (x_engine if row.get("platform") == db.PLATFORM_X else engine).wake()
    message = "已批准，即将发送"
    running = x_engine.running if row.get("platform") == db.PLATFORM_X else engine.running
    if not running:
        message = "已批准。这个平台的引擎没有运行，启动后才会发出去"
    return {"success": True, "message": message}


@app.post("/api/replies/{reply_id}/reject")
async def reject_reply(reply_id: int):
    if not db.get_reply(reply_id):
        raise HTTPException(404, "记录不存在")
    db.update_reply_status(reply_id, db.STATUS_REJECTED, mark_reviewed=True)
    return {"success": True, "message": "已拒绝"}


@app.post("/api/replies/{reply_id}/regenerate")
async def regenerate_reply(reply_id: int):
    row = db.get_reply(reply_id)
    if row and row.get("platform") == db.PLATFORM_X:
        ok, msg = await x_engine.regenerate(reply_id)
    else:
        ok, msg = await engine.regenerate(reply_id)
    return {"success": ok, "message": msg}


@app.post("/api/replies/approve_all")
async def approve_all(platform: str | None = None):
    platform = _check_platform(platform)
    rows = db.list_replies(db.STATUS_PENDING_REVIEW, limit=200, platform=platform)
    count = skipped = 0
    for r in rows:
        text = (r["final_text"] or r["draft"] or "").strip()
        if not text:
            continue
        if r.get("platform") == db.PLATFORM_X and weighted_length(text) > X_MAX_WEIGHT:
            skipped += 1
            continue
        db.update_reply_status(
            r["id"], db.STATUS_APPROVED, final_text=text, mark_reviewed=True
        )
        count += 1
    wake_all()
    message = f"已批准 {count} 条"
    if skipped:
        message += f"，{skipped} 条超过 X 长度上限没有批准"
    return {"success": True, "message": message}


# ==================== X 关注者 ====================
# 这里没有「全部回关」：X 开发者指南把自动回关归为批量/激进关注。
# 每个回关都得是人对一个具体账号做的决定。

@app.get("/api/x/followers")
async def get_x_followers(status: str | None = None, limit: int = 100, offset: int = 0):
    return {
        "items": db.list_followers(status, min(limit, 200), offset),
        "counts": db.count_followers_by_status(),
    }


@app.post("/api/x/followers/{user_id}/follow")
async def follow_x_follower(user_id: str):
    f = db.get_follower(user_id)
    if not f:
        raise HTTPException(404, "记录不存在")
    if f["status"] not in (db.FOLLOWER_PENDING, db.FOLLOWER_IGNORED, db.FOLLOWER_FAILED):
        raise HTTPException(400, "这个人已经回关过或正在排队")
    db.update_follower_status(user_id, db.FOLLOWER_APPROVED, error="", mark_acted=True)
    x_engine.wake()
    if not x_engine.running:
        return {"success": True, "message": "已加入回关队列。X 引擎没有运行，启动后才会执行"}
    return {"success": True, "message": "已加入回关队列，按间隔依次执行"}


@app.post("/api/x/followers/{user_id}/ignore")
async def ignore_x_follower(user_id: str):
    f = db.get_follower(user_id)
    if not f:
        raise HTTPException(404, "记录不存在")
    if f["status"] == db.FOLLOWER_FOLLOWED:
        raise HTTPException(400, "已经回关了，忽略不会取消关注")
    db.update_follower_status(user_id, db.FOLLOWER_IGNORED, mark_acted=True)
    return {"success": True, "message": "已忽略"}


@app.post("/api/x/followers/check")
async def check_x_followers():
    if not x_engine.running:
        return {"success": False, "message": "X 引擎没有运行"}
    x_engine.check_followers_now()
    return {"success": True, "message": "正在检查新关注者"}


# ==================== 统计与日志 ====================

@app.get("/api/stats")
async def get_stats():
    today = date.today()
    trend = []
    for i in range(13, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        trend.append({
            "date": d,
            "label": d[5:],
            "count": db.scalar(
                "SELECT COUNT(*) FROM replies WHERE status = ? AND sent_at LIKE ?",
                (db.STATUS_SENT, f"{d}%"),
            ) or 0,
            "is_today": i == 0,
        })

    total_sent = db.count_by_status(db.STATUS_SENT)
    today_by_platform = {p: db.count_sent_today(p) for p in PLATFORMS}
    total_failed = db.count_by_status(db.STATUS_FAILED)
    attempted = total_sent + total_failed

    return {
        "today_sent": db.count_sent_today(),
        "today_by_platform": today_by_platform,
        "total_sent": total_sent,
        "total_failed": total_failed,
        "success_rate": round(total_sent / attempted * 100, 1) if attempted else 0.0,
        "total_messages": db.scalar("SELECT COUNT(*) FROM messages") or 0,
        "trend": trend,
        "top_authors": db.query(
            """SELECT m.author, COUNT(*) AS n FROM replies r
               JOIN messages m ON m.id = r.message_id
               WHERE r.status = ? GROUP BY m.author ORDER BY n DESC LIMIT 10""",
            (db.STATUS_SENT,),
        ),
    }


@app.get("/api/events")
async def get_events(limit: int = 200):
    return {"items": db.list_events(min(limit, 1000))}


@app.get("/api/stream")
async def stream():
    """SSE：推送引擎状态，供控制台实时刷新状态灯与计数"""
    async def gen():
        while True:
            payload = json.dumps(full_status(), ensure_ascii=False)
            yield f"data: {payload}\n\n"
            await asyncio.sleep(3)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/maintenance/cleanup")
async def cleanup(days: int = 90):
    deleted = db.cleanup(days)
    return {"success": True, "message": f"已清理 {deleted} 条 {days} 天前的记录"}


# ==================== 静态资源 ====================

@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")
