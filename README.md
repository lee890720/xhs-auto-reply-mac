# 小红书自动回复工作流

基于 Playwright RPA + Coze API 的小红书评论/私信自动回复系统。

## 工作原理

```
┌─────────────────────────────────────────────────────────────┐
│                     你的 Mac (Chrome 调试模式)                 │
│                                                             │
│  ┌──────────────────┐    CDP 连接    ┌───────────────────┐  │
│  │  Chrome 浏览器     │◄────────────│  Python 工作流      │  │
│  │  (已登录小红书)    │              │                   │  │
│  │                  │              │  1. 轮询评论/私信   │  │
│  │  小红书网页版      │─────────────►│  2. 调用 Coze API  │  │
│  │                  │   页面操作     │  3. 生成智能回复    │  │
│  │                  │◄─────────────│  4. 自动发送回复    │  │
│  └──────────────────┘              └───────┬───────────┘  │
│                                            │               │
└────────────────────────────────────────────┼───────────────┘
                                             │
                                    ┌────────▼────────┐
                                    │  Coze API       │
                                    │  (智能回复生成)   │
                                    └─────────────────┘
```

## 文件结构

| 文件 | 说明 |
|------|------|
| `config.py` | 所有配置项（API Token、轮询间隔、账号人设等） |
| `browser_ctrl.py` | 浏览器控制器（连接 Chrome、读取消息、发送回复） |
| `reply_generator.py` | AI 回复生成器（对接 Coze API） |
| `state_manager.py` | 状态管理（记录已处理消息，防重复回复） |
| `main.py` | 主程序入口 |
| `.env.example` | 环境变量模板 |

## 快速开始

### 第一步：安装依赖

```bash
cd xhs-auto-reply
pip install -r requirements.txt
playwright install chromium
```

### 第二步：配置 Coze API

1. 访问 [coze.cn](https://www.coze.cn)，创建一个新 Bot
2. 在 Bot 的「人设与回复逻辑」中写清楚你的账号定位和回复规则
3. 点击「发布」→ 选择「API」→ 发布
4. 在 Bot 的「概览」页面获取 **Bot ID**
5. 在个人中心 → 「API Token」页面创建 **Personal Access Token**
6. 复制 `.env.example` 为 `.env`，填入配置：

```bash
cp .env.example .env
```

编辑 `.env`：

```
COZE_API_TOKEN=pat_你的token
COZE_BOT_ID=你的bot_id
```

### 第三步：启动 Chrome 调试模式

在终端中执行：

```bash
# macOS
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222

# 如果 Chrome 已经在运行，需要先完全退出 Chrome，再执行上面的命令
```

### 第四步：手动登录小红书

在打开的 Chrome 浏览器中：
1. 访问 https://www.xiaohongshu.com
2. 扫码登录你的小红书账号
3. 确认能看到你的头像和首页内容

### 第五步：启动工作流

```bash
python3 main.py
```

## 回复模式

### 审核模式（默认，推荐先使用）

- 工作流会读取新消息并生成回复草稿
- **不会自动发送**，只在日志中展示
- 适合初期调试，确认 AI 回复质量后再切换自动模式
- 配置：`REPLY_MODE=review`

### 全自动模式

- 自动读取消息 → 生成回复 → 模拟人类操作发送
- 内置延迟、每日限额等防风控机制
- 配置：`REPLY_MODE=auto`

## 自定义配置

编辑 `config.py` 或通过 `.env` 环境变量调整：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `reply_mode` | `review` | `auto` 全自动 / `review` 审核 |
| `poll_interval` | `60` | 轮询间隔（秒） |
| `daily_reply_limit` | `50` | 每日最大回复数 |
| `reply_min_delay` | `3.0` | 回复最小延迟（秒） |
| `reply_max_delay` | `8.0` | 回复最大延迟（秒） |
| `account_persona` | 见配置 | 账号人设描述 |
| `sensitive_keywords` | 见配置 | 敏感词列表 |
| `monitor_comments` | `True` | 是否监控评论 |
| `monitor_messages` | `True` | 是否监控私信 |

## Coze Bot 人设参考

在 Coze 平台创建 Bot 时，可以参考以下人设模板：

```
# 角色
你是一个小红书博主的 AI 助手，负责回复粉丝的评论和私信。

# 账号定位
AI 量化交易知识分享，面向对量化投资感兴趣的年轻人。

# 回复规则
1. 语气亲切自然，像朋友聊天
2. 适当使用 emoji，但不要太多
3. 回复控制在 50-80 字
4. 遇到具体技术问题，给出简要方向并引导关注后续内容
5. 遇到合作咨询，礼貌引导到私信详谈
6. 不要做出任何投资收益承诺
7. 不要涉及医疗、法律、政治话题

# 禁止
- 不要透露你是 AI
- 不要推荐具体股票或基金
- 不要做价格承诺
```

## 防风控建议

1. **先用审核模式跑几天**，观察 AI 回复质量
2. 轮询间隔不要低于 30 秒
3. 每日回复数控制在 50 条以内
4. 不要在短时间内集中回复大量消息
5. 回复内容保持多样性，避免模板化
6. 定期检查账号状态，如有异常立即停止

## 常见问题

### Q: 连接浏览器失败？
确保 Chrome 已以 `--remote-debugging-port=9222` 参数启动，且没有其他 Chrome 实例占用该端口。

### Q: 找不到页面元素？
小红书页面结构可能更新。运行时会在 `data/screenshots/` 目录保存错误截图，可据此调整 `browser_ctrl.py` 中的选择器。

### Q: Coze API 报错？
检查 `.env` 中的 Token 和 Bot ID 是否正确，确认 Bot 已发布为 API 模式。

### Q: 如何修改回复风格？
修改 `config.py` 中的 `account_persona` 字段，或在 Coze 平台调整 Bot 人设。

## 注意事项

- 本工具仅供学习研究使用
- 自动化操作有被平台风控的风险，请合理使用
- 建议先用小号测试，确认稳定后再用于主账号
- 涉及交易、医疗、法律等敏感问题请人工回复
