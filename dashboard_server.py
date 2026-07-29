#!/usr/bin/env python3
"""
小红书自动回复 - 运维看板
基于 Python 内置 http.server，零第三方依赖
"""

import json
import os
from datetime import datetime, date, timedelta
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

STATE_FILE = Path("/tmp/xhs-auto-reply/processed_messages.json")


def load_stats():
    """读取 JSON 并计算统计数据"""
    if not STATE_FILE.exists():
        return {
            "is_running": False,
            "today_count": 0,
            "total_count": 0,
            "success_rate": 0,
            "remaining_quota": 50,
            "last_updated": None,
        }, []

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {
            "is_running": False,
            "today_count": 0,
            "total_count": 0,
            "success_rate": 0,
            "remaining_quota": 50,
            "last_updated": None,
        }, []

    raw = data.get("processed", {})
    last_updated = data.get("last_updated", "")

    # 解析记录
    records = []
    for msg_id, record in raw.items():
        ts = record.get("timestamp", "")
        status = record.get("status", "success")
        msg_type = record.get("msg_type", "comment")
        mode = record.get("mode", "auto")

        # 作者：优先用 author 字段，否则从 msg_id 解析
        author = record.get("author", "")
        if not author and "_" in msg_id:
            parts = msg_id.split("_")
            # comment_author_xxx 或 message_author_xxx
            if len(parts) >= 3:
                author = parts[1]

        note_image_url = record.get("note_image_url", "")
        records.append({
            "msg_id": msg_id,
            "timestamp": ts,
            "reply": record.get("reply", ""),
            "content": record.get("content", ""),
            "author": author or "未知",
            "msg_type": msg_type,
            "status": status,
            "mode": mode,
            "note_image_url": note_image_url,
        })

    # 按时间倒序
    records.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

    today_str = date.today().isoformat()
    today_records = [r for r in records if r.get("timestamp", "").startswith(today_str)]

    today_count = len(today_records)
    total_count = len(records)
    today_success = sum(1 for r in today_records if r.get("status") == "success")
    success_rate = round(today_success / today_count * 100, 1) if today_count > 0 else 0
    remaining_quota = max(0, 50 - today_count)

    # 运行状态判断
    is_running = False
    if last_updated:
        try:
            lu = datetime.fromisoformat(last_updated)
            if (datetime.now() - lu).total_seconds() < 120:
                is_running = True
        except Exception:
            pass

    # 最近 7 天数据
    daily_data = []
    for i in range(6, -1, -1):
        d = (date.today() - timedelta(days=i)).isoformat()
        day_records = [r for r in records if r.get("timestamp", "").startswith(d)]
        daily_data.append({
            "date": d,
            "date_label": d[5:],  # MM-DD
            "count": len(day_records),
            "is_today": i == 0,
        })

    stats = {
        "is_running": is_running,
        "today_count": today_count,
        "total_count": total_count,
        "success_rate": success_rate,
        "remaining_quota": remaining_quota,
        "last_updated": last_updated,
        "daily_data": daily_data,
    }

    return stats, records


def generate_html(stats, records):
    """生成完整 HTML 页面"""
    is_running = stats["is_running"]
    today_count = stats["today_count"]
    total_count = stats["total_count"]
    success_rate = stats["success_rate"]
    remaining_quota = stats["remaining_quota"]
    last_updated = stats["last_updated"] or "无"
    daily_data = stats["daily_data"]

    status_dot = "#52c41a" if is_running else "#ff4d4f"
    status_text = "运行中" if is_running else "未运行"

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

    # 最近回复列表（最多20条）
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

        rows_html += f"""
            <tr>
                <td>{ts}</td>
                <td>{author}</td>
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
<title>小红书自动回复 · 运维看板</title>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif;
        background: #f5f5f5;
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

    /* 顶部栏 */
    .header {{
        background: #fff;
        border-radius: 12px;
        padding: 20px 24px;
        margin-bottom: 16px;
        box-shadow: 0 2px 8px rgba(0,0,0,0.06);
        display: flex;
        align-items: center;
        justify-content: space-between;
        flex-wrap: wrap;
        gap: 12px;
    }}
    .header-left {{
        display: flex;
        align-items: center;
        gap: 12px;
    }}
    .header-title {{
        font-size: 20px;
        font-weight: 600;
        color: #1677ff;
    }}
    .header-right {{
        display: flex;
        align-items: center;
        gap: 16px;
        font-size: 13px;
        color: #8c8c8c;
    }}
    .status-indicator {{
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 13px;
        font-weight: 500;
        color: #262626;
    }}
    .status-dot {{
        width: 8px;
        height: 8px;
        border-radius: 50%;
        background: {status_dot};
        box-shadow: 0 0 0 3px {status_dot}33;
        animation: pulse 2s infinite;
    }}
    @keyframes pulse {{
        0%, 100% {{ opacity: 1; }}
        50% {{ opacity: 0.6; }}
    }}

    /* 数据卡片 */
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

    /* 图表 */
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

    /* 表格 */
    .table-wrapper {{
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
    }}
    table {{
        width: 100%;
        border-collapse: collapse;
        font-size: 13px;
        min-width: 500px;
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

    /* 底部 */
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
        font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    }}
</style>

</head>
<body>
<div class="container">
    <div class="header">
        <div class="header-left">
            <div class="header-title">小红书自动回复 · 运维看板</div>
        </div>
        <div class="header-right">
            <div class="status-indicator">
                <span class="status-dot"></span>
                <span class="status-text">{status_text}</span>
            </div>
            <span>最后更新: <span id="last-updated">{last_updated}</span></span>
        </div>
    </div>

    <div class="cards-grid">
        <div class="card">
            <div class="card-value">{today_count}</div>
            <div class="card-label">今日回复数</div>
        </div>
        <div class="card">
            <div class="card-value">{total_count}</div>
            <div class="card-label">总回复数</div>
        </div>
        <div class="card card-success">
            <div class="card-value">{success_rate}%</div>
            <div class="card-label">成功率</div>
        </div>
        <div class="card">
            <div class="card-value">{remaining_quota}</div>
            <div class="card-label">剩余配额</div>
        </div>
    </div>

    <div class="section">
        <div class="section-title">最近 7 天回复趋势</div>
        <div class="chart-wrapper">
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
                <tbody>
                    {rows_html}
                </tbody>
            </table>
        </div>
    </div>

    <div class="footer">
        <div>数据文件: <code>{STATE_FILE}</code></div>
        <div>每 15 秒自动刷新 · 下次刷新: <span id="countdown">15s</span></div>
    </div>
</div>
</body>
</html>"""

    # AJAX JavaScript (separate string to avoid f-string / JS template literal conflict)
    ajax_js = """
<script>
// 每 15 秒自动刷新数据（无页面刷新，AJAX 更新）
const REFRESH_INTERVAL = 15000;
let countdown = REFRESH_INTERVAL / 1000;

function updateCountdown() {
    const el = document.getElementById('countdown');
    if (el) el.textContent = countdown + 's';
    countdown--;
    if (countdown < 0) countdown = REFRESH_INTERVAL / 1000;
}

async function fetchData() {
    try {
        const resp = await fetch('/api/stats');
        const data = await resp.json();

        // 更新状态指示器
        const statusDot = document.querySelector('.status-dot');
        const statusText = document.querySelector('.status-text');
        if (statusDot && statusText) {
            if (data.is_running) {
                statusDot.style.background = '#52c41a';
                statusDot.style.boxShadow = '0 0 0 3px #52c41a33';
                statusDot.style.animation = 'pulse 2s infinite';
                statusText.textContent = '运行中';
                statusText.style.color = '#52c41a';
            } else {
                statusDot.style.background = '#ff4d4f';
                statusDot.style.boxShadow = '0 0 0 3px #ff4d4f33';
                statusDot.style.animation = 'none';
                statusText.textContent = '未运行';
                statusText.style.color = '#ff4d4f';
            }
        }

        // 更新最后更新时间
        const lastUpdated = document.getElementById('last-updated');
        if (lastUpdated) lastUpdated.textContent = data.last_updated || '无';

        // 更新数据卡片
        const cards = document.querySelectorAll('.card-value');
        if (cards.length >= 4) {
            cards[0].textContent = data.today_count;
            cards[1].textContent = data.total_count;
            cards[2].textContent = data.success_rate + '%';
            cards[3].textContent = data.remaining_quota;
        }

        // 更新柱状图
        updateChart(data.daily_data);

        // 更新表格
        updateTable(data.records);

        // 重置倒计时
        countdown = REFRESH_INTERVAL / 1000;
    } catch (e) {
        console.error('刷新失败:', e);
    }
}

function updateChart(dailyData) {
    const chartWrapper = document.querySelector('.chart-wrapper');
    if (!chartWrapper) return;

    const maxCount = Math.max(...dailyData.map(d => d.count), 1);
    chartWrapper.innerHTML = dailyData.map(d => {
        const height = (d.count / maxCount * 100) || 0;
        const color = d.is_today ? '#1677ff' : '#d9d9d9';
        const labelColor = d.is_today ? '#1677ff' : '#8c8c8c';
        return `
            <div class="bar-item">
                <div class="bar-value" style="color:${labelColor}">${d.count}</div>
                <div class="bar-track">
                    <div class="bar-fill" style="height:${height}%;background:${color};"></div>
                </div>
                <div class="bar-label">${d.date_label}</div>
            </div>
        `;
    }).join('');
}

function updateTable(records) {
    const tbody = document.querySelector('tbody');
    if (!tbody) return;

    if (!records || records.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:#8c8c8c;padding:32px;">暂无回复记录</td></tr>';
        return;
    }

    const typeLabels = {"comment": "评论", "message": "私信"};
    const statusLabels = {"success": "成功", "failed": "失败", "skipped": "跳过"};
    const statusColors = {"success": "#52c41a", "failed": "#ff4d4f", "skipped": "#fa8c16"};

    tbody.innerHTML = records.map(r => {
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
            ? `<img src="${noteImg}" style="width:48px;height:48px;object-fit:cover;border-radius:6px;cursor:pointer;" onclick="window.open('${noteImg}','_blank')" title="点击查看笔记封面">`
            : '<span style="color:#bfbfbf;font-size:12px;">无</span>';

        return `
            <tr>
                <td>${ts}</td>
                <td>${author}</td>
                <td>${noteCell}</td>
                <td>${msgType}</td>
                <td><span class="status-tag" style="background:${statusColor}15;color:${statusColor};border:1px solid ${statusColor}40">${statusLabel}</span></td>
                <td class="truncate" title="${r.content || ''}">${content}</td>
                <td class="truncate" title="${r.reply || ''}">${reply}</td>
            </tr>
        `;
    }).join('');
}

// 启动定时刷新
setInterval(updateCountdown, 1000);
setInterval(fetchData, REFRESH_INTERVAL);

// 页面加载后立即获取一次
fetchData();
</script>
"""

    # Insert the script tag right before </body>
    html = html.replace("</body>", ajax_js + "\n</body>")
    return html


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            stats, records = load_stats()
            html = generate_html(stats, records)
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
        elif self.path == "/api/stats":
            stats, records = load_stats()
            api_data = {
                "is_running": stats["is_running"],
                "today_count": stats["today_count"],
                "total_count": stats["total_count"],
                "success_rate": stats["success_rate"],
                "remaining_quota": stats["remaining_quota"],
                "last_updated": stats["last_updated"],
                "daily_data": stats["daily_data"],
                "records": records[:20],
            }
            self.send_response(200)
            self.send_header("Content-type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(api_data, ensure_ascii=False).encode("utf-8"))
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        # 静默日志或简化输出
        pass


def find_port(start=8080, max_attempts=10):
    """尝试寻找可用端口"""
    import socket
    for port in range(start, start + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("0.0.0.0", port)) != 0:
                return port
    return None


if __name__ == "__main__":
    port = find_port(8080)
    if port is None:
        print("错误: 找不到可用端口 (8080-8089 均被占用)")
        exit(1)

    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    print(f"Dashboard running at http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        server.shutdown()
