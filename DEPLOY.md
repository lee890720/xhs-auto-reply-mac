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

改写时有两处别删掉：

- **具体的负向约束**（不准以「哈哈」开头、不准每条都带 emoji、不准用「～」、
  不准叫对方昵称）。写成「语气自然」这种抽象要求时，模型给出的是它以为的自然，
  实测是十条里八条以「哈哈」开头、条条带 emoji。
- **`[SKIP]` 相关的规则**。它是模型的弃答开关：辱骂、贬称反问、引战立场、
  质疑你是机器人、只有 `[图片]` 的评论都靠它拦下来转人工。删掉之后这些评论会被
  硬编出一句得体回复，结果通常是「哈哈，欢迎你呀」这种自证愚蠢的答复。
- **「不知道的事情不要编」那一节**。删掉之后模型会替你编场馆名、场地规则、
  个人水平、当天日程 —— 都是别人会当真、你又交不出来的东西。

人设里的禁令只是概率约束。真正不能出现在回复里的词，填到
**设置 → 安全策略 → 回复里的禁用词**，那一项由代码强制执行。

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

依赖有变动时重跑 `bash scripts/install.sh`。数据库表结构的变动在启动时自动完成，
不用手动处理，历史数据不受影响。

**注意：代码里的默认回复人设不会覆盖你已保存的那一份。** 配置存在数据库里，
始终以控制台里填的为准——这是有意的，否则每次升级都会冲掉你的改写。
代价是人设的改进不会自动生效，升级后回复语气还是老样子。

要用新版默认人设覆盖，先备份现有的：

```bash
.venv/bin/python -c "from xhsd import settings_store as s; print(s.load().persona.system_prompt)" > ~/Desktop/persona-backup.txt
```

再覆盖（改完立即生效，不用重启）：

```bash
.venv/bin/python -c "from xhsd import settings_store as s; s.save_section('persona', {'system_prompt': s.DEFAULT_PERSONA})"
```

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

### 点启动后报「Chromium 浏览器内核未安装」

重装内核即可，登录态和历史数据都不受影响：

```bash
.venv/bin/python -m playwright install chromium
```

内核装在 `~/Library/Caches/ms-playwright/`。**这是系统缓存目录，各类清理工具
都会清它** —— 实际发生过一次：`~/Library/Caches/` 下别的缓存都在，唯独
`ms-playwright` 整个目录没了，系统随即起不来。手动清理磁盘时也容易误删。

装完之后第一次启动，浏览器 profile 没有缓存，通知页会加载得比平时慢很多，
日志里可能出现一条「等待通知条目渲染超时」的警告，属正常，下一轮就好。

### 一直提示需要扫码

登录态过期。菜单栏 →「扫码登录小红书」→ 扫完点「重新检测」。

若浏览器窗口没弹出，检查是否开了自动登录、当前是否有图形界面会话（SSH 进去是没有的，要用屏幕共享）。

引擎在待登录状态下每 5 轮会自己重检一次（默认轮询 120 秒，即约 10 分钟一次），
日志里是「自动重新检测登录态」。所以如果只是误判或者登录态自己恢复了，
不用管它也会自动继续；人工点「重新检测」则是立刻生效。

### 回复一直不发出去

按顺序排查：

1. 是不是**审核模式**——草稿要手动点「批准并发送」
2. 是不是到了**每日上限**——概览页能看到 `今日 N/50`
3. 是不是在**夜间静默时段**——默认 23:00–08:00
4. 队列是不是在正常消化——发送间隔 8–30 秒随机，5 条要 1–2 分钟，
   状态栏会显示 `待发 N`

### 有不少评论一条都没回

先看「敏感词拦截」筛选里有没有堆积。除了命中敏感词，模型判断不宜回复的评论
也会进这个队列（`error` 写着「模型判断不宜回复」），包括辱骂、贬称反问、
引战立场、质疑你是机器人、只有 `[图片]` `[视频]` 的评论。

**这个比例可能不低。** 实测一个评论区有争议内容的账号，22 条里 12 条弃答。
这不是故障——强行回复这些评论比不回更糟。逐条人工处理，或者觉得太严就在
人设里放松 `[SKIP]` 的判定范围。

### 找不到页面元素 / 回复发送失败

小红书改版了。`~/Library/Application Support/XHSAutoReply/screenshots/` 里有失败现场截图，
据此调整 `xhsd/collector.py` 里的选择器。

采集的过滤依据是「条目内有没有 `.action-reply`」，改版后若该类名变化，
会表现为一条评论都采不到。

### 回复变得泛泛而谈、开始跑题

说明笔记上下文没拿到。日志里会有这两条之一：

- `N 条评论未能对上 mentions 接口数据` —— 通知页的
  `/api/sns/web/v1/you/mentions` 接口变了（路径、字段名，或返回顺序与页面
  渲染顺序不再一致）。见 `xhsd/collector.py` 的 `_absorb_mentions`
  和 `_match_meta`。
- `抓取笔记 xxx 失败` —— 笔记详情页的 `__INITIAL_STATE__` 结构变了，
  见 `fetch_note`。注意不能改用 `.title` 这类选择器抓 DOM，explore 页右侧
  有推荐笔记流，会命中别人的笔记。

审核队列里每条评论上方会显示它所属的笔记，没有笔记内容时显示
「这篇笔记没取到文字内容」，据此判断是全局失效还是个别笔记的问题。

笔记内容缓存在 `notes` 表，抓成功过就不再抓。改完选择器要重试某篇笔记，
清掉它的缓存行即可：

```bash
sqlite3 ~/Library/Application\ Support/XHSAutoReply/data.db "DELETE FROM notes WHERE note_id='粘贴note_id';"
```

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
