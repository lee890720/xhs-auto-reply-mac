# 小红书评论自动回复系统

本地运行的小红书评论自动回复工具。Playwright 驱动浏览器采集评论，调用大模型 API 生成回复，
人工审核放行后自动发送。为在 Mac mini 上长期无人值守运行而设计。

```
        ┌──────────────────────────────┐
        │  菜单栏 App                    │   书 ● 3
        │  状态 / 启停 / 打开控制台        │
        └───────────┬──────────────────┘
                    │ HTTP · launchctl
        ┌───────────▼──────────────────┐
        │  xhsd 守护进程（launchd 托管）  │   开机自启 · 崩溃自愈 · 单实例锁
        │  ┌────────────────────────┐  │
        │  │ FastAPI   控制台 + API  │  │
        │  │ Scheduler 轮询/限流/静默 │  │
        │  │ Collector Playwright   │  │
        │  │ Generator  人设 + 清洗   │──┼──→ DeepSeek / 百炼 / TokenHub
        │  │ SQLite    消息/回复/配置 │  │
        │  └────────────────────────┘  │
        └──────────────────────────────┘
```

## 安装

```bash
bash scripts/install.sh
```

脚本会创建虚拟环境、下载 Chromium、注册 launchd 开机自启、在 `~/Applications` 生成菜单栏 App，
然后自动打开控制台。全程约 15 分钟，绝大部分时间在下载浏览器内核。

> **在新机器上首次部署，请看 [DEPLOY.md](DEPLOY.md)。**
> 无人值守运行还需要配置自动登录和禁止休眠——不做的话重启后系统不会自己恢复，
> 而且不会有任何提示。

## 首次配置

1. **填模型 API** — 控制台「设置 → 模型 API」，选服务商、粘贴 API Key，点「测试连接」确认通
2. **登录小红书** — 点菜单栏图标 →「扫码登录小红书」，在弹出的浏览器窗口扫码
3. **开始监控** — 点菜单栏 →「开始监控」

默认是**审核模式**：只生成草稿，你在控制台点「批准并发送」后才会真的发出去。
建议先这样跑一周，确认回复质量后再切全自动。

## 支持的模型服务商

| 服务商 | Base URL | 说明 |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | 性价比高，中文口语自然，推荐 `deepseek-chat` |
| 阿里百炼 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 国内延迟低，`qwen-plus` / `qwen-turbo` |
| TokenHub | 需手动填写 | 填控制台给出的 OpenAI 兼容端点 |
| 自定义 | 需手动填写 | 任何 `/chat/completions` 兼容服务，含本地 Ollama |

切换服务商只需改配置，不用动代码。

## 控制台

默认地址 `http://127.0.0.1:8787`，只监听本机。

- **概览** — 今日发送量、待审数、成功率、14 天趋势、互动最多的用户
- **审核队列** — 逐条查看评论原文和 AI 草稿，可直接编辑后批准，或重新生成、拒绝
- **设置** — 模型 API、回复人设、发送节奏、敏感词，改完立即生效，不用重启
- **日志** — 结构化运行日志

## 回复状态流转

```
采集到评论 ──→ 生成草稿 ──┬─→ 审核模式 ─→ 待审核 ─┬─→ 批准 ─→ 待发送 ─→ 已发送
                        │                    └─→ 拒绝
                        └─→ 全自动 ────────────────→ 待发送 ─→ 已发送
                                                              └─→ 失败（重试 3 次）

命中敏感词 ──→ 敏感词拦截（不调用模型，等你手动处理）
```

## 防风控设计

| 机制 | 默认值 | 位置 |
|---|---|---|
| 轮询间隔 | 120 秒 | 设置 → 回复策略 |
| 每日发送上限 | 50 条 | 同上 |
| 发送间隔随机化 | 8–30 秒 | 同上 |
| 夜间静默 | 23:00–08:00 不动作 | 同上 |
| 打字节奏模拟 | 每字符 40–110ms 随机 | 代码内置 |
| 敏感词拦截 | 投诉/律师/医疗等 | 设置 → 安全策略 |

评论去重靠数据库唯一索引（消息指纹），进程重启不会重复回复同一条。

## 数据位置

```
~/Library/Application Support/XHSAutoReply/
├── data.db          消息、回复、配置（含 API Key，目录权限 700）
├── browser/         浏览器登录态，删掉需要重新扫码
├── logs/            运行日志
├── screenshots/     页面操作失败时的现场截图
└── runtime.json     当前监听端口
```

放在 Application Support 而不是 `/tmp`——后者会被系统清理，去重记录一丢就会重复回复。

## 日常操作

```bash
# 查看守护进程状态
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
# 前台调试运行（需先停掉 launchd 实例）
.venv/bin/python -m xhsd --port 8787
```

## 卸载

```bash
bash scripts/uninstall.sh
```

数据默认保留，加 `--purge` 连数据一起删。

## 代码结构

| 路径 | 职责 |
|---|---|
| `xhsd/db.py` | SQLite 数据层，消息去重、回复状态机 |
| `xhsd/settings_store.py` | 运行时配置，存库、热生效 |
| `xhsd/llm/` | 服务商预设 + OpenAI 兼容客户端 |
| `xhsd/generator.py` | 人设 prompt 组装、敏感词、输出清洗 |
| `xhsd/collector.py` | Playwright 采集与发送 |
| `xhsd/scheduler.py` | 轮询循环、限额、静默、重试 |
| `xhsd/api.py` | 控制台 REST API |
| `web/` | 控制台前端（无框架，无构建步骤） |
| `menubar/app.py` | 菜单栏 App |
| `launchd/` `scripts/` | 开机自启与安装脚本 |

## 常见问题

**点了 App 图标什么都没发生** — 先看屏幕右上角菜单栏，图标可能已经在那儿了（这个 App 不占 Dock）。
如果确实没有，看 `logs/menubar.log`。已知两个坑都已规避，改代码时注意别踩回去：

- `rumps.App(name=...)` 的 name 必须是 ASCII，用中文会静默不创建菜单栏项
- launcher 脚本必须后台启动 python 再自己退出，用 `exec` 会让 python 成为
  bundle 主进程，这种情况下 WindowServer 不给它创建菜单栏项

**菜单栏图标显示 `书 ✕`** — 守护进程没在跑。点「重启守护进程」，或看 `logs/daemon.err.log`。

**一直提示需要扫码** — 小红书登录态过期了。点「扫码登录小红书」，扫完回控制台点「重新检测」。

**回复没发出去** — 检查是不是还在审核模式（草稿要手动批准）、是否到了每日上限、是否处于静默时段。这三项在概览页都能看到。

**找不到页面元素** — 小红书改版了。`screenshots/` 里有失败现场，据此调整 `xhsd/collector.py` 里的选择器。

## 注意

- 自动化操作有被平台风控的风险，建议先用小号测试
- 涉及交易、医疗、法律的评论请人工回复，敏感词拦截默认就是这个用途
- 本工具仅供学习研究使用
