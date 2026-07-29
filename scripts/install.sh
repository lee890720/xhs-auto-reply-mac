#!/bin/bash
# 一键安装：虚拟环境 → 浏览器内核 → launchd 守护进程 → 菜单栏 App
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$PROJECT_DIR/.venv"
PYTHON="$VENV/bin/python"
DATA_DIR="$HOME/Library/Application Support/XHSAutoReply"
LOG_DIR="$DATA_DIR/logs"
AGENT_DIR="$HOME/Library/LaunchAgents"
LABEL="com.xhs.autoreply.daemon"
PLIST="$AGENT_DIR/$LABEL.plist"
APP_DIR="$HOME/Applications/小红书自动回复.app"

info()  { printf "\033[34m▸\033[0m %s\n" "$1"; }
ok()    { printf "\033[32m✓\033[0m %s\n" "$1"; }
warn()  { printf "\033[33m!\033[0m %s\n" "$1"; }

echo
echo "  小红书自动回复 · 安装"
echo "  项目目录: $PROJECT_DIR"
echo

# ---------- 1. Python 虚拟环境 ----------
if [ ! -x "$PYTHON" ]; then
    BASE_PYTHON=""
    for candidate in /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3 /usr/bin/python3; do
        if [ -x "$candidate" ]; then BASE_PYTHON="$candidate"; break; fi
    done
    if [ -z "$BASE_PYTHON" ]; then
        echo "未找到 Python 3，请先安装：brew install python@3.12" >&2
        exit 1
    fi
    info "创建虚拟环境（$BASE_PYTHON）"
    "$BASE_PYTHON" -m venv "$VENV"
fi

info "安装依赖"
"$VENV/bin/pip" -q install --upgrade pip
"$VENV/bin/pip" -q install playwright httpx fastapi uvicorn pydantic loguru rumps
ok "依赖就绪"

# ---------- 2. 浏览器内核 ----------
if [ ! -d "$HOME/Library/Caches/ms-playwright" ]; then
    info "下载 Chromium（约 150MB，仅首次）"
    "$PYTHON" -m playwright install chromium
fi
ok "浏览器内核就绪"

mkdir -p "$DATA_DIR" "$LOG_DIR" "$AGENT_DIR" "$HOME/Applications"
chmod 700 "$DATA_DIR"          # 数据库里有 API Key，收紧权限

# ---------- 3. launchd 守护进程 ----------
info "配置开机自启"
sed -e "s|__PYTHON__|$PYTHON|g" \
    -e "s|__PROJECT__|$PROJECT_DIR|g" \
    -e "s|__LOGDIR__|$LOG_DIR|g" \
    "$PROJECT_DIR/launchd/$LABEL.plist.template" > "$PLIST"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"
ok "守护进程已注册（开机自启 + 崩溃自动重启）"

# ---------- 4. 菜单栏 App ----------
info "生成菜单栏 App"
rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"

cat > "$APP_DIR/Contents/Info.plist" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>小红书自动回复</string>
    <key>CFBundleDisplayName</key><string>小红书自动回复</string>
    <key>CFBundleIdentifier</key><string>com.xhs.autoreply.menubar</string>
    <key>CFBundleVersion</key><string>2.0.0</string>
    <key>CFBundleShortVersionString</key><string>2.0.0</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleExecutable</key><string>launcher</string>
    <key>CFBundleIconFile</key><string>icon</string>
    <!-- 只驻留菜单栏，不占用 Dock -->
    <key>LSUIElement</key><true/>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST_EOF

cat > "$APP_DIR/Contents/MacOS/launcher" <<LAUNCHER_EOF
#!/bin/bash
# 启动失败时这个日志是唯一线索——GUI 启动的进程没有终端可看
exec >> "$LOG_DIR/menubar.log" 2>&1
echo "--- \$(date '+%F %T') 启动菜单栏 App ---"

# 必须后台启动再让 launcher 自己退出，不能用 exec。
# 用 exec 时 python 会成为 .app bundle 的主进程，这种情况下 WindowServer
# 不会给它创建菜单栏项——进程正常运行，但图标永远不出现。
# 重复启动由 app.py 内的文件锁拦截。
"$PYTHON" "$PROJECT_DIR/menubar/app.py" &
disown
exit 0
LAUNCHER_EOF
chmod +x "$APP_DIR/Contents/MacOS/launcher"

"$PYTHON" "$PROJECT_DIR/scripts/make_icon.py" "$APP_DIR/Contents/Resources/icon.icns" >/dev/null
touch "$APP_DIR"      # 让 Finder 刷新图标缓存
ok "菜单栏 App: $APP_DIR"

# ---------- 5. 等待就绪 ----------
info "等待守护进程启动"
URL=""
for _ in $(seq 1 20); do
    sleep 1
    if [ -f "$DATA_DIR/runtime.json" ]; then
        URL=$("$PYTHON" -c "import json,sys;print(json.load(open(sys.argv[1]))['url'])" "$DATA_DIR/runtime.json" 2>/dev/null || echo "")
        if [ -n "$URL" ] && curl -sf "$URL/api/status" >/dev/null 2>&1; then break; fi
    fi
done

echo
if [ -n "$URL" ]; then
    ok "守护进程运行中: $URL"
else
    warn "守护进程尚未响应，稍后可查看日志: $LOG_DIR/daemon.err.log"
    URL="http://127.0.0.1:8787"
fi

cat <<SUMMARY

  安装完成
  ─────────────────────────────────────────────
  控制台      $URL
  菜单栏 App  $APP_DIR
  数据目录    $DATA_DIR

  接下来：
    1. 打开菜单栏 App（Launchpad 里搜「小红书自动回复」）
    2. 在控制台「设置」里填写模型 API Key
    3. 点菜单栏的「扫码登录小红书」完成登录
    4. 点「开始监控」

  卸载：bash scripts/uninstall.sh

SUMMARY

open "$APP_DIR" 2>/dev/null || true
open "$URL" 2>/dev/null || true
