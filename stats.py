#!/usr/bin/env python3
"""
小红书自动回复 - 数据统计 CLI 工具

用法:
    python3 stats.py              # 今日汇总 + 最近10条回复
    python3 stats.py today        # 今日所有回复明细
    python3 stats.py history      # 历史每日回复统计
    python3 stats.py all          # 所有回复记录
    python3 stats.py --html       # 生成 HTML 可视化报表
"""
import sys
import json
from pathlib import Path
from datetime import datetime, date, timedelta
from collections import Counter

# 直接读取状态文件，不依赖 StateManager（避免加载 config 的副作用）
STATE_FILE = Path("/tmp/xhs-auto-reply/processed_messages.json")


def load_data() -> dict:
    """加载状态数据，兼容旧格式"""
    if not STATE_FILE.exists():
        print("未找到状态文件，无数据可展示。")
        sys.exit(0)
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        raw = data.get("processed", {})
        processed = {}
        for msg_id, record in raw.items():
            processed[msg_id] = {
                "timestamp": record.get("timestamp", ""),
                "reply": record.get("reply", ""),
                "msg_type": record.get("msg_type", "comment"),
                "author": record.get("author", ""),
                "content": record.get("content", ""),
                "status": record.get("status", "success"),
                "mode": record.get("mode", "auto"),
            }
        return processed
    except Exception as e:
        print(f"读取状态文件失败: {e}")
        sys.exit(1)


def get_records_sorted(processed: dict, reverse: bool = True) -> list:
    """按时间排序返回记录列表"""
    records = []
    for msg_id, r in processed.items():
        r["msg_id"] = msg_id
        records.append(r)
    records.sort(key=lambda x: x.get("timestamp", ""), reverse=reverse)
    return records


def format_time(ts: str) -> str:
    """格式化时间戳为 HH:MM:SS"""
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%H:%M:%S")
    except Exception:
        return ts


def format_status(status: str) -> str:
    """格式化状态标签"""
    labels = {"success": "成功", "failed": "失败", "skipped": "跳过"}
    return labels.get(status, status)


def format_mode(mode: str) -> str:
    """格式化模式标签"""
    labels = {"auto": "自动", "review": "审核"}
    return labels.get(mode, mode)


def format_type(msg_type: str) -> str:
    """格式化类型标签"""
    labels = {"comment": "评论", "message": "私信"}
    return labels.get(msg_type, msg_type)


def print_summary(processed: dict):
    """今日汇总 + 最近10条回复"""
    today = date.today().isoformat()
    records = get_records_sorted(processed)
    today_records = [r for r in records if r.get("timestamp", "").startswith(today)]

    # 统计
    today_count = len(today_records)
    today_status = Counter(r.get("status", "success") for r in today_records)
    today_type = Counter(r.get("msg_type", "comment") for r in today_records)
    total = len(records)

    print(f"\n{'='*50}")
    print(f"  小红书自动回复 - 今日汇总 ({today})")
    print(f"{'='*50}")
    print(f"  今日回复数:   {today_count}")
    print(f"  成功/失败/跳过: {today_status.get('success', 0)} / {today_status.get('failed', 0)} / {today_status.get('skipped', 0)}")
    print(f"  剩余配额:     50 - {today_count} = {50 - today_count}")
    print(f"  历史总计:     {total} 条")
    if today_type:
        type_str = "  ".join(f"{format_type(k)}: {v}" for k, v in today_type.items())
        print(f"  按类型:       {type_str}")

    # 最近10条
    print(f"\n{'─'*50}")
    print(f"  最近 10 条回复:")
    print(f"{'─'*50}")
    recent = records[:10]
    for i, r in enumerate(recent, 1):
        time = format_time(r.get("timestamp", ""))
        status = format_status(r.get("status", "success"))
        author = r.get("author", "未知") or "未知"
        mode = format_mode(r.get("mode", "auto"))
        reply = (r.get("reply", "") or "")[:40]
        print(f"  {i:>2}. [{time}] {author} ({status}/{mode})")
        print(f"      → {reply}")
    print()


def print_today(processed: dict):
    """今日所有回复明细"""
    today = date.today().isoformat()
    records = get_records_sorted(processed)
    today_records = [r for r in records if r.get("timestamp", "").startswith(today)]

    if not today_records:
        print(f"\n今日 ({today}) 暂无回复记录。")
        return

    print(f"\n{'='*60}")
    print(f"  今日回复明细 ({today}) 共 {len(today_records)} 条")
    print(f"{'='*60}")
    print(f"{'序号':>4}  {'时间':<10} {'作者':<12} {'类型':<5} {'状态':<5} {'原文':<20} {'回复':<30}")
    print(f"{'─'*110}")
    for i, r in enumerate(today_records, 1):
        time = format_time(r.get("timestamp", ""))
        author = (r.get("author", "") or "未知")[:12]
        msg_type = format_type(r.get("msg_type", "comment"))
        status = format_status(r.get("status", "success"))
        content = (r.get("content", "") or "")[:20]
        reply = (r.get("reply", "") or "")[:30]
        print(f"{i:>4}  {time:<10} {author:<12} {msg_type:<5} {status:<5} {content:<20} {reply:<30}")
    print()


def print_history(processed: dict):
    """历史每日回复统计"""
    records = list(processed.values())
    daily_counts: dict[str, int] = {}
    daily_success: dict[str, int] = {}

    for i in range(30):
        d = (date.today() - timedelta(days=i)).isoformat()
        day_records = [r for r in records if r.get("timestamp", "").startswith(d)]
        if day_records:
            daily_counts[d] = len(day_records)
            daily_success[d] = sum(1 for r in day_records if r.get("status") == "success")

    if not daily_counts:
        print("\n最近30天无回复记录。")
        return

    # 按日期倒序排列
    sorted_days = sorted(daily_counts.keys(), reverse=True)

    print(f"\n{'='*50}")
    print(f"  历史每日回复统计（最近30天有记录的日期）")
    print(f"{'='*50}")
    print(f"  {'日期':<12} {'回复数':>6} {'成功数':>6} {'成功率':>8}")
    print(f"  {'─'*38}")
    for d in sorted_days:
        total = daily_counts[d]
        success = daily_success[d]
        rate = f"{success/total*100:.0f}%" if total > 0 else "N/A"
        print(f"  {d:<12} {total:>6} {success:>6} {rate:>8}")
    print()


def print_all(processed: dict):
    """所有回复记录"""
    records = get_records_sorted(processed)

    if not records:
        print("\n无回复记录。")
        return

    print(f"\n{'='*60}")
    print(f"  全部回复记录 共 {len(records)} 条")
    print(f"{'='*60}")
    print(f"{'序号':>4}  {'时间':<20} {'作者':<12} {'类型':<5} {'状态':<5} {'模式':<5} {'回复':<40}")
    print(f"{'─'*110}")
    for i, r in enumerate(records, 1):
        time = r.get("timestamp", "")
        author = (r.get("author", "") or "未知")[:12]
        msg_type = format_type(r.get("msg_type", "comment"))
        status = format_status(r.get("status", "success"))
        mode = format_mode(r.get("mode", "auto"))
        reply = (r.get("reply", "") or "")[:40]
        print(f"{i:>4}  {time:<20} {author:<12} {msg_type:<5} {status:<5} {mode:<5} {reply:<40}")
    print()


def generate_html(processed: dict):
    """生成 HTML 可视化报表"""
    today = date.today().isoformat()
    records = list(processed.values())

    # 统计
    today_records = [r for r in records if r.get("timestamp", "").startswith(today)]
    today_count = len(today_records)
    today_status = Counter(r.get("status", "success") for r in today_records)
    today_type = Counter(r.get("msg_type", "comment") for r in today_records)
    total = len(records)

    # 历史每日数据
    daily_data = []
    for i in range(14):
        d = (date.today() - timedelta(days=i)).isoformat()
        day_records = [r for r in records if r.get("timestamp", "").startswith(d)]
        day_count = len(day_records)
        day_success = sum(1 for r in day_records if r.get("status") == "success")
        rate = f"{day_success/day_count*100:.0f}" if day_count > 0 else "0"
        daily_data.append({"date": d, "count": day_count, "success": day_success, "rate": rate})
    daily_data.reverse()

    # 找到最大值用于柱状图缩放
    max_count = max((d["count"] for d in daily_data), default=1) or 1

    # 构建表格行
    rows_html = ""
    sorted_records = sorted(records, key=lambda x: x.get("timestamp", ""), reverse=True)
    for r in sorted_records:
        time = r.get("timestamp", "")
        author = r.get("author", "") or "未知"
        msg_type = format_type(r.get("msg_type", "comment"))
        status = format_status(r.get("status", "success"))
        mode = format_mode(r.get("mode", "auto"))
        content = (r.get("content", "") or "")[:50]
        reply = (r.get("reply", "") or "")[:80]
        status_class = r.get("status", "success")
        rows_html += f"""
        <tr>
            <td class="time">{time[:19]}</td>
            <td>{author}</td>
            <td>{msg_type}</td>
            <td class="status-{status_class}">{status}</td>
            <td class="mode">{mode}</td>
            <td class="content">{content}</td>
            <td class="reply">{reply}</td>
        </tr>"""

    # 构建柱状图
    chart_html = ""
    for d in daily_data:
        height_pct = (d["count"] / max_count * 100) if max_count > 0 else 0
        short_date = d["date"][5:]  # MM-DD
        bar_color = "#4A90D9" if d["count"] > 0 else "#E0E0E0"
        chart_html += f"""
        <div class="bar-group">
            <div class="bar" style="height: {height_pct}%; background: {bar_color};" title="{d['date']}: {d['count']}条回复"></div>
            <div class="bar-label">{short_date}</div>
            <div class="bar-value">{d['count']}</div>
        </div>"""

    success_count = today_status.get("success", 0)
    failed_count = today_status.get("failed", 0)
    skipped_count = today_status.get("skipped", 0)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>小红书自动回复 - 数据统计</title>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
        background: #f5f7fa;
        color: #333;
        line-height: 1.6;
    }}
    .container {{
        max-width: 1100px;
        margin: 0 auto;
        padding: 24px;
    }}
    h1 {{
        text-align: center;
        color: #2c3e50;
        margin-bottom: 8px;
        font-size: 24px;
    }}
    .subtitle {{
        text-align: center;
        color: #7f8c8d;
        margin-bottom: 24px;
        font-size: 14px;
    }}
    .cards {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        gap: 16px;
        margin-bottom: 32px;
    }}
    .card {{
        background: #fff;
        border-radius: 10px;
        padding: 20px;
        text-align: center;
        box-shadow: 0 2px 8px rgba(0,0,0,0.06);
    }}
    .card .value {{
        font-size: 32px;
        font-weight: 700;
        color: #4A90D9;
    }}
    .card .label {{
        font-size: 13px;
        color: #95a5a6;
        margin-top: 4px;
    }}
    .card.success .value {{ color: #27ae60; }}
    .card.warning .value {{ color: #f39c12; }}
    .card.danger .value {{ color: #e74c3c; }}
    .card.info .value {{ color: #4A90D9; }}

    .section {{
        background: #fff;
        border-radius: 10px;
        padding: 20px;
        margin-bottom: 24px;
        box-shadow: 0 2px 8px rgba(0,0,0,0.06);
    }}
    .section h2 {{
        font-size: 16px;
        color: #2c3e50;
        margin-bottom: 16px;
        padding-bottom: 8px;
        border-bottom: 2px solid #4A90D9;
    }}

    .chart-container {{
        display: flex;
        align-items: flex-end;
        gap: 8px;
        height: 200px;
        padding: 16px 0;
        border-bottom: 1px solid #eee;
    }}
    .bar-group {{
        flex: 1;
        display: flex;
        flex-direction: column;
        align-items: center;
        height: 100%;
        justify-content: flex-end;
    }}
    .bar {{
        width: 70%;
        border-radius: 4px 4px 0 0;
        min-height: 2px;
        transition: height 0.3s;
    }}
    .bar:hover {{
        opacity: 0.8;
    }}
    .bar-label {{
        font-size: 11px;
        color: #7f8c8d;
        margin-top: 6px;
    }}
    .bar-value {{
        font-size: 12px;
        font-weight: 600;
        color: #4A90D9;
        margin-bottom: 4px;
    }}

    table {{
        width: 100%;
        border-collapse: collapse;
        font-size: 13px;
    }}
    th {{
        background: #4A90D9;
        color: #fff;
        padding: 10px 8px;
        text-align: left;
        font-weight: 500;
    }}
    td {{
        padding: 8px;
        border-bottom: 1px solid #f0f0f0;
    }}
    tr:hover {{
        background: #f8fafc;
    }}
    .time {{ color: #7f8c8d; font-size: 12px; }}
    .content, .reply {{ max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .status-success {{ color: #27ae60; font-weight: 500; }}
    .status-failed {{ color: #e74c3c; font-weight: 500; }}
    .status-skipped {{ color: #f39c12; font-weight: 500; }}
    .mode {{ color: #95a5a6; }}

    .footer {{
        text-align: center;
        color: #bdc3c7;
        font-size: 12px;
        margin-top: 24px;
    }}
</style>
</head>
<body>
<div class="container">
    <h1>小红书自动回复 - 数据统计</h1>
    <div class="subtitle">生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>

    <div class="cards">
        <div class="card info">
            <div class="value">{today_count}</div>
            <div class="label">今日回复</div>
        </div>
        <div class="card success">
            <div class="value">{success_count}</div>
            <div class="label">成功</div>
        </div>
        <div class="card warning">
            <div class="value">{skipped_count}</div>
            <div class="label">跳过（审核模式）</div>
        </div>
        <div class="card danger">
            <div class="value">{failed_count}</div>
            <div class="label">失败</div>
        </div>
        <div class="card">
            <div class="value">{50 - today_count}</div>
            <div class="label">剩余配额</div>
        </div>
        <div class="card">
            <div class="value">{total}</div>
            <div class="label">历史总计</div>
        </div>
    </div>

    <div class="section">
        <h2>最近 14 天回复趋势</h2>
        <div class="chart-container">
            {chart_html}
        </div>
    </div>

    <div class="section">
        <h2>回复明细（共 {total} 条）</h2>
        <div style="overflow-x: auto;">
        <table>
            <thead>
                <tr>
                    <th>时间</th>
                    <th>作者</th>
                    <th>类型</th>
                    <th>状态</th>
                    <th>模式</th>
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

    <div class="footer">xhs-auto-reply stats report</div>
</div>
</body>
</html>"""

    output_dir = Path("/tmp/xhs-auto-reply")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "stats_report.html"
    output_path.write_text(html, encoding="utf-8")
    print(f"\nHTML 报表已生成: {output_path}")
    print(f"文件大小: {output_path.stat().st_size:,} 字节")


def main():
    processed = load_data()

    args = sys.argv[1:] if len(sys.argv) > 1 else []

    if "--html" in args:
        generate_html(processed)
    elif "today" in args:
        print_today(processed)
    elif "history" in args:
        print_history(processed)
    elif "all" in args:
        print_all(processed)
    else:
        print_summary(processed)


if __name__ == "__main__":
    main()
