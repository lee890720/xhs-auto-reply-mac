#!/bin/bash
# 卸载：停守护进程、移除 launchd 配置与菜单栏 App。默认保留数据。
set -uo pipefail

LABEL="com.xhs.autoreply.daemon"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
APP_DIR="$HOME/Applications/小红书自动回复.app"
DATA_DIR="$HOME/Library/Application Support/XHSAutoReply"

echo
echo "  卸载小红书自动回复"
echo

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null && echo "  ✓ 守护进程已停止" || echo "  · 守护进程未运行"
rm -f "$PLIST" && echo "  ✓ 已移除开机自启配置"
pkill -f "menubar/app.py" 2>/dev/null && echo "  ✓ 菜单栏 App 已退出" || true
rm -rf "$APP_DIR" && echo "  ✓ 已移除菜单栏 App"

echo
if [ "${1:-}" = "--purge" ]; then
    rm -rf "$DATA_DIR"
    echo "  ✓ 已删除全部数据（数据库、日志、浏览器登录态）"
else
    echo "  数据保留在: $DATA_DIR"
    echo "  彻底删除请执行: bash scripts/uninstall.sh --purge"
fi
echo
