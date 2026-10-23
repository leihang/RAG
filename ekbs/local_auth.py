"""本机账号密码登录 —— 管理台的第二种登录方式（`00` v4.0 决策补齐）。

定位：**服务器本机运维/应急**用。日常仍走钉钉免登（身份随组织架构同步），
本机账号只在「人就在这台 Mac 前、或钉钉不可用」时兜底。

四条硬纪律：

1. **默认只允许回环**（``127.0.0.1``）。经反向代理进来时 ``client.host`` 恒为
   127.0.0.1，所以必须同时要求 **没有 ``X-Forwarded-For``** —— 只看 client.host
   会把内网所有人当成"本机"（最容易搞错的一点）。要远程用请走钉钉或 VPN。
2. **不碰 ``ekbs_user``**：用户表由钉钉同步维护，密码塞进去会让同步语义变脏。
   本模块只维护「账号 → user_id」的绑定，权限/密级/审计全部复用既有链路。
3. **只存 scrypt 编码串**（标准库 ``hashlib.scrypt``，无第三方依赖），永不存明文、
   永不入日志；校验用 ``hmac.compare_digest`` 常数时间比较。
4. **账号维度锁定**：连续失败 N 次锁 M 分钟（配置可调），暴力破解不依赖外部限流。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from typing import Any

from ekbs.config import Settings
from ekbs.db import Database
from ekbs.logging import get_logger

log = get_logger(component="local_auth")

#: scrypt 参数（N=2^14, r=8, p=1 是交互式登录的常用档位，约 30~60ms/次）
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SALT_BYTES = 16

#: 密码最短长度。管理台账号是少数人用的口令，6 位即可，不再要求 12 位。
MIN_PASSWORD_LENGTH = 6

#: 默认失败上限与锁定时长（可被 settings.security 覆盖）
DEFAULT_MAX_FAILURES = 5
DEFAULT_LOCK_MINUTES = 15

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def is_loopback(client_host: str | None, forwarded_for: str | None) -> bool:
    """是否为「直连本机」的请求（本机登录的唯一放行条件）。"""
    if forwarded_for:  # 经代理 → 一定是外部来源（代理会把 client.host 变成回环）
        return False
    return bool(client_host) and client_host in _LOOPBACK_HOSTS


# ---------------------------------------------------------------------------
# 密码哈希
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """→ ``scrypt$N$r$p$salt_b64$hash_b64``（参数进串，便于将来升级参数后仍可校验旧密）。"""
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise ValueError(f"密码至少 {MIN_PASSWORD_LENGTH} 位")
    salt = os.urandom(SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN,
    )
    return "$".join(
        [
            "scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P),
            base64.b64encode(salt).decode(), base64.b64encode(digest).decode(),
        ]
    )


def verify_password(password: str, encoded: str) -> bool:
    """常数时间校验；编码串损坏一律 False（不抛异常，避免把"库坏了"变成 500）。"""
    try:
        scheme, n_s, r_s, p_s, salt_b64, hash_b64 = (encoded or "").split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        digest = hashlib.scrypt(
            (password or "").encode("utf-8"), salt=salt,
            n=int(n_s), r=int(r_s), p=int(p_s), dklen=len(expected),
        )
    except Exception:  # noqa: BLE001 - 任何解析/计算异常都视为校验失败
        return False
    return hmac.compare_digest(digest, expected)


# ---------------------------------------------------------------------------
# 账号服务
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthOutcome:
    ok: bool
    user_id: int | None = None
    reason: str = ""  # unknown | disabled | locked | bad_password | ""
    locked_until: str | None = None
    must_change: bool = False


class LocalAccountService:
    """``ekbs_local_account`` 的读写与认证（同步实现；接口层用 to_thread 调用）。"""

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    # ---- 配置 ----

    @property
    def _cfg(self) -> dict[str, Any]:
        return dict(getattr(self.settings, "security", None) or {})

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.get("local_login_enabled", True))

    @property
    def allow_remote(self) -> bool:
        return bool(self._cfg.get("local_login_allow_remote", False))

    @property
    def max_failures(self) -> int:
        return int(self._cfg.get("local_login_max_failures", DEFAULT_MAX_FAILURES))

    @property
    def lock_minutes(self) -> int:
        return int(self._cfg.get("local_login_lock_minutes", DEFAULT_LOCK_MINUTES))

    # ---- 账号维护（由 ekbs.tools.local_account 调用）----

    def create(
        self, *, username: str, user_id: int, password: str, must_change: bool = False
    ) -> None:
        with self.db.sync_connect() as conn:
            conn.execute(
                "INSERT INTO ekbs_local_account(username, user_id, password_hash, must_change)"
                " VALUES (?, ?, ?, ?)",
                (username, int(user_id), hash_password(password), 1 if must_change else 0),
            )
            conn.commit()

    def set_password_for_user(self, user_id: int, password: str, *, must_change: bool = False) -> int:
        """按用户重置口令（管理员操作，不校验旧口令）。没有本机账号时返回 0。"""
        encoded = hash_password(password)
        with self.db.sync_connect() as conn:
            cursor = conn.execute(
                "UPDATE ekbs_local_account SET password_hash = ?, must_change = ?,"
                " failed_attempts = 0, locked_until = NULL, updated_at = datetime('now')"
                " WHERE user_id = ?",
                (encoded, 1 if must_change else 0, int(user_id)),
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    def change_own_password(self, user_id: int, current: str, new: str) -> str:
        """本人改口令。返回 ``ok`` / ``no_account`` / ``bad_password``。"""
        with self.db.sync_connect() as conn:
            row = conn.execute(
                "SELECT username, password_hash FROM ekbs_local_account "
                "WHERE user_id = ? AND status = 'active' ORDER BY username LIMIT 1",
                (int(user_id),),
            ).fetchone()
        if row is None:
            return "no_account"
        if not verify_password(current, str(row["password_hash"])):
            return "bad_password"
        if current == new:
            return "same_password"
        self.set_password(str(row["username"]), new, must_change=False)
        return "ok"

    def set_password(self, username: str, password: str, *, must_change: bool = False) -> int:
        with self.db.sync_connect() as conn:
            cursor = conn.execute(
                "UPDATE ekbs_local_account SET password_hash = ?, must_change = ?,"
                " failed_attempts = 0, locked_until = NULL, updated_at = datetime('now')"
                " WHERE username = ?",
                (hash_password(password), 1 if must_change else 0, username),
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    def set_status(self, username: str, status: str) -> int:
        with self.db.sync_connect() as conn:
            cursor = conn.execute(
                "UPDATE ekbs_local_account SET status = ?, updated_at = datetime('now')"
                " WHERE username = ?",
                (status, username),
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    def list_accounts(self) -> list[dict[str, Any]]:
        with self.db.sync_connect() as conn:
            rows = conn.execute(
                "SELECT a.username, a.user_id, a.status, a.must_change, a.failed_attempts,"
                "       a.locked_until, a.last_login_at, u.nick, u.dingtalk_userid"
                " FROM ekbs_local_account a JOIN ekbs_user u ON u.user_id = a.user_id"
                " ORDER BY a.username"
            ).fetchall()
        return [dict(r) for r in rows]

    # ---- 认证 ----

    def authenticate(self, username: str, password: str) -> AuthOutcome:
        """校验用户名+密码，并维护失败计数/锁定。"""
        with self.db.sync_connect() as conn:
            row = conn.execute(
                "SELECT username, user_id, password_hash, status, must_change, failed_attempts,"
                "       locked_until,"
                "       (locked_until IS NOT NULL AND locked_until > datetime('now')) AS locked"
                " FROM ekbs_local_account WHERE username = ?",
                (username,),
            ).fetchone()
            if row is None:
                # 不区分「账号不存在」与「密码错」：避免账号枚举
                return AuthOutcome(False, reason="unknown")
            if str(row["status"]) != "active":
                return AuthOutcome(False, reason="disabled")
            if int(row["locked"] or 0):
                return AuthOutcome(
                    False, reason="locked", locked_until=str(row["locked_until"] or "")
                )
            if not verify_password(password, str(row["password_hash"])):
                self._register_failure(conn, str(username), int(row["failed_attempts"] or 0))
                return AuthOutcome(False, reason="bad_password")
            conn.execute(
                "UPDATE ekbs_local_account SET failed_attempts = 0, locked_until = NULL,"
                " last_login_at = datetime('now'), updated_at = datetime('now')"
                " WHERE username = ?",
                (username,),
            )
            conn.commit()
            return AuthOutcome(
                True, user_id=int(row["user_id"]), must_change=bool(row["must_change"])
            )

    def _register_failure(self, conn: Any, username: str, failures: int) -> None:
        """失败累加；达到上限即锁定并留痕（审计里能看见暴力破解尝试）。"""
        attempts = failures + 1
        if attempts >= self.max_failures:
            conn.execute(
                "UPDATE ekbs_local_account SET failed_attempts = ?,"
                " locked_until = datetime('now', ?), updated_at = datetime('now')"
                " WHERE username = ?",
                (attempts, f"+{self.lock_minutes} minutes", username),
            )
            conn.execute(
                "INSERT INTO ekbs_security_event(event_type, user_id, severity, detail)"
                " VALUES ('local_login_locked', NULL, 'medium', ?)",
                (f'{{"username": "{username}", "attempts": {attempts}}}',),
            )
            log.warning("local_login_locked", username=username, attempts=attempts)
        else:
            conn.execute(
                "UPDATE ekbs_local_account SET failed_attempts = ?, updated_at = datetime('now')"
                " WHERE username = ?",
                (attempts, username),
            )
        conn.commit()

    # ---- 运维辅助 ----

    def generate_password(self) -> str:
        """生成一个可复制的强口令（避免运维手编弱口令）。"""
        return secrets.token_urlsafe(12)
