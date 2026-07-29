#!/usr/bin/env python3
"""
小红书自动回复 - 统一管理中心
=================================
一个命令管理全部：启动主程序、停止主程序、查看运维数据、实时日志。

使用方式：
    python manager.py              # 启动管理中心
    python manager.py --port 9090  # 指定端口

打开浏览器访问 http://localhost:8080 即可看到统一操控台。
"""

import json
import os
import sys
import signal
import subprocess
import threading
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ============ 路径常量 ============
PROJECT_DIR = Path(__file__).parent.resolve()
STATE_FILE = Path("/tmp/xhs-auto-reply/processed_messages.json")
LOG_FILE = Path("/tmp/xhs-auto-reply/auto_reply.log")
PID_FILE = Path("/tmp/xhs-auto-reply/manager.pid")


# ============ 进程管理 ============

class ProcessManager:
    """管理 main.py 子进程的生命周期"""

    def __init__(self):
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        with self._lock:
            if self._process is None:
                return False
            return self._process.poll() is None

    @property
    def pid(self) -> int | None:
        with self._lock:
            if self._process and self._process.poll() is None:
                return self._process.pid
            return None

    def start(self) -> tuple[bool, str]:
        """启动 main.py"""
        with self._lock:
            if self._process and self._process.poll() is None:
                return False, "主程序已在运行中"

            try:
                self._process = subprocess.Popen(
                    [sys.executable, str(PROJECT_DIR / "main.py")],
                    cwd=str(PROJECT_DIR),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    preexec_fn=os.setsid,
                )
                return True, f"主程序已启动 (PID: {self._process.pid})"
            except Exception as e:
                return False, f"启动失败: {e}"

    def stop(self) -> tuple[bool, str]:
        """停止 main.py（优雅终止）"""
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                self._process = None
                return False, "主程序未在运行"

            pid = self._process.pid
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
                try:
                    self._process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                    self._process.wait(timeout=3)
                self._process = None
                return True, f"主程序已停止 (PID: {pid})"
            except ProcessLookupError:
                self._process = None
                return True, "主程序已停止（进程已不存在）"
            except Exception as e:
                self._process = None
                return False, f"停止失败: {e}"

    def restart(self) -> tuple[bool, str]:
        """重启 main.py"""
        stop_ok, stop_msg = self.stop()
        time.sleep(2)
        start_ok, start_msg = self.start()
        if start_ok:
            return True, f"主程序已重启 (PID: {self._process.pid})"
        return False, start_msg


# ============ 数据加载 ============

def load_stats():
    """读取统计数据"""
    if not STATE_FILE.exists():
        return _empty_stats(), []

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return _empty_stats(), []

    raw = data.get("processed", {})
    last_updated = data.get("last_updated", "")

    records = []
    for msg_id, record in raw.items():
        ts = record.get("timestamp", "")
        status = record.get("status", "success")
        msg_type = record.get("msg_type", "comment")
        mode = record.get("mode", "auto")
        author = record.get("author", "")
        if not author and "_" in msg_id:
            parts = msg_id.split("_")
            if len(parts) >= 3:
                author = parts[1]

        records.append({
            "msg_id": msg_id,
            "timestamp": ts,
            "reply": record.get("reply", ""),
            "content": record.get("content", ""),
            "author": author or "未知",
            "msg_type": msg_type,
            "status": status,
            "mode": mode,
            "note_image_url": record.get("note_image_url", ""),
        })

    records.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

    today_str = date.today().isoformat()
    today_records = [r for r in records if r.get("timestamp", "").startswith(today_str)]

    today_count = len(today_records)
    total_count = len(records)
    today_success = sum(1 for r in today_records if r.get("status") == "success")
    success_rate = round(today_success / today_count * 100, 1) if today_count > 0 else 0
    remaining_quota = max(0, 50 - today_count)

    daily_data = []
    for i in range(6, -1, -1):
        d = (date.today() - timedelta(days=i)).isoformat()
        day_records = [r for r in records if r.get("timestamp", "").startswith(d)]
        daily_data.append({
            "date": d,
            "date_label": d[5:],
            "count": len(day_records),
            "is_today": i == 0,
        })

    stats = {
        "today_count": today_count,
        "total_count": total_count,
        "success_rate": success_rate,
        "remaining_quota": remaining_quota,
        "last_updated": last_updated,
        "daily_data": daily_data,
    }
    return stats, records


def _empty_stats():
    return {
        "today_count": 0,
        "total_count": 0,
        "success_rate": 0,
        "remaining_quota": 50,
        "last_updated": None,
        "daily_data": [],
    }


def load_logs(lines: int = 50) -> list[str]:
    """读取最近 N 行日志"""
    if not LOG_FILE.exists():
        return ["（暂无日志）"]
    try:
        content = LOG_FILE.read_text(encoding="utf-8")
        all_lines = content.strip().split("\n")
        return all_lines[-lines:]
    except Exception:
        return ["（日志读取失败）"]


# ============ HTML 页面 ============

def generate_html(stats: dict, records: list, is_running: bool, pid: int | None) -> str:
    """生成完整的操控台 HTML 页面"""

    today_count = stats["today_count"]
    total_count = stats["total_count"]
    success_rate = stats["success_rate"]
    remaining_quota = stats["remaining_quota"]
    last_updated = stats["last_updated"] or "无"
    daily_data = stats["daily_data"]

    status_dot = "#52c41a" if is_running else "#ff4d4f"
    status_text = "运行中" if is_running else "未运行"
    pid_text = f"PID: {pid}" if pid else ""

    start_disabled = "disabled" if is_running else ""
    stop_disabled = "" if is_running else "disabled"
    start_btn_text = "主程序运行中" if is_running else "启动主程序"
    stop_btn_text = "停止主程序" if is_running else "主程序未运行"

    # 柱状图
    max_count = max((d["count"] for d in daily_data), default=1) or 1
    bars_html = ""
    for d in daily_data:
        height = (d["count"] / max_count * 100) if max_count > 0 else 0
        bar_color = "#1677ff" if d["is_today"] else "#d9d9d9"
        label_color = "#1677ff" if d["is_today"] else "#8c8c8c"
        bars_html += f"""
            <div class="bar-item">
                <div class="bar-value" style="color:{label_color}">{d['count']}</div>
                <div class="bar-track">
                    <div class="bar-fill" style="height:{height}%;background:{bar_color};"></div>
                </div>
                <div class="bar-label">{d['date_label']}</div>
            </div>
        """

    # 回复列表
    rows_html = ""
    type_labels = {"comment": "评论", "message": "私信"}
    status_labels = {"success": "成功", "failed": "失败", "skipped": "跳过"}
    status_colors = {"success": "#52c41a", "failed": "#ff4d4f", "skipped": "#fa8c16"}

    for r in records[:20]:
        ts = r.get("timestamp", "")[:19]
        author = r.get("author", "未知")
        msg_type = type_labels.get(r.get("msg_type", "comment"), r.get("msg_type", "comment"))
        status = r.get("status", "success")
        status_label = status_labels.get(status, status)
        status_color = status_colors.get(status, "#8c8c8c")
        content = (r.get("content", "") or "")[:20]
        reply = (r.get("reply", "") or "")[:30]
        note_img = r.get("note_image_url", "")
        note_cell = (
            f'<img src="{note_img}" class="note-thumb" onclick="window.open(\'{note_img}\',\'_blank\')" title="点击查看笔记封面">'
            if note_img
            else '<span style="color:#bfbfbf;font-size:12px;">无</span>'
        )

        rows_html += f"""
            <tr>
                <td>{ts}</td>
                <td>{author}</td>
                <td>{note_cell}</td>
                <td>{msg_type}</td>
                <td><span class="status-tag" style="background:{status_color}15;color:{status_color};border:1px solid {status_color}40">{status_label}</span></td>
                <td class="truncate" title="{r.get('content','')}">{content}</td>
                <td class="truncate" title="{r.get('reply','')}">{reply}</td>
            </tr>
        """

    if not rows_html:
        rows_html = '<tr><td colspan="7" style="text-align:center;color:#8c8c8c;padding:32px;">暂无回复记录</td></tr>'

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>小红书自动回复 · 统一管理中心</title>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif;
        background: #f0f2f5;
        color: #262626;
        line-height: 1.6;
    }}
    .container {{
        max-width: 1100px;
        margin: 0 auto;
        padding: 16px;
    }}
    @media (min-width: 768px) {{
        .container {{ padding: 24px; }}
    }}

    .header {{
        background: linear-gradient(135deg, #1677ff 0%, #0958d9 100%);
        border-radius: 12px;
        padding: 20px 24px;
        margin-bottom: 16px;
        color: #fff;
        display: flex;
        align-items: center;
        justify-content: space-between;
        flex-wrap: wrap;
        gap: 12px;
        box-shadow: 0 4px 12px rgba(22,119,255,0.3);
    }}
    .header-left {{
        display: flex;
        align-items: center;
        gap: 12px;
    }}
    .header-title {{
        font-size: 20px;
        font-weight: 700;
    }}
    .header-subtitle {{
        font-size: 12px;
        opacity: 0.8;
    }}
    .header-right {{
        display: flex;
        align-items: center;
        gap: 16px;
        font-size: 13px;
    }}
    .status-badge {{
        display: flex;
        align-items: center;
        gap: 8px;
        background: rgba(255,255,255,0.15);
        padding: 6px 14px;
        border-radius: 20px;
        font-size: 13px;
        font-weight: 500;
    }}
    .status-dot {{
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: {status_dot};
        box-shadow: 0 0 0 3px {status_dot}44;
        animation: {'pulse 2s infinite' if is_running else 'none'};
    }}
    @keyframes pulse {{
        0%, 100% {{ opacity: 1; }}
        50% {{ opacity: 0.5; }}
    }}
    .pid-text {{
        font-size: 11px;
        opacity: 0.7;
        font-family: "SFMono-Regular", Consolas, monospace;
    }}

    .control-panel {{
        background: #fff;
        border-radius: 12px;
        padding: 20px;
        margin-bottom: 16px;
        box-shadow: 0 2px 8px rgba(0,0,0,0.06);
        display: flex;
        align-items: center;
        gap: 12px;
        flex-wrap: wrap;
    }}
    .control-panel .section-label {{
        font-size: 14px;
        font-weight: 600;
        color: #262626;
        margin-right: 4px;
        white-space: nowrap;
    }}
    .btn {{
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 8px 20px;
        border: none;
        border-radius: 8px;
        font-size: 13px;
        font-weight: 500;
        cursor: pointer;
        transition: all 0.2s;
        white-space: nowrap;
    }}
    .btn:disabled {{
        opacity: 0.45;
        cursor: not-allowed;
    }}
    .btn-start {{
        background: #52c41a;
        color: #fff;
    }}
    .btn-start:hover:not(:disabled) {{
        background: #49b018;
        box-shadow: 0 2px 8px rgba(82,196,26,0.4);
    }}
    .btn-stop {{
        background: #ff4d4f;
        color: #fff;
    }}
    .btn-stop:hover:not(:disabled) {{
        background: #e04345;
        box-shadow: 0 2px 8px rgba(255,77,79,0.4);
    }}
    .btn-restart {{
        background: #fa8c16;
        color: #fff;
    }}
    .btn-restart:hover:not(:disabled) {{
        background: #e07d14;
        box-shadow: 0 2px 8px rgba(250,140,22,0.4);
    }}
    .control-msg {{
        font-size: 12px;
        color: #8c8c8c;
        margin-left: 8px;
        flex: 1;
        min-width: 150px;
    }}
    .control-msg.success {{ color: #52c41a; }}
    .control-msg.error {{ color: #ff4d4f; }}

    .cards-grid {{
        display: grid;
        grid-template-columns: repeat(2, 1fr);
        gap: 12px;
        margin-bottom: 16px;
    }}
    @media (min-width: 768px) {{
        .cards-grid {{ grid-template-columns: repeat(4, 1fr); gap: 16px; }}
    }}
    .card {{
        background: #fff;
        border-radius: 12px;
        padding: 20px 16px;
        text-align: center;
        box-shadow: 0 2px 8px rgba(0,0,0,0.06);
        transition: transform 0.2s, box-shadow 0.2s;
    }}
    .card:hover {{
        transform: translateY(-2px);
        box-shadow: 0 4px 16px rgba(0,0,0,0.1);
    }}
    .card-value {{
        font-size: 32px;
        font-weight: 700;
        color: #1677ff;
        line-height: 1.2;
    }}
    .card-label {{
        font-size: 13px;
        color: #8c8c8c;
        margin-top: 6px;
    }}
    .card-success .card-value {{ color: #52c41a; }}

    .section {{
        background: #fff;
        border-radius: 12px;
        padding: 20px;
        margin-bottom: 16px;
        box-shadow: 0 2px 8px rgba(0,0,0,0.06);
    }}
    .section-title {{
        font-size: 15px;
        font-weight: 600;
        color: #262626;
        margin-bottom: 16px;
        padding-bottom: 10px;
        border-bottom: 1px solid #f0f0f0;
    }}

    .chart-wrapper {{
        display: flex;
        align-items: flex-end;
        justify-content: space-between;
        height: 180px;
        padding: 8px 0 0;
        gap: 6px;
    }}
    .bar-item {{
        flex: 1;
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: flex-end;
        height: 100%;
        min-width: 0;
    }}
    .bar-value {{
        font-size: 12px;
        font-weight: 600;
        margin-bottom: 4px;
    }}
    .bar-track {{
        width: 70%;
        flex: 1;
        display: flex;
        align-items: flex-end;
        border-radius: 4px 4px 0 0;
        background: #f5f5f5;
        overflow: hidden;
    }}
    .bar-fill {{
        width: 100%;
        border-radius: 4px 4px 0 0;
        min-height: 2px;
        transition: height 0.5s ease;
    }}
    .bar-label {{
        font-size: 11px;
        color: #8c8c8c;
        margin-top: 6px;
    }}

    .table-wrapper {{
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
    }}
    table {{
        width: 100%;
        border-collapse: collapse;
        font-size: 13px;
        min-width: 600px;
    }}
    th {{
        background: #fafafa;
        color: #595959;
        padding: 10px 12px;
        text-align: left;
        font-weight: 500;
        border-bottom: 1px solid #f0f0f0;
        white-space: nowrap;
    }}
    td {{
        padding: 10px 12px;
        border-bottom: 1px solid #f5f5f5;
        color: #434343;
        vertical-align: middle;
    }}
    tr:hover td {{
        background: #fafafa;
    }}
    .truncate {{
        max-width: 120px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }}
    @media (min-width: 768px) {{
        .truncate {{ max-width: 180px; }}
    }}
    .status-tag {{
        display: inline-block;
        padding: 1px 8px;
        border-radius: 4px;
        font-size: 12px;
        font-weight: 500;
        white-space: nowrap;
    }}
    .note-thumb {{
        width: 48px;
        height: 48px;
        object-fit: cover;
        border-radius: 6px;
        cursor: pointer;
        transition: transform 0.2s;
    }}
    .note-thumb:hover {{
        transform: scale(1.1);
    }}

    .log-viewer {{
        background: #1e1e1e;
        color: #d4d4d4;
        font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
        font-size: 12px;
        padding: 16px;
        border-radius: 8px;
        max-height: 300px;
        overflow-y: auto;
        line-height: 1.7;
        white-space: pre-wrap;
        word-break: break-all;
    }}
    .log-viewer .log-info {{ color: #6a9955; }}
    .log-viewer .log-warn {{ color: #ce9178; }}
    .log-viewer .log-error {{ color: #f44747; }}
    .log-viewer .log-debug {{ color: #808080; }}

    .footer {{
        text-align: center;
        color: #bfbfbf;
        font-size: 12px;
        padding: 16px 0 8px;
        line-height: 1.8;
    }}
    .footer code {{
        background: #f5f5f5;
        padding: 1px 6px;
        border-radius: 4px;
        font-family: "SFMono-Regular", Consolas, monospace;
    }}

    .toast {{
        position: fixed;
        top: 20px;
        right: 20px;
        padding: 12px 20px;
        border-radius: 8px;
        font-size: 13px;
        font-weight: 500;
        color: #fff;
        z-index: 9999;
        opacity: 0;
        transform: translateY(-10px);
        transition: all 0.3s;
        pointer-events: none;
    }}
    .toast.show {{
        opacity: 1;
        transform: translateY(0);
    }}
    .toast.success {{ background: #52c41a; }}
    .toast.error {{ background: #ff4d4f; }}
    .toast.info {{ background: #1677ff; }}
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <div class="header-left">
            <div class="header-title">小红书自动回复</div>
            <div class="header-subtitle">统一管理中心</div>
        </div>
        <div class="header-right">
            <div class="status-badge">
                <span class="status-dot" id="status-dot"></span>
                <span id="status-text">{status_text}</span>
                <span class="pid-text" id="pid-text">{pid_text}</span>
            </div>
            <span style="font-size:12px;opacity:0.8;">更新: <span id="last-updated">{last_updated}</span></span>
        </div>
    </div>

    <div class="control-panel">
        <span class="section-label">程序控制</span>
        <button class="btn btn-start" id="btn-start" {start_disabled} onclick="control('start')">
            &#9654; {start_btn_text}
        </button>
        <button class="btn btn-stop" id="btn-stop" {stop_disabled} onclick="control('stop')">
            &#9632; {stop_btn_text}
        </button>
        <button class="btn btn-restart" id="btn-restart" onclick="control('restart')">
            &#8635; 重启主程序
        </button>
        <span class="control-msg" id="control-msg"></span>
    </div>

    <div class="cards-grid">
        <div class="card">
            <div class="card-value" id="val-today">{today_count}</div>
            <div class="card-label">今日回复数</div>
        </div>
        <div class="card">
            <div class="card-value" id="val-total">{total_count}</div>
            <div class="card-label">总回复数</div>
        </div>
        <div class="card card-success">
            <div class="card-value" id="val-rate">{success_rate}%</div>
            <div class="card-label">成功率</div>
        </div>
        <div class="card">
            <div class="card-value" id="val-quota">{remaining_quota}</div>
            <div class="card-label">剩余配额</div>
        </div>
    </div>

    <div class="section">
        <div class="section-title">最近 7 天回复趋势</div>
        <div class="chart-wrapper" id="chart-wrapper">
            {bars_html}
        </div>
    </div>

    <div class="section">
        <div class="section-title">最近回复列表（最多 20 条）</div>
        <div class="table-wrapper">
            <table>
                <thead>
                    <tr>
                        <th>时间</th>
                        <th>作者</th>
                        <th>笔记</th>
                        <th>类型</th>
                        <th>状态</th>
                        <th>原文</th>
                        <th>回复</th>
                    </tr>
                </thead>
                <tbody id="records-tbody">
                    {rows_html}
                </tbody>
            </table>
        </div>
    </div>

    <div class="section">
        <div class="section-title">实时日志 <span style="font-weight:400;font-size:12px;color:#8c8c8c;">（最近 50 行，每 10 秒刷新）</span></div>
        <div class="log-viewer" id="log-viewer">加载中...</div>
    </div>

    <div class="footer">
        <div>数据文件: <code>{STATE_FILE}</code></div>
        <div>每 10 秒自动刷新 · 下次刷新: <span id="countdown">10s</span></div>
        <div style="margin-top:4px;">启动命令: <code>python manager.py</code></div>
    </div>
</div>

<div class="toast" id="toast"></div>

<script>
const REFRESH_INTERVAL = 10000;
let countdown = REFRESH_INTERVAL / 1000;

function showToast(msg, type) {{
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.className = 'toast ' + type + ' show';
    setTimeout(() => t.classList.remove('show'), 3000);
}}

async function control(action) {{
    const label = {{start: '启动', stop: '停止', restart: '重启'}}[action];
    const msgEl = document.getElementById('control-msg');

    if (!confirm('确定要' + label + '主程序吗？')) return;

    msgEl.textContent = '正在' + label + '...';
    msgEl.className = 'control-msg';

    try {{
        const resp = await fetch('/api/control/' + action, {{ method: 'POST' }});
        const data = await resp.json();
        msgEl.textContent = data.message;
        msgEl.className = 'control-msg ' + (data.success ? 'success' : 'error');
        showToast(data.message, data.success ? 'success' : 'error');
    }} catch (e) {{
        msgEl.textContent = '请求失败: ' + e;
        msgEl.className = 'control-msg error';
        showToast('请求失败', 'error');
    }}

    setTimeout(fetchData, 2000);
}}

function updateCountdown() {{
    const el = document.getElementById('countdown');
    if (el) el.textContent = countdown + 's';
    countdown--;
    if (countdown < 0) countdown = REFRESH_INTERVAL / 1000;
}}

async function fetchData() {{
    try {{
        const [statsResp, logsResp] = await Promise.all([
            fetch('/api/stats'),
            fetch('/api/logs'),
        ]);

        const statsData = await statsResp.json();
        const logsData = await logsResp.json();

        updateStatus(statsData.is_running, statsData.pid);
        updateButtons(statsData.is_running);
        document.getElementById('last-updated').textContent = statsData.last_updated || '无';
        document.getElementById('val-today').textContent = statsData.today_count;
        document.getElementById('val-total').textContent = statsData.total_count;
        document.getElementById('val-rate').textContent = statsData.success_rate + '%';
        document.getElementById('val-quota').textContent = statsData.remaining_quota;
        updateChart(statsData.daily_data);
        updateTable(statsData.records);
        updateLogs(logsData.lines);

        countdown = REFRESH_INTERVAL / 1000;
    }} catch (e) {{
        console.error('刷新失败:', e);
    }}
}}

function updateStatus(isRunning, pid) {{
    const dot = document.getElementById('status-dot');
    const text = document.getElementById('status-text');
    const pidText = document.getElementById('pid-text');

    if (isRunning) {{
        dot.style.background = '#52c41a';
        dot.style.boxShadow = '0 0 0 3px #52c41a44';
        dot.style.animation = 'pulse 2s infinite';
        text.textContent = '运行中';
        pidText.textContent = pid ? 'PID: ' + pid : '';
    }} else {{
        dot.style.background = '#ff4d4f';
        dot.style.boxShadow = '0 0 0 3px #ff4d4f44';
        dot.style.animation = 'none';
        text.textContent = '未运行';
        pidText.textContent = '';
    }}
}}

function updateButtons(isRunning) {{
    const btnStart = document.getElementById('btn-start');
    const btnStop = document.getElementById('btn-stop');

    if (isRunning) {{
        btnStart.disabled = true;
        btnStart.innerHTML = '&#9654; 主程序运行中';
        btnStop.disabled = false;
        btnStop.innerHTML = '&#9632; 停止主程序';
    }} else {{
        btnStart.disabled = false;
        btnStart.innerHTML = '&#9654; 启动主程序';
        btnStop.disabled = true;
        btnStop.innerHTML = '&#9632; 主程序未运行';
    }}
}}

function updateChart(dailyData) {{
    const wrapper = document.getElementById('chart-wrapper');
    if (!wrapper || !dailyData || dailyData.length === 0) return;

    const maxCount = Math.max(...dailyData.map(d => d.count), 1);
    wrapper.innerHTML = dailyData.map(d => {{
        const height = (d.count / maxCount * 100) || 0;
        const color = d.is_today ? '#1677ff' : '#d9d9d9';
        const labelColor = d.is_today ? '#1677ff' : '#8c8c8c';
        return '<div class="bar-item"><div class="bar-value" style="color:' + labelColor + '">' + d.count + '</div><div class="bar-track"><div class="bar-fill" style="height:' + height + '%;background:' + color + ';"></div></div><div class="bar-label">' + d.date_label + '</div></div>';
    }}).join('');
}}

function updateTable(records) {{
    const tbody = document.getElementById('records-tbody');
    if (!tbody) return;

    if (!records || records.length === 0) {{
        tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:#8c8c8c;padding:32px;">暂无回复记录</td></tr>';
        return;
    }}

    const typeLabels = {{"comment": "评论", "message": "私信"}};
    const statusLabels = {{"success": "成功", "failed": "失败", "skipped": "跳过"}};
    const statusColors = {{"success": "#52c41a", "failed": "#ff4d4f", "skipped": "#fa8c16"}};

    tbody.innerHTML = records.map(r => {{
        const ts = (r.timestamp || '').slice(0, 19);
        const author = r.author || '未知';
        const msgType = typeLabels[r.msg_type] || r.msg_type;
        const status = r.status || 'success';
        const statusLabel = statusLabels[status] || status;
        const statusColor = statusColors[status] || '#8c8c8c';
        const content = (r.content || '').slice(0, 20);
        const reply = (r.reply || '').slice(0, 30);
        const noteImg = r.note_image_url;
        const noteCell = noteImg
            ? '<img src="' + noteImg + '" class="note-thumb" onclick="window.open(\\'' + noteImg + '\\',\\'_blank\\')" title="点击查看笔记封面">'
            : '<span style="color:#bfbfbf;font-size:12px;">无</span>';

        return '<tr><td>' + ts + '</td><td>' + author + '</td><td>' + noteCell + '</td><td>' + msgType + '</td><td><span class="status-tag" style="background:' + statusColor + '15;color:' + statusColor + ';border:1px solid ' + statusColor + '40">' + statusLabel + '</span></td><td class="truncate" title="' + (r.content || '') + '">' + content + '</td><td class="truncate" title="' + (r.reply || '') + '">' + reply + '</td></tr>';
    }}).join('');
}}

function updateLogs(lines) {{
    const viewer = document.getElementById('log-viewer');
    if (!viewer) return;

    if (!lines || lines.length === 0) {{
        viewer.textContent = '（暂无日志）';
        return;
    }}

    viewer.innerHTML = lines.map(line => {{
        let cls = '';
        if (line.indexOf('| ERROR') >= 0) cls = 'log-error';
        else if (line.indexOf('| WARNING') >= 0) cls = 'log-warn';
        else if (line.indexOf('| INFO') >= 0) cls = 'log-info';
        else if (line.indexOf('| DEBUG') >= 0) cls = 'log-debug';
        return '<span class="' + cls + '">' + line.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') + '</span>';
    }}).join('\\n');
    viewer.scrollTop = viewer.scrollHeight;
}}

setInterval(updateCountdown, 1000);
setInterval(fetchData, REFRESH_INTERVAL);
fetchData();
</script>
</body>
</html>"""

    return html


# ============ HTTP 请求处理 ============

class ManagerHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器"""

    process_manager: ProcessManager = None

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._serve_html()
        elif path == "/api/stats":
            self._serve_stats()
        elif path == "/api/logs":
            self._serve_logs()
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/control/start":
            self._handle_control("start")
        elif path == "/api/control/stop":
            self._handle_control("stop")
        elif path == "/api/control/restart":
            self._handle_control("restart")
        else:
            self.send_error(404)

    def _serve_html(self):
        pm = self.process_manager
        stats, records = load_stats()
        html = generate_html(stats, records, pm.is_running, pm.pid)
        self._respond(200, html, "text/html; charset=utf-8")

    def _serve_stats(self):
        pm = self.process_manager
        stats, records = load_stats()
        api_data = {
            "is_running": pm.is_running,
            "pid": pm.pid,
            "today_count": stats["today_count"],
            "total_count": stats["total_count"],
            "success_rate": stats["success_rate"],
            "remaining_quota": stats["remaining_quota"],
            "last_updated": stats["last_updated"],
            "daily_data": stats["daily_data"],
            "records": records[:20],
        }
        self._respond(200, json.dumps(api_data, ensure_ascii=False), "application/json; charset=utf-8")

    def _serve_logs(self):
        lines = load_logs(50)
        self._respond(200, json.dumps({"lines": lines}, ensure_ascii=False), "application/json; charset=utf-8")

    def _handle_control(self, action: str):
        pm = self.process_manager
        if action == "start":
            ok, msg = pm.start()
        elif action == "stop":
            ok, msg = pm.stop()
        elif action == "restart":
            ok, msg = pm.restart()
        else:
            ok, msg = False, "未知操作"

        self._respond(200, json.dumps({"success": ok, "message": msg}, ensure_ascii=False), "application/json; charset=utf-8")

    def _respond(self, code: int, body: str, content_type: str):
        self.send_response(code)
        self.send_header("Content-type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, format, *args):
        pass


# ============ 端口查找 ============

def find_port(start: int = 8080, max_attempts: int = 10) -> int | None:
    import socket
    for port in range(start, start + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("0.0.0.0", port)) != 0:
                return port
    return None


# ============ 主入口 ============

def main():
    import argparse
    parser = argparse.ArgumentParser(description="小红书自动回复 - 统一管理中心")
    parser.add_argument("--port", type=int, default=8080, help="Web 服务端口 (默认 8080)")
    args = parser.parse_args()

    port = find_port(args.port)
    if port is None:
        print(f"错误: 端口 {args.port}-{args.port + 9} 均被占用")
        sys.exit(1)

    if port != args.port:
        print(f"端口 {args.port} 被占用，使用端口 {port}")

    pm = ProcessManager()
    ManagerHandler.process_manager = pm

    server = HTTPServer(("0.0.0.0", port), ManagerHandler)

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    print(f"""
╔══════════════════════════════════════════════╗
║     小红书自动回复 · 统一管理中心              ║
╠══════════════════════════════════════════════╣
║  操控台地址: http://localhost:{port}          ║
║  启动主程序: 在操控台点击"启动"按钮           ║
║  停止服务:   Ctrl+C                           ║
╚══════════════════════════════════════════════╝
""")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭...")
        if pm.is_running:
            pm.stop()
        server.shutdown()
        PID_FILE.unlink(missing_ok=True)
        print("管理中心已关闭")


if __name__ == "__main__":
    main()