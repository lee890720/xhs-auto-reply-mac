"""
小红书自动回复工作流 - 配置文件
所有可调参数集中在此处管理。
"""
from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Optional


class Settings(BaseSettings):
    # ============ 浏览器连接 ============
    # Chrome for Testing 用户数据目录（Playwright 自动启动）
    chrome_user_data_dir: str = "/tmp/xhs-auto-reply/chrome-user-data"

    # ============ Coze API 配置 ============
    # 在 Coze 平台发布 Bot 后获取 API Token
    coze_api_token: str = ""
    # Coze Bot ID（发布后在概览页获取）
    coze_bot_id: str = ""
    # Coze API 端点（国内版）
    coze_api_base: str = "https://api.coze.cn/v3"
    # 回复生成超时时间（秒）
    coze_timeout: int = 30

    # ============ 回复策略 ============
    # 回复模式: "auto" 全自动发送 | "review" 生成草稿人工审核
    reply_mode: str = "review"
    # 轮询间隔（秒），建议不低于 30 秒以规避风控
    poll_interval: int = 60
    # 每次轮询最多处理的消息数
    max_messages_per_poll: int = 5
    # 每条回复最小延迟（秒），模拟人类思考
    reply_min_delay: float = 3.0
    # 每条回复最大延迟（秒）
    reply_max_delay: float = 8.0
    # 每日最大回复数（防风控）
    daily_reply_limit: int = 50

    # ============ 账号角色设定 ============
    # 告诉 AI 你的账号定位，让回复风格一致
    account_persona: str = (
        "你是一个小红书博主的通用型AI回复助手，能够适配各类账号定位。"
        "回复风格：亲切、真诚、有温度，根据内容灵活调整语气，"
        "适当使用 emoji，保持自然像朋友聊天。回复控制在 80 字以内。"
    )
    # 不自动回复的关键词（投诉、法律、医疗等敏感话题）
    sensitive_keywords: list[str] = [
        "投诉", "律师", "法院", "报警", "退款", "举报",
        "生病", "医生", "吃药", "抑郁", "自杀",
    ]
    # 遇到敏感话题时的兜底回复
    sensitive_fallback: str = "收到你的消息啦～这个问题比较重要，我稍后亲自回复你哦🙏"

    # ============ 监控范围 ============
    # 是否监控评论
    monitor_comments: bool = True
    # 是否监控私信
    monitor_messages: bool = True
    # 评论通知页 URL
    comments_url: str = "https://www.xiaohongshu.com/notification"
    # 私信页 URL
    messages_url: str = "https://www.xiaohongshu.com/notification"

    # ============ 数据持久化 ============
    # 已处理消息记录文件
    state_file: str = "/tmp/xhs-auto-reply/processed_messages.json"
    # 日志文件
    log_file: str = "/tmp/xhs-auto-reply/auto_reply.log"

    # ============ 调试 ============
    # 是否开启无头模式（建议 False，方便观察和手动登录）
    headless: bool = False
    # 截图调试目录
    screenshot_dir: str = "/tmp/xhs-auto-reply/screenshots"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
