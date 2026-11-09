"""钉钉 access_token 客户端 —— 依据 `docs/spec/50-dingtalk.md` §1（**已核实**的契约）。

契约要点（逐条来自官方文档，不是推测）：

* ``POST https://api.dingtalk.com/v1.0/oauth2/accessToken``，体 ``{"appKey","appSecret"}``，
  响应 ``{"accessToken", "expireIn"}``（``expireIn`` 单位**秒**，典型 7200）；
* **每个企业内部应用的 accessToken 相互独立**，须按应用维度缓存；
* 官方明确要求「缓存过期时间略小于 7200 秒（例如 7000 秒）」「不可频繁调用本接口，否则触发限流」，
  并建议「过期前 10 分钟」刷新 —— 本实现用配置里的 ``token_refresh_ahead_seconds``（默认 300）；
* 错误码 ``400 invalidClientIdOrSecret`` 属**不可重试**（密钥错就是错），
  5xx/网络错误按带抖动退避重试。

⚠️ 旧版 ``oapi.dingtalk.com/gettoken`` 已进「历史文档（不推荐）」，本实现不使用。

⚠️ 官方**没有**「每日调用次数上限」的口径（`docs/spec/50` §1.4 已标为未核实到），
因此这里不做每日配额假设，只做令牌级缓存。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from ekbs.cache.store import CacheStore
from ekbs.config import Settings
from ekbs.errors import ConfigError, ModelUnavailable
from ekbs.logging import get_logger
from ekbs.observability.metrics import TOKEN_EXPIRE, TOKEN_REFRESH_FAILED, inc, set_gauge
from ekbs.provider.http_client import CloudError, request_with_retry, resolve_base_url

log = get_logger(component="dingtalk.token")

DEFAULT_BASE_URL = "https://api.dingtalk.com"
TOKEN_PATH = "/v1.0/oauth2/accessToken"
#: 缓存 TTL 下限（``expireIn`` 异常小的时候别把缓存做成 0）
MIN_CACHE_TTL = 10


@dataclass
class TokenStatus:
    present: bool
    remaining_seconds: int = 0
    cached: bool = False


class DingTalkTokenClient:
    """按应用维度缓存 access_token，并在过期前刷新。"""

    def __init__(
        self,
        *,
        app_key: str,
        app_secret: str,
        base_url: str | None = None,
        cache: CacheStore | None = None,
        cache_key_template: str = "ekbs:dt:app_token:{app_key}",
        refresh_ahead_seconds: int = 300,
        timeout_s: float = 5.0,
        max_retries: int = 2,
        retry_base_delay: float = 1.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not app_key or not app_secret:
            raise ConfigError(
                "钉钉 clientId/clientSecret 缺失（应放在 ~/.ekbs/secrets.env 的 "
                "DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET，04 §2.6）"
            )
        self.app_key = app_key
        self.app_secret = app_secret
        self.base_url = resolve_base_url(base_url, DEFAULT_BASE_URL)
        self.cache = cache
        self.cache_key = cache_key_template.format(app_key=app_key)
        self.refresh_ahead_seconds = max(0, int(refresh_ahead_seconds))
        self.timeout_s = timeout_s
        self.max_retries = max(0, int(max_retries))
        self.retry_base_delay = retry_base_delay
        self._client = client
        #: 进程内兜底缓存（没有 CacheStore 时也能工作）
        self._memory: tuple[str, float] | None = None

    # ------------------------------------------------------------------

    @classmethod
    def from_settings(
        cls, settings: Settings, secrets: Any, cache: CacheStore | None = None
    ) -> "DingTalkTokenClient":
        openapi = (settings.dingtalk or {}).get("openapi") or {}
        return cls(
            app_key=secrets.get("DINGTALK_CLIENT_ID"),
            app_secret=secrets.get("DINGTALK_CLIENT_SECRET"),
            base_url=openapi.get("base_url") or DEFAULT_BASE_URL,
            cache=cache,
            cache_key_template=openapi.get("token_cache_key", "ekbs:dt:app_token:{app_key}"),
            refresh_ahead_seconds=int(openapi.get("token_refresh_ahead_seconds", 300) or 300),
            timeout_s=float(openapi.get("read_timeout", 5.0) or 5.0),
            max_retries=int(openapi.get("max_retries", 2) or 2),
        )

    # ------------------------------------------------------------------

    async def get_token(self, *, force: bool = False) -> str:
        """取 access_token；命中缓存不发起 HTTP（官方明确要求不许频繁调用）。"""
        if not force:
            cached = await self._load_cached()
            if cached is not None:
                return cached
        return await self._fetch_and_cache()

    async def _load_cached(self) -> str | None:
        if self.cache is not None:
            payload = await self.cache.get(self.cache_key)
            if isinstance(payload, dict) and payload.get("accessToken"):
                self._set_expire_gauge(payload)
                return str(payload["accessToken"])
            return None
        if self._memory is not None:
            token, expires_at = self._memory
            if expires_at > time.time():
                set_gauge(TOKEN_EXPIRE, max(0, int(expires_at - time.time())))
                return token
            self._memory = None
        return None

    async def _fetch_and_cache(self) -> str:
        url = f"{self.base_url}{TOKEN_PATH}"
        client = self._client or httpx.AsyncClient(timeout=self.timeout_s)
        owned = self._client is None
        try:
            response = await request_with_retry(
                client, "POST", url, provider="dingtalk", endpoint="accessToken",
                kind="llm", max_retries=self.max_retries, base_delay=self.retry_base_delay,
                json_body={"appKey": self.app_key, "appSecret": self.app_secret},
                headers={"Content-Type": "application/json"},
            )
        except CloudError as exc:
            inc(TOKEN_REFRESH_FAILED, 1)
            # 400 invalidClientIdOrSecret 是不可重试的配置错误，要说清楚
            fatal = "invalidClientIdOrSecret" in (exc.code + exc.message)
            detail = (
                "钉钉 clientId/clientSecret 无效（invalidClientIdOrSecret）"
                if fatal
                else f"获取钉钉 access_token 失败：{exc}"
            )
            log.error("dingtalk_token_failed", fatal=fatal, error=str(exc))
            if fatal:
                raise ConfigError(detail) from exc
            raise ModelUnavailable(detail) from exc
        finally:
            if owned:
                await client.aclose()

        payload = response.json() or {}
        token = str(payload.get("accessToken") or "")
        if not token:
            inc(TOKEN_REFRESH_FAILED, 1)
            raise ModelUnavailable(f"钉钉 access_token 响应缺少 accessToken：{payload}")
        expire_in = int(payload.get("expireIn") or 7200)
        ttl = max(MIN_CACHE_TTL, expire_in - self.refresh_ahead_seconds)

        record = {
            "accessToken": token,
            "expireIn": expire_in,
            "expires_at": int(time.time()) + expire_in,
        }
        if self.cache is not None:
            await self.cache.set(self.cache_key, record, ttl=ttl)
        else:
            self._memory = (token, float(record["expires_at"]))
        self._set_expire_gauge(record)
        log.info("dingtalk_token_refreshed", expire_in=expire_in, cache_ttl=ttl)
        return token

    def _set_expire_gauge(self, record: dict[str, Any]) -> None:
        """``ekbs_dingtalk_token_expire_seconds``（< 300s 触发 P3 告警）。"""
        expires_at = record.get("expires_at")
        if expires_at:
            set_gauge(TOKEN_EXPIRE, max(0, int(expires_at) - int(time.time())))
        elif record.get("expireIn"):
            set_gauge(TOKEN_EXPIRE, int(record["expireIn"]))

    # ------------------------------------------------------------------

    async def status(self) -> TokenStatus:
        """给 ``/readyz`` 与 ``/health/deps`` 用的令牌状态（**不发起网络请求**）。"""
        if self.cache is not None:
            payload = await self.cache.get(self.cache_key)
            if isinstance(payload, dict) and payload.get("accessToken"):
                remaining = max(0, int(payload.get("expires_at", 0)) - int(time.time()))
                return TokenStatus(True, remaining, cached=True)
            return TokenStatus(False)
        if self._memory is not None:
            remaining = max(0, int(self._memory[1] - time.time()))
            return TokenStatus(remaining > 0, remaining, cached=True)
        return TokenStatus(False)

    async def invalidate(self) -> None:
        """强制失效（401 之类需要换令牌时用）。"""
        self._memory = None
        if self.cache is not None:
            await self.cache.delete(self.cache_key)
