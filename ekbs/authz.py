"""RBAC 权限判定 —— `01` §5.3 的表结构 + `05` §3.4/§8.2 的权限点口径。

判定链（与 Schema 一一对应）：

    ekbs_user_role（含 ``scope_type``/``scope_id``）
        → ekbs_role_permission
        → ekbs_permission.code

三条纪律：

1. **权限点命名以 `05` §8.2 为准**（如 ``audit:qa:read``、``sys:health:read``、
   ``doc:document:read``）—— 接口与权限点必须一一对上，否则管理台菜单过滤与后端校验会漂移。
2. ``super_admin`` 视为**通配**：`00` §3.3 定义它「含系统设置、模型配置」全部权限；
   与其在每次新增权限点时都要记得给它授权，不如在判定处显式兜底（并留日志）。
3. 本模块**只回答「能不能」**，不做任何数据过滤 ——
   数据可见性是 :mod:`ekbs.permission` 的职责，两者不要混。
"""

from __future__ import annotations

from dataclasses import dataclass

from ekbs.db import Database
from ekbs.errors import PermissionDenied
from ekbs.logging import get_logger

log = get_logger(component="authz")

SUPER_ADMIN = "super_admin"


@dataclass(frozen=True)
class Principal:
    user_id: int
    permissions: frozenset[str]
    roles: frozenset[str]

    def has(self, code: str) -> bool:
        return code in self.permissions or SUPER_ADMIN in self.roles

    def require(self, code: str) -> None:
        if not self.has(code):
            raise PermissionDenied(f"缺少权限点：{code}")


class AuthzService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------

    def load(self, user_id: int) -> Principal:
        """取某用户的角色与权限点集合（一次 SQL，避免每个接口查两次）。"""
        with self.db.sync_connect() as conn:
            roles = {
                str(r["code"])
                for r in conn.execute(
                    "SELECT r.code FROM ekbs_user_role ur "
                    "JOIN ekbs_role r ON r.role_id = ur.role_id "
                    "WHERE ur.user_id = ? AND r.status = 1",
                    (user_id,),
                ).fetchall()
            }
            permissions = {
                str(r["code"])
                for r in conn.execute(
                    "SELECT DISTINCT p.code FROM ekbs_user_role ur "
                    "JOIN ekbs_role_permission rp ON rp.role_id = ur.role_id "
                    "JOIN ekbs_permission p ON p.perm_id = rp.perm_id "
                    "WHERE ur.user_id = ?",
                    (user_id,),
                ).fetchall()
            }
        return Principal(user_id=user_id, permissions=frozenset(permissions), roles=frozenset(roles))

    def has_permission(self, user_id: int, code: str) -> bool:
        return self.load(user_id).has(code)

    def require(self, user_id: int, code: str) -> Principal:
        principal = self.load(user_id)
        if not principal.has(code):
            log.warning(
                "authz_denied", user_id=user_id, permission=code, roles=sorted(principal.roles)
            )
            raise PermissionDenied(f"缺少权限点：{code}")
        return principal

    # ------------------------------------------------------------------

    def granted_permissions(self, user_id: int) -> list[str]:
        """给 ``GET /api/v1/auth/me`` 用：前端据此**动态生成菜单**（`05` §2.1）。"""
        principal = self.load(user_id)
        if SUPER_ADMIN in principal.roles:
            with self.db.sync_connect() as conn:
                return sorted(
                    str(r["code"]) for r in conn.execute("SELECT code FROM ekbs_permission")
                )
        return sorted(principal.permissions)
