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
from pydantic import BaseModel

from . import db, settings_store
from .llm import ChatClient, registry
from .paths import WEB_DIR
from .scheduler import Engine

engine = Engine()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    logger.info("控制台已就绪")
    if db.get_setting("autostart", False):
        logger.info("检测到自动启动配置，正在启动引擎")
        await engine.start()
    yield
    if engine.running:
        await engine.stop()


app = FastAPI(title="小红书自动回复", lifespan=lifespan, docs_url=None, redoc_url=None)


# ==================== 状态与控制 ====================

@app.get("/api/status")
async def get_status():
    return engine.snapshot()


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
    }


class SectionUpdate(BaseModel):
    values: dict


@app.put("/api/config/{section}")
async def update_config(section: str, body: SectionUpdate):
    values = dict(body.values)
    # 前端回传的是脱敏后的 Key，出现掩码字符说明用户没改，保持原值
    if section == "llm":
        key = values.get("api_key", "")
        if "·" in key or key == "已设置":
            values.pop("api_key", None)
    try:
        settings_store.save_section(section, values)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(422, f"配置校验失败: {e}")

    engine.wake()
    logger.info(f"配置已更新: {section}")
    return {"success": True, "config": settings_store.redact(settings_store.load())}


@app.put("/api/autostart")
async def set_autostart(body: SectionUpdate):
    value = bool(body.values.get("enabled", False))
    db.set_setting("autostart", value)
    return {"success": True, "autostart": value}


@app.post("/api/llm/test")
async def test_llm():
    cfg = settings_store.load()
    ok, msg = await ChatClient(cfg.llm).test()
    return {"success": ok, "message": msg}


# ==================== 审核队列 ====================

@app.get("/api/replies")
async def get_replies(status: str | None = None, limit: int = 50, offset: int = 0):
    return {
        "items": db.list_replies(status, min(limit, 200), offset),
        "counts": {
            "pending_review": db.count_by_status(db.STATUS_PENDING_REVIEW),
            "approved": db.count_by_status(db.STATUS_APPROVED),
            "sent": db.count_by_status(db.STATUS_SENT),
            "failed": db.count_by_status(db.STATUS_FAILED),
            "rejected": db.count_by_status(db.STATUS_REJECTED),
            "blocked": db.count_by_status(db.STATUS_BLOCKED),
        },
    }


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

    db.update_reply_status(
        reply_id, db.STATUS_APPROVED, final_text=text, error="", mark_reviewed=True
    )
    engine.wake()          # 让引擎立刻醒来发送，不必等下一轮
    return {"success": True, "message": "已批准，即将发送"}


@app.post("/api/replies/{reply_id}/reject")
async def reject_reply(reply_id: int):
    if not db.get_reply(reply_id):
        raise HTTPException(404, "记录不存在")
    db.update_reply_status(reply_id, db.STATUS_REJECTED, mark_reviewed=True)
    return {"success": True, "message": "已拒绝"}


@app.post("/api/replies/{reply_id}/regenerate")
async def regenerate_reply(reply_id: int):
    ok, msg = await engine.regenerate(reply_id)
    return {"success": ok, "message": msg}


@app.post("/api/replies/approve_all")
async def approve_all():
    rows = db.list_replies(db.STATUS_PENDING_REVIEW, limit=200)
    count = 0
    for r in rows:
        text = (r["final_text"] or r["draft"] or "").strip()
        if not text:
            continue
        db.update_reply_status(
            r["id"], db.STATUS_APPROVED, final_text=text, mark_reviewed=True
        )
        count += 1
    engine.wake()
    return {"success": True, "message": f"已批准 {count} 条"}


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
    total_failed = db.count_by_status(db.STATUS_FAILED)
    attempted = total_sent + total_failed

    return {
        "today_sent": db.count_sent_today(),
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
            payload = json.dumps(engine.snapshot(), ensure_ascii=False)
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
