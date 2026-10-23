"""鉴权 —— 两条入口，**都不接受调用方声明的权限**。

1. **用户**：``Authorization: Bearer <JWT>``。载荷由 ``ekbs-api`` 在钉钉免登后签发
   （``sub`` = ``ekbs_user.user_id``，``channel`` = ``dingtalk_h5`` 等）。
2. **内部回环**：``X-Internal-Token``（``EKBS_INTERNAL_TOKEN``）+ ``X-Internal-User-Id``。
   仅用于 ``com.ekbs.dingtalk`` → ``ekbs-api`` 的回环调用（`04` §1.5）。

关键纪律（`04` §4.6 / `05`）：**权限指纹与可见分片集合一律由本服务重算**。
请求里若带 ``X-Perm-Fingerprint`` 之类的头，本模块**忽略**它们 —— 那是被删除的信任路径。

⚠️ 待确认（`docs/spec/30` C-项）：内部回环 token 如何映射到身份，权威文档未定义。
本实现要求显式给出 ``X-Internal-User-Id``（或 ``X-Internal-Dt-Userid``）并由服务端查库确认，
既不信任其权限、也不接受其指纹。
"""

from __future__ import annotations

from dataclasses import dataclass

from ekbs.config import Secrets, Settings
from ekbs.db import Database
from ekbs.errors import PermissionDenied

INTERNAL_TOKEN_HEADER = "X-Internal-Token"
INTERNAL_USER_HEADER = "X-Internal-User-Id"
INTERNAL_DT_USER_HEADER = "X-Internal-Dt-Userid"
INTERNAL_CHANNEL_HEADER = "X-Internal-Channel"


@dataclass(frozen=True)
class AuthUser:
    user_id: int
    dingtalk_userid: str
    channel: str
    via: str  # jwt | internal


class Authenticator:
    def __init__(self, db: Database, settings: Settings, secrets: Secrets) -> None:
        self.db = db
        self.settings = settings
        self.secrets = secrets

    # ------------------------------------------------------------------

    def authenticate(
        self,
        *,
        authorization: str | None = None,
        internal_token: str | None = None,
        internal_user_id: str | None = None,
        internal_dt_userid: str | None = None,
        internal_channel: str | None = None,
    ) -> AuthUser:
        if internal_token:
            return self._authenticate_internal(
                internal_token, internal_user_id, internal_dt_userid, internal_channel
            )
        if authorization and authorization.lower().startswith("bearer "):
            return self._authenticate_jwt(authorization.split(" ", 1)[1].strip())
        raise PermissionDenied("未提供凭证")

    # ------------------------------------------------------------------

    def _authenticate_internal(
        self,
        token: str,
        user_id: str | None,
        dt_userid: str | None,
        channel: str | None,
    ) -> AuthUser:
        expected = self.secrets.get("EKBS_INTERNAL_TOKEN")
        if not expected:
            raise PermissionDenied("内部回环 token 未配置（EKBS_INTERNAL_TOKEN）")
        if not _constant_time_equals(token, expected):
            raise PermissionDenied("内部回环 token 不匹配")
        if not user_id and not dt_userid:
            raise PermissionDenied(
                "内部调用必须显式给出 X-Internal-User-Id 或 X-Internal-Dt-Userid"
            )
        user = self._load_user(
            user_id=int(user_id) if user_id and user_id.isdigit() else None,
            dt_userid=dt_userid,
        )
        if user is None:
            raise PermissionDenied("内部调用指向的用户不存在或已离职")
        return AuthUser(
            user_id=user[0], dingtalk_userid=user[1],
            channel=channel or "dingtalk_robot", via="internal",
        )

    def _authenticate_jwt(self, token: str) -> AuthUser:
        import jwt  # PyJWT

        secret = self.secrets.get("JWT_SECRET")
        if not secret:
            raise PermissionDenied("JWT_SECRET 未配置，用户令牌鉴权不可用")
        try:
            payload = jwt.decode(token, secret, algorithms=["HS256"])
        except Exception as exc:
            raise PermissionDenied(f"令牌无效：{exc}") from exc
        user_id = payload.get("sub")
        if not user_id:
            raise PermissionDenied("令牌缺少 sub")
        user = self._load_user(user_id=int(user_id), dt_userid=None)
        if user is None:
            raise PermissionDenied("用户不存在或已离职")
        channel = str(payload.get("channel") or "web")
        if channel == "dingtalk_h5":
            raise PermissionDenied("管理台不接受钉钉免登，请使用登录名和口令")
        return AuthUser(
            user_id=user[0],
            dingtalk_userid=user[1],
            channel=channel,
            via="jwt",
        )

    # ------------------------------------------------------------------

    def _load_user(self, *, user_id: int | None, dt_userid: str | None) -> tuple[int, str] | None:
        if user_id is None and dt_userid is None:
            return None
        sql = (
            "SELECT user_id, dingtalk_userid FROM ekbs_user "
            "WHERE deleted_at IS NULL AND status = 'active' AND "
        )
        params: tuple[object, ...]
        if user_id is not None:
            sql += "user_id = ?"
            params = (user_id,)
        else:
            sql += "dingtalk_userid = ?"
            params = (dt_userid,)
        with self.db.sync_connect() as conn:
            row = conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return int(row["user_id"]), str(row["dingtalk_userid"])

    # ------------------------------------------------------------------

    def issue_token(self, user_id: int, *, channel: str = "web", ttl_seconds: int = 7200) -> str:
        """签发用户 JWT（钉钉免登成功后由 API 调用）。"""
        import time

        import jwt

        secret = self.secrets.get("JWT_SECRET")
        if not secret:
            raise PermissionDenied("JWT_SECRET 未配置，无法签发令牌")
        payload = {
            "sub": str(user_id),
            "channel": channel,
            "iat": int(time.time()),
            "exp": int(time.time()) + int(ttl_seconds),
        }
        return jwt.encode(payload, secret, algorithm="HS256")


def _constant_time_equals(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
