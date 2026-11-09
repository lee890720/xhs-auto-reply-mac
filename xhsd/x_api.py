"""
X（推特）官方 API v2 客户端。

只用到五个接口：
  GET  /2/users/me                 验证凭证，拿到自己的用户 ID
  GET  /2/users/:id/mentions       @ 我的推文（别人回复我的推文时也会自动带上 @我）
  POST /2/tweets                   发回复
  GET  /2/users/:id/followers      关注者列表，用来发现新关注者
  POST /2/users/:id/following      回关 —— 只在人点了「回关」之后才会调用

刻意不提供任何搜索接口。X 的开发者指南把「按关键词自动回复陌生人」列为
未经请求的互动，是封号的主要原因；这里从接口层面就不留这条路。

认证用 OAuth 1.0a 用户上下文（开发者后台生成的 4 个密钥）。只服务自己一个
账号时它比 OAuth 2.0 省事得多：不需要回调地址，令牌也不会过期，适合无人值守。
签名只有几十行，自己实现，不为此引入新依赖。
"""
import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

import httpx

API_BASE = "https://api.x.com/2"

TWEET_URL = "https://x.com/i/web/status/{tweet_id}"
PROFILE_URL = "https://x.com/{username}"

# X 的回复长度上限按「加权字符」算：拉丁字母、数字、常用标点算 1，
# 中日韩文字和 emoji 算 2，总上限 280。也就是说纯中文最多 140 个字。
X_MAX_WEIGHT = 280
_WEIGHT_1_RANGES = (
    (0, 4351),
    (8192, 8205),
    (8208, 8223),
    (8242, 8247),
)


# ==================== 异常 ====================

class XAPIError(Exception):
    """X API 调用失败。status 为 HTTP 状态码，网络错误时为 0"""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class XAuthError(XAPIError):
    """401：密钥无效、被撤销，或 App 被停用。重试没有用，需要人去处理"""


class XForbidden(XAPIError):
    """
    403：这个操作不被允许。

    常见原因：App 权限只有 Read（改成 Read and Write 后必须重新生成
    Access Token，旧令牌仍然是只读的）；对方限制了谁能回复；对方没有 @ 你，
    不满足 X 对 API 回复的限制；对方屏蔽了你。
    """


class XNotFound(XAPIError):
    """404：推文或用户已经不存在"""


class XPaymentRequired(XAPIError):
    """402：按量计费的额度用完了，需要去开发者后台充值"""


class XRateLimited(XAPIError):
    """429：触发频率限制。reset_at 是可以再次请求的 Unix 时间戳"""

    def __init__(self, message: str, reset_at: float):
        super().__init__(message, 429)
        self.reset_at = reset_at


# ==================== 数据结构 ====================

@dataclass
class XUser:
    id: str
    username: str = ""
    name: str = ""
    description: str = ""
    profile_image_url: str = ""
    followers_count: int = 0
    following_count: int = 0
    tweet_count: int = 0
    created_at: str = ""
    protected: bool = False
    # 只有用户上下文认证下才会返回，例如 ["following", "followed_by"]
    connection_status: list[str] = field(default_factory=list)

    @classmethod
    def from_api(cls, d: dict) -> "XUser":
        m = d.get("public_metrics") or {}
        return cls(
            id=str(d.get("id", "")),
            username=d.get("username", ""),
            name=d.get("name", ""),
            description=d.get("description", ""),
            profile_image_url=d.get("profile_image_url", ""),
            followers_count=int(m.get("followers_count", 0) or 0),
            following_count=int(m.get("following_count", 0) or 0),
            tweet_count=int(m.get("tweet_count", 0) or 0),
            created_at=d.get("created_at", ""),
            protected=bool(d.get("protected", False)),
            connection_status=list(d.get("connection_status") or []),
        )


@dataclass
class ContextTweet:
    """这条提及所回复（或引用）的那条推文，相当于小红书的「笔记」"""
    id: str
    text: str
    author_id: str = ""
    author_username: str = ""
    relation: str = "replied_to"      # replied_to | quoted


@dataclass
class Mention:
    id: str
    text: str
    author_id: str
    author_username: str = ""
    author_name: str = ""
    created_at: str = ""
    conversation_id: str = ""
    in_reply_to_user_id: str = ""
    mentioned_usernames: list[str] = field(default_factory=list)
    is_retweet: bool = False
    context: ContextTweet | None = None


@dataclass
class MentionsPage:
    mentions: list[Mention]
    newest_id: str = ""
    next_token: str = ""


@dataclass
class FollowersPage:
    users: list[XUser]
    next_token: str = ""


# ==================== 工具函数 ====================

def weighted_length(text: str) -> int:
    """按 X 的规则计算长度（不处理 URL 折算，回复里本来就不该带链接）"""
    total = 0
    for ch in text:
        cp = ord(ch)
        total += 1 if any(lo <= cp <= hi for lo, hi in _WEIGHT_1_RANGES) else 2
    return total


def truncate_weighted(text: str, limit: int = X_MAX_WEIGHT) -> str:
    """截到不超过 limit 的加权长度"""
    total = 0
    for i, ch in enumerate(text):
        cp = ord(ch)
        total += 1 if any(lo <= cp <= hi for lo, hi in _WEIGHT_1_RANGES) else 2
        if total > limit:
            return text[:i].rstrip()
    return text


def _pct(value) -> str:
    """RFC 3986 百分号编码，OAuth 1.0a 签名要求只保留 A-Z a-z 0-9 - . _ ~"""
    return quote(str(value), safe="~-._")


def oauth1_header(
    method: str,
    url: str,
    params: dict | None,
    consumer_key: str,
    consumer_secret: str,
    token: str,
    token_secret: str,
    nonce: str | None = None,
    timestamp: int | None = None,
) -> str:
    """
    生成 OAuth 1.0a（HMAC-SHA1）的 Authorization 头。

    params 是查询参数。JSON 请求体不参与签名（规范只要求签表单编码的请求体）。
    """
    oauth = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(timestamp if timestamp is not None else int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }

    parts = urlsplit(url)
    base_url = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))
    pairs = list(parse_qsl(parts.query, keep_blank_values=True))
    pairs += [(k, v) for k, v in (params or {}).items()]
    pairs += list(oauth.items())

    encoded = sorted((_pct(k), _pct(v)) for k, v in pairs)
    param_string = "&".join(f"{k}={v}" for k, v in encoded)
    base_string = "&".join((method.upper(), _pct(base_url), _pct(param_string)))
    signing_key = f"{_pct(consumer_secret)}&{_pct(token_secret)}"

    digest = hmac.new(signing_key.encode(), base_string.encode(), hashlib.sha1).digest()
    oauth["oauth_signature"] = base64.b64encode(digest).decode()

    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items()))


def _error_message(resp: httpx.Response) -> str:
    """从 X 的错误响应里取出人能看懂的部分"""
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:200] or f"HTTP {resp.status_code}"
    if isinstance(data, dict):
        if data.get("detail"):
            return str(data["detail"])
        if data.get("title"):
            return str(data["title"])
        errs = data.get("errors") or []
        if errs and isinstance(errs[0], dict):
            return str(errs[0].get("message") or errs[0].get("detail") or errs[0])
    return str(data)[:200]


# ==================== 客户端 ====================

class XClient:
    def __init__(
        self,
        consumer_key: str,
        consumer_secret: str,
        access_token: str,
        access_token_secret: str,
        timeout: float = 30.0,
        base_url: str = API_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.access_token = access_token
        self.access_token_secret = access_token_secret
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self._transport = transport          # 测试时注入假的 X 服务

    @property
    def configured(self) -> bool:
        return all((self.consumer_key, self.consumer_secret,
                    self.access_token, self.access_token_secret))

    async def _request(
        self, method: str, path: str, params: dict | None = None, json: dict | None = None
    ) -> dict:
        if not self.configured:
            raise XAuthError("X 的 4 个密钥没有填全，请在控制台「设置 → X 账号」中填写")

        url = self.base_url + path
        params = {k: str(v) for k, v in (params or {}).items() if v not in (None, "")}
        headers = {
            "Authorization": oauth1_header(
                method, url, params,
                self.consumer_key, self.consumer_secret,
                self.access_token, self.access_token_secret,
            ),
            "User-Agent": "xhs-auto-reply",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as c:
                resp = await c.request(method, url, params=params or None, json=json, headers=headers)
        except httpx.TimeoutException as e:
            raise XAPIError(f"请求 X 超时（{self.timeout}s）") from e
        except httpx.RequestError as e:
            raise XAPIError(f"连接 X 失败: {e}") from e

        if resp.status_code < 400:
            try:
                return resp.json()
            except ValueError as e:
                raise XAPIError(f"X 返回了无法解析的内容: {resp.text[:200]}") from e

        msg = _error_message(resp)
        status = resp.status_code
        if status == 401:
            raise XAuthError(f"X 认证失败（401）: {msg}", status)
        if status == 402:
            raise XPaymentRequired(f"X API 额度已用完（402）: {msg}", status)
        if status == 403:
            raise XForbidden(f"X 拒绝了这个操作（403）: {msg}", status)
        if status == 404:
            raise XNotFound(f"目标不存在（404）: {msg}", status)
        if status == 429:
            try:
                reset_at = float(resp.headers.get("x-rate-limit-reset", "0"))
            except ValueError:
                reset_at = 0.0
            if reset_at <= time.time():
                reset_at = time.time() + 15 * 60      # 没给重置时间就按 15 分钟窗口算
            raise XRateLimited(f"触发 X 的频率限制（429）: {msg}", reset_at)
        raise XAPIError(f"X 返回 HTTP {status}: {msg}", status)

    # ---------- 账号 ----------

    async def me(self) -> XUser:
        data = await self._request(
            "GET", "/users/me", {"user.fields": "username,name,public_metrics"}
        )
        return XUser.from_api(data.get("data") or {})

    # ---------- 提及 ----------

    async def mentions(
        self,
        user_id: str,
        since_id: str = "",
        max_results: int = 20,
        pagination_token: str = "",
    ) -> MentionsPage:
        """
        拉取 @ 我的推文，按时间从新到旧。

        被回复/被引用的那条推文通过 expansions 一次带回来，不用为了上下文
        再单独请求一次 —— 按量计费下这能省一半的读取费用。
        """
        params = {
            "max_results": max(5, min(100, max_results)),
            "since_id": since_id,
            "pagination_token": pagination_token,
            "tweet.fields": "author_id,conversation_id,created_at,referenced_tweets,"
                            "in_reply_to_user_id,entities",
            "expansions": "author_id,referenced_tweets.id,referenced_tweets.id.author_id",
            "user.fields": "username,name",
        }
        data = await self._request("GET", f"/users/{user_id}/mentions", params)

        includes = data.get("includes") or {}
        users = {u["id"]: u for u in includes.get("users") or [] if "id" in u}
        tweets = {t["id"]: t for t in includes.get("tweets") or [] if "id" in t}

        result = []
        for t in data.get("data") or []:
            refs = t.get("referenced_tweets") or []
            ref_types = {r.get("type"): r.get("id") for r in refs}

            context = None
            for rel in ("replied_to", "quoted"):
                ref_id = ref_types.get(rel)
                if ref_id and ref_id in tweets:
                    ref = tweets[ref_id]
                    ref_author = users.get(ref.get("author_id", ""), {})
                    context = ContextTweet(
                        id=ref_id,
                        text=ref.get("text", ""),
                        author_id=ref.get("author_id", ""),
                        author_username=ref_author.get("username", ""),
                        relation=rel,
                    )
                    break
                if ref_id:
                    # 被回复的推文没带回来（被删或不可见），至少记下 ID
                    context = ContextTweet(id=ref_id, text="", relation=rel)
                    break

            author = users.get(t.get("author_id", ""), {})
            mentioned = [
                m.get("username", "")
                for m in ((t.get("entities") or {}).get("mentions") or [])
            ]
            result.append(Mention(
                id=t["id"],
                text=t.get("text", ""),
                author_id=t.get("author_id", ""),
                author_username=author.get("username", ""),
                author_name=author.get("name", ""),
                created_at=t.get("created_at", ""),
                conversation_id=t.get("conversation_id", ""),
                in_reply_to_user_id=t.get("in_reply_to_user_id", ""),
                mentioned_usernames=[u for u in mentioned if u],
                is_retweet="retweeted" in ref_types,
                context=context,
            ))

        meta = data.get("meta") or {}
        return MentionsPage(
            mentions=result,
            newest_id=meta.get("newest_id", ""),
            next_token=meta.get("next_token", ""),
        )

    async def reply(self, in_reply_to_id: str, text: str) -> str:
        """回复一条推文，返回新推文的 ID"""
        data = await self._request(
            "POST", "/tweets",
            json={"text": text, "reply": {"in_reply_to_tweet_id": in_reply_to_id}},
        )
        new_id = (data.get("data") or {}).get("id", "")
        if not new_id:
            raise XAPIError(f"X 没有返回新推文 ID: {str(data)[:200]}")
        return new_id

    # ---------- 关注 ----------

    async def followers(
        self, user_id: str, max_results: int = 5, pagination_token: str = ""
    ) -> FollowersPage:
        """
        拉取关注者，一般是最近关注的排在最前。

        按量计费是按返回的用户数收钱的，所以每次只取很少几个，
        全是新人才往后翻页（翻页逻辑在引擎里）。
        """
        params = {
            "max_results": max(1, min(1000, max_results)),
            "pagination_token": pagination_token,
            "user.fields": "created_at,description,profile_image_url,public_metrics,"
                           "protected,connection_status",
        }
        data = await self._request("GET", f"/users/{user_id}/followers", params)
        return FollowersPage(
            users=[XUser.from_api(u) for u in data.get("data") or []],
            next_token=(data.get("meta") or {}).get("next_token", ""),
        )

    async def follow(self, my_id: str, target_user_id: str) -> str:
        """
        关注一个用户。返回 "following"，或对方是受保护账号时返回 "pending"
        （已发送关注请求，要等对方同意）。
        """
        data = await self._request(
            "POST", f"/users/{my_id}/following", json={"target_user_id": target_user_id}
        )
        d = data.get("data") or {}
        if d.get("following"):
            return "following"
        if d.get("pending_follow"):
            return "pending"
        raise XAPIError(f"关注结果无法识别: {str(data)[:200]}")
