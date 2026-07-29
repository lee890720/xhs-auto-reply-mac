# 部署文档

在一台新 Mac 上从零部署本系统。面向长期无人值守运行的场景（例如常年开着的 Mac mini）。

预计耗时 15 分钟，其中大部分是下载浏览器内核。

---

## 一、环境要求

| 项目 | 要求 | 检查命令 |
|---|---|---|
| 系统 | macOS 13 及以上 | `sw_vers -productVersion` |
| 芯片 | Apple Silicon 或 Intel 均可 | `uname -m` |
| Python | 3.11 及以上 | `python3 -V` |
| 磁盘 | 至少 2 GB 可用 | `df -h ~` |
| 网络 | 能访问小红书、模型服务商 API、Playwright CDN | — |

磁盘实际占用：

```
虚拟环境        195 MB
Chromium 内核   540 MB
运行数据        初始 ~45 MB，随使用缓慢增长
项目代码        316 KB
```

**没有 Python 3.11+ 的话**，先装 Homebrew 再装 Python：

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

```bash
brew install python@3.12
```

---

## 二、无人值守的系统设置（重要，先做）

这一节容易被跳过，但**不做的话重启后系统不会自己恢复**。

### 1. 开启自动登录

守护进程通过 launchd 的 **LaunchAgent** 运行，它只在用户登录到桌面后才加载。
如果 Mac 重启后停在登录界面，自动回复不会启动，而你不会收到任何提示。

**系统设置 → 用户与群组 → 自动登录** → 选择你的账号。

> 开启自动登录意味着任何能物理接触这台机器的人都能直接进入桌面。
> 放在办公室等公共环境时，请自行权衡，或改用远程唤醒后手动登录。

### 2. 阻止休眠

睡眠期间不会执行轮询。

```bash
sudo pmset -a sleep 0
```

```bash
sudo pmset -a disksleep 0
```

显示器休眠不影响运行，可以保留。

### 3. 断电恢复后自动开机

```bash
sudo pmset -a autorestart 1
```

### 4. 确认设置生效

```bash
pmset -g | grep -E "sleep|autorestart"
```

`sleep` 应为 `0`，`autorestart` 应为 `1`。

### 5. 远程管理（可选，无显示器时必需）

**系统设置 → 通用 → 共享** → 打开「屏幕共享」和「远程登录」。
之后可以从另一台 Mac 用访达的「前往 → 连接服务器」输入 `vnc://这台机器的IP` 接入。

首次扫码登录小红书需要图形界面，用屏幕共享完成即可。

---

## 三、部署

### 1. 取得代码

```bash
git clone <仓库地址> ~/xhs-auto-reply
```

没有 Git 仓库就直接拷贝整个项目目录过去，位置随意，但**部署后不要再移动**——
launchd 配置和 App 启动器里写的是绝对路径。

### 2. 运行安装脚本

```bash
cd ~/xhs-auto-reply && bash scripts/install.sh
```

脚本会依次完成：

1. 创建虚拟环境 `.venv` 并安装依赖
2. 下载 Chromium 内核（约 540 MB，最慢的一步）
3. 注册 launchd 开机自启，并立即启动守护进程
4. 在 `~/Applications` 生成菜单栏 App 和图标
5. 等待服务就绪，自动打开控制台

结尾出现下面这段就算成功：

```
✓ 守护进程运行中: http://127.0.0.1:8787
```

脚本可以重复执行，不会重复下载已有的内容。

### 3. 打开菜单栏 App

Launchpad 里搜「小红书自动回复」并点击。图标 `书 ○` 会出现在**屏幕右上角菜单栏**。

这个 App 不占 Dock，点了之后 Dock 没反应是正常的，去菜单栏找。

---

## 四、首次配置

### 1. 填写模型 API

控制台 → **设置 → 模型 API**：

| 字段 | 说明 |
|---|---|
| 服务商 | DeepSeek / 阿里百炼 / TokenHub / 自定义 |
| API Key | 服务商控制台生成，只存本机 |
| Base URL | 选了 DeepSeek 或百炼可留空，其余必填 |
| 模型 | **必填**，去服务商控制台查你账号开通了哪些 |

填完点「测试连接」，出现 `连接成功 · 模型名 · 返回「正常」` 才算通。

### 2. 登录小红书

菜单栏 → **扫码登录小红书** → 在弹出的浏览器窗口扫码 → 回控制台点「重新检测」。

登录态保存在 `~/Library/Application Support/XHSAutoReply/browser/`，除非被平台踢下线，否则不用重复登录。

### 3. 检查回复人设

**设置 → 回复人设**。默认人设是通用的，按你的账号定位改写。改完立即生效。

### 4. 开始监控

菜单栏 → **开始监控**。

> **首轮会把通知页上的现有评论全部当作新消息处理。**
> 如果账号积压了很多未回复评论，建议先在「设置 → 回复策略」里保持**审核模式**，
> 确认草稿质量后再切全自动。

---

## 五、部署验证清单

逐条确认，全绿才算部署完成：

```bash
# 1. 守护进程在运行
launchctl print gui/$UID/com.xhs.autoreply.daemon | grep -m1 "state ="
```
期望 `state = running`

```bash
# 2. 控制台可访问
curl -s http://127.0.0.1:8787/api/status
```
期望返回 JSON，且 `"llm_configured": true`

```bash
# 3. 模型 API 通
curl -s -X POST http://127.0.0.1:8787/api/llm/test
```
期望 `"success": true`

```bash
# 4. 菜单栏 App 在跑
pgrep -f "menubar/app.py" > /dev/null && echo "运行中" || echo "未运行"
```

```bash
# 5. 浏览器内核就位
.venv/bin/python -c "
from playwright.sync_api import sync_playwright
import os
with sync_playwright() as p:
    print('OK' if os.path.exists(p.chromium.executable_path) else '缺失')"
```

6. 菜单栏能看到 `书 ○` 图标，点开菜单项齐全
7. 引擎启动后状态变为 `监控中`，不是 `需要扫码登录`

---

## 六、要不要开自动恢复监控

**设置 → 系统 → 守护进程启动时自动开始监控**

launchd 只保证**进程**活着，不保证**引擎**在工作。不开这个开关时，以下情况会静悄悄停止回复：

- 系统更新后重启
- 断电恢复后开机
- 守护进程崩溃被拉起

无人值守场景建议**开启**。各项安全闸门依然生效：登录态失效时停在待登录状态不会乱操作，
每日上限、夜间静默、审核模式都照常。

代价是：开机即自动开始抓取和回复，全自动模式下没有人工确认环节。

---

## 七、从旧机迁移

想保留配置和登录态、不重新扫码，把数据目录整个搬过去。

**旧机上**（先停服务）：

```bash
launchctl bootout gui/$UID/com.xhs.autoreply.daemon
```

```bash
cd ~/Library/Application\ Support && tar czf ~/Desktop/xhs-data.tgz XHSAutoReply
```

**新机上**（先完成第三节的部署，但**不要启动引擎**）：

```bash
launchctl bootout gui/$UID/com.xhs.autoreply.daemon
```

```bash
cd ~/Library/Application\ Support && rm -rf XHSAutoReply && tar xzf ~/Desktop/xhs-data.tgz
```

```bash
chmod 700 ~/Library/Application\ Support/XHSAutoReply
```

```bash
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.xhs.autoreply.daemon.plist
```

打开控制台确认 API Key 和历史记录都在，登录态是否有效点「重新检测」验证。

压缩包里含 API Key 和登录凭据，传输后请删除。

---

## 八、日常运维

```bash
# 看守护进程状态
launchctl print gui/$UID/com.xhs.autoreply.daemon | head -20
```

```bash
# 重启守护进程（菜单栏里也有这个入口）
launchctl kickstart -k gui/$UID/com.xhs.autoreply.daemon
```

```bash
# 实时看日志
tail -f ~/Library/Application\ Support/XHSAutoReply/logs/xhsd.log
```

```bash
# 看崩溃信息（进程起不来时看这个）
tail -50 ~/Library/Application\ Support/XHSAutoReply/logs/daemon.err.log
```

```bash
# 前台调试运行（先停掉 launchd 实例，否则会被单实例锁拦下）
launchctl bootout gui/$UID/com.xhs.autoreply.daemon && .venv/bin/python -m xhsd
```

### 更新代码后

```bash
git pull && launchctl kickstart -k gui/$UID/com.xhs.autoreply.daemon
```

依赖有变动时重跑 `bash scripts/install.sh`。

### 清理历史数据

控制台 → 设置 → 系统 → 「清理 90 天前的历史数据」。

旧版 Playwright 留下的浏览器内核可以手动删，能省几百 MB：

```bash
ls ~/Library/Caches/ms-playwright/
```

保留版本号最新的 `chromium-*` 和 `chromium_headless_shell-*`，其余可删。

---

## 九、故障排查

### 点了 App 图标没反应

先看屏幕右上角菜单栏，图标可能已经在那儿了——这个 App 不占 Dock。
确实没有就看 `logs/menubar.log`。

改动菜单栏代码时注意两个已规避的坑，别踩回去：

- `rumps.App(name=...)` 的 name 必须是 ASCII，用中文会静默不创建菜单栏项
- `launcher` 脚本必须后台启动 python 再自己退出。用 `exec` 会让 python 成为
  bundle 主进程，这种情况下 WindowServer 不给它创建菜单栏项

### 菜单栏显示 `书 ✕`

守护进程没跑起来。看 `logs/daemon.err.log`，常见原因：

- 项目目录被移动过（plist 里是绝对路径）→ 重跑 `bash scripts/install.sh`
- 端口 8787 被占用 → 会自动顺延到 8788，菜单栏读 `runtime.json` 自动适配

### 启动引擎报 `Executable doesn't exist`

Chromium 内核缺失或版本不匹配：

```bash
.venv/bin/python -m playwright install chromium
```

### 一直提示需要扫码

登录态过期。菜单栏 →「扫码登录小红书」→ 扫完点「重新检测」。

若浏览器窗口没弹出，检查是否开了自动登录、当前是否有图形界面会话（SSH 进去是没有的，要用屏幕共享）。

### 回复一直不发出去

按顺序排查：

1. 是不是**审核模式**——草稿要手动点「批准并发送」
2. 是不是到了**每日上限**——概览页能看到 `今日 N/50`
3. 是不是在**夜间静默时段**——默认 23:00–08:00
4. 队列是不是在正常消化——发送间隔 8–30 秒随机，5 条要 1–2 分钟，
   状态栏会显示 `待发 N`

### 找不到页面元素 / 回复发送失败

小红书改版了。`~/Library/Application Support/XHSAutoReply/screenshots/` 里有失败现场截图，
据此调整 `xhsd/collector.py` 里的选择器。

采集的过滤依据是「条目内有没有 `.action-reply`」，改版后若该类名变化，
会表现为一条评论都采不到。

### 系统重启后不工作了

多半是本文第二节没做：

- 没开自动登录 → 卡在登录界面，LaunchAgent 根本没加载
- 没关休眠 → 睡眠期间不轮询
- 没开「守护进程启动时自动开始监控」→ 进程活着但引擎是停止状态

---

## 十、卸载

```bash
bash scripts/uninstall.sh
```

停服务、移除开机自启和菜单栏 App，**数据保留**。

连数据一起删（含 API Key 和登录态，不可恢复）：

```bash
bash scripts/uninstall.sh --purge
```

浏览器内核是全局缓存，不随卸载删除，需要的话手动清 `~/Library/Caches/ms-playwright/`。
