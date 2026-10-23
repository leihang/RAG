"""H5 微应用免登 —— 依据 `docs/spec/50-dingtalk.md` §4（**已核实**的官方主链路）。

官方推荐流程（四步）：

1. 前端 ``dd.requestAuthCode({corpId, clientId})`` → ``code``（**5 分钟一次性**）；
2. 后端用**企业应用 access_token** 调 ``POST https://oapi.dingtalk.com/topapi/v2/user/getuserinfo?access_token=…``，
   body ``{"code": code}``；
3. 响应 ``result.userid`` / ``result.unionid`` / ``result.name``；
4. 用 ``userid`` 映射到 ``ekbs_user.dingtalk_userid``，再由 ``ekbs-api`` 签发 JWT。

两处容易踩的坑（官方口径，`docs/spec/50` §4.4）：

* ``/v1.0/contact/users/{unionId}`` 要的是**用户级 token**（OAuth2 登录得来），且
  **不返回 userid** —— 所以免登主链路反而要走旧版路径 ``topapi/v2/user/getuserinfo``
  （`方案/04` §10.1 把它标成「兼容兜底」是错的，见 README §四之二 的 D5）；
* 旧版路径的鉴权是 **URL 查询参数** ``?access_token=``，不是新版的头部。

**防重放**：``code`` 官方是一次性且 5 分钟有效，因此本模块额外做一次本地一次性消费
（持久化在 ``ekbs_msg_dedup``，**不是**可清空的 ``ekbs_cache``），
避免网络层重放导致同一 code 换两次身份（`04` §3.4）。
"""

from __future__ import annotations

import httpx

from ekbs.cache.store import CacheStore
from ekbs.config import Settings
from ekbs.db import Database
from ekbs.errors import PermissionDenied
from ekbs.logging import get_logger
from ekbs.observability.metrics import AUTH_LOGIN, AUTH_REPLAY, inc
from ekbs.provider.http_client import CloudError, request_with_retry, resolve_base_url

log = get_logger(component="dingtalk.auth")

LEGACY_BASE_URL = "https://oapi.dingtalk.com"
USERINFO_PATH = "/topapi/v2/user/getuserinfo"
#: 官方：authCode 5 分钟有效、一次性；本地一次性消费记录持久化在 ekbs_msg_dedup（见 _consume_code）


class DingTalkAuthService:
    """把免登 ``authCode`` 换成 ``ekbs_user``。"""

    def __init__(
        self,
        *,
        db: Database,
        settings: Settings,
        token_client,
        cache: CacheStore | None = None,
        base_url: str | None = None,
        timeout_s: float = 5.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.token_client = token_client
        self.cache = cache
        openapi = (settings.dingtalk or {}).get("openapi") or {}
        self.base_url = resolve_base_url(
            base_url or openapi.get("legacy_base_url"), LEGACY_BASE_URL
        )
        self.timeout_s = timeout_s
        self.max_retries = max(0, int(max_retries))
        self._client = client

    # ------------------------------------------------------------------

    async def resolve_user(self, auth_code: str) -> dict:
        """返回 ``{"user_id", "dingtalk_userid", "nick", "unionid"}``；失败抛 ``PermissionDenied``。"""
        code = (auth_code or "").strip()
        if not code:
            inc(AUTH_LOGIN, 1, result="failure")
            raise PermissionDenied("缺少 authCode")

        if not await self._consume_code(code):
            inc(AUTH_REPLAY, 1)
            inc(AUTH_LOGIN, 1, result="failure")
            raise PermissionDenied("authCode 已被使用过（防重放）")

        identity = await self._exchange(code)
        userid = str(identity.get("userid") or "")
        if not userid:
            inc(AUTH_LOGIN, 1, result="failure")
            raise PermissionDenied("钉钉未返回 userid（请确认应用可见范围与该用户一致）")

        with self.db.sync_connect() as conn:
            row = conn.execute(
                "SELECT user_id, dingtalk_userid, nick FROM ekbs_user "
                "WHERE dingtalk_userid = ? AND status = 'active' AND deleted_at IS NULL",
                (userid,),
            ).fetchone()
        if row is None:
            inc(AUTH_LOGIN, 1, result="failure")
            log.warning("dingtalk_login_user_unmapped", userid=userid)
            raise PermissionDenied("该钉钉用户尚未同步到本系统，请联系管理员")
        inc(AUTH_LOGIN, 1, result="success")
        return {
            "user_id": int(row["user_id"]),
            "dingtalk_userid": str(row["dingtalk_userid"]),
            "nick": str(row["nick"]),
            "unionid": str(identity.get("unionid") or ""),
        }

    # ------------------------------------------------------------------

    async def _consume_code(self, code: str) -> bool:
        """一次性消费：同一 authCode 只能换一次身份（04 §3.4 的防重放）。

        持久化在 ``ekbs_msg_dedup``（与消息幂等同表，``dt_authcode:`` 前缀区分），
        ``INSERT ON CONFLICT`` 原子判定「首次使用」。**不放 ekbs_cache**——
        缓存的语义是「可随时清空」，而「这个 code 已用过」是安全事实：
        清缓存 = 解除防重放（v4.0 评估 P1 修复；与 ``IdempotencyGuard`` 同源论证）。
        留存由 ``msg_dedup_cleanup`` 作业负责（7 天 ≫ 官方 5 分钟有效期，安全方向）。
        """
        import asyncio
        import hashlib

        key = "dt_authcode:" + hashlib.sha256(code.encode()).hexdigest()[:32]

        def _insert_once() -> bool:
            with self.db.sync_connect() as conn:
                cursor = conn.execute(
                    "INSERT INTO ekbs_msg_dedup(msg_id, created_at) VALUES (?, datetime('now'))"
                    " ON CONFLICT(msg_id) DO NOTHING",
                    (key,),
                )
                conn.commit()
                return int(cursor.rowcount or 0) == 1

        return await asyncio.to_thread(_insert_once)

    async def _exchange(self, code: str) -> dict:
        token = await self.token_client.get_token()
        # ⚠️ 旧版 OAPI 路径的鉴权是**查询参数** access_token，不是新版头部
        # （docs/spec/50 §1.3/§4.3：两套规范长期并存，混用会 40014 invalid access_token）
        url = f"{self.base_url}{USERINFO_PATH}?access_token={token}"
        client = self._client or httpx.AsyncClient(timeout=self.timeout_s)
        owned = self._client is None
        try:
            response = await request_with_retry(
                client, "POST", url, provider="dingtalk", endpoint="user/getuserinfo",
                kind="llm", max_retries=self.max_retries,
                json_body={"code": code},
                headers={"Content-Type": "application/json"},
            )
        except CloudError as exc:
            raise PermissionDenied(f"钉钉免登换取身份失败：{exc}") from exc
        finally:
            if owned:
                await client.aclose()

        payload = response.json() or {}
        if int(payload.get("errcode", 0) or 0) != 0:
            raise PermissionDenied(
                f"钉钉返回错误：errcode={payload.get('errcode')} errmsg={payload.get('errmsg')}"
            )
        result = payload.get("result") or {}
        return {
            "userid": result.get("userid"),
            "unionid": result.get("unionid"),
            "name": result.get("name"),
        }
