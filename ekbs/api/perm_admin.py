"""权限管理页（`05` §8.2 的 ``/api/v1/perm/*``）—— 二期最后一块。

设计原则：**凡是会改变"谁能看到什么"的操作，一律委托给
:class:`~ekbs.permission.changes.PermissionChangeService`**。
那个服务已经把「数据更新 + 分片迁移 + 缓存失效 + 审计」四件事做在一个事务里
（`03` §6.4，第 2 轮修越权漏洞时建的）。本模块**不自己写 UPDATE**，
否则很容易漏掉 ``policy_version`` 自增，出现「权限改了、指纹没变、旧缓存继续命中」的窗口。

| 端点 | 权限点（`05` §8.2 / §3.4） |
|---|---|
| ``GET /perm/roles`` | ``perm:role:read``（SA/KA/AU） |
| ``POST/PUT/DELETE /perm/roles…`` | ``perm:role:create/update/delete``（**仅 SA**） |
| ``POST /perm/roles/{id}/assign`` | ``perm:role:assign``（仅 SA） |
| ``GET /perm/permissions`` | ``perm:role:read`` —— 权限点中文目录 |
| ``GET /perm/users`` | ``perm:user:read``（SA/KA/AU）；不含 ``demo_`` 种子身份 |
| ``POST /perm/users`` | ``perm:user:create``（仅 SA）—— 新建本机登录账号 |
| ``GET /perm/org/sync`` | ``perm:user:read`` —— 最近一次通讯录同步 |
| ``POST /perm/org/sync`` | ``perm:user:update``（SA/KA）—— 按授权范围手动拉一次部门和人员 |
| ``PUT /perm/org/sync/schedule`` | ``perm:user:update`` —— 定时更新的开关和间隔小时数 |
| ``PUT /perm/users/{id}`` | ``perm:user:update``（SA/KA）—— 只允许改 ``status`` |
| ``PUT /perm/users/{id}/clearance`` | ``perm:user:set_clearance``（**仅 SA**） |
| ``GET /perm/depts`` | ``perm:dept:read``（SA/KA/AU） |
| ``GET /perm/acl`` / ``POST`` / ``DELETE`` | ``perm:acl:read/create/delete`` |
| ``POST /perm/acl/simulate`` | ``perm:acl:simulate``（仅 SA） |
| ``GET /perm/audit`` | ``perm:audit:read``（SA/KA/AU） |

⚠️ **``PUT /perm/depts/{dept_id}/mapping`` 不实现**（返回 501）：部门↔分片映射正是开放项
**O6-1**（`06` §7.3：权威方案未定它存哪），而且它现在由 ``application.yaml`` 承载 ——
由 API 写 YAML 会制造"线上配置与 Git 版本漂移"，与 ``PUT /parse/config`` 是同一类问题。
文档定稿后要么加表、要么明确允许写 YAML，再实现不迟（已记入 README 待确认）。

⚠️ 用户资料（昵称/部门/职务）**由钉钉同步拥有**（`04` §4.2），管理台只允许改
``status``（启用/禁用）与 ``clearance``；改前者走审计，改后者走权限变更服务。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ekbs.api.auth import INTERNAL_TOKEN_HEADER, INTERNAL_USER_HEADER
from ekbs.api.qa import _authenticate
from ekbs.errors import ConfigError, PermissionDenied
from ekbs.logging import get_logger
from ekbs.permission.org_schedule import (
    read_org_schedule,
    save_org_schedule,
    touch_org_sync_clock,
)
from ekbs.permission.changes import PermissionChangeService
from ekbs.permission.router import bump_policy_version

router = APIRouter(tags=["perm"])
log = get_logger(component="api.perm")

BUILTIN_ROLE_CODES = ("super_admin", "kb_admin", "doc_reviewer", "auditor", "end_user")


def _principal(request: Request, headers: dict[str, str | None], code: str) -> Any:
    runtime = request.app.state.runtime
    user = _authenticate(request, headers)
    try:
        return runtime.authz.require(user.user_id, code)
    except PermissionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _h(authorization: str | None, token: str | None, user_id: str | None) -> dict[str, str | None]:
    return {
        "authorization": authorization,
        INTERNAL_TOKEN_HEADER.lower(): token,
        INTERNAL_USER_HEADER.lower(): user_id,
    }


def _changes(runtime: Any) -> PermissionChangeService:
    return PermissionChangeService(
        db=runtime.db, settings=runtime.settings, providers=runtime.providers,
        shards=runtime.shards, fts=runtime.fts, cache=runtime.cache,
    )


# ---------------------------------------------------------------------------
# 角色
# ---------------------------------------------------------------------------


@router.get("/perm/roles")
async def list_roles(
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """角色列表（含权限点与已分配人数）。"""
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "perm:role:read")
    runtime = request.app.state.runtime
    with runtime.db.sync_connect() as conn:
        roles = [dict(r) for r in conn.execute(
            "SELECT role_id, code, name, description, is_builtin, status FROM ekbs_role "
            "ORDER BY role_id"
        ).fetchall()]
        perms = conn.execute(
            "SELECT rp.role_id AS role_id, p.code AS code FROM ekbs_role_permission rp "
            "JOIN ekbs_permission p ON p.perm_id = rp.perm_id ORDER BY p.code"
        ).fetchall()
        counts = {
            int(r["role_id"]): int(r["c"])
            for r in conn.execute(
                "SELECT role_id, COUNT(*) AS c FROM ekbs_user_role GROUP BY role_id"
            ).fetchall()
        }
    by_role: dict[int, list[str]] = {}
    for row in perms:
        by_role.setdefault(int(row["role_id"]), []).append(str(row["code"]))
    for role in roles:
        role["permissions"] = by_role.get(int(role["role_id"]), [])
        role["user_count"] = counts.get(int(role["role_id"]), 0)
    return JSONResponse({"items": roles, "builtin_codes": list(BUILTIN_ROLE_CODES)})


@router.get("/perm/permissions")
async def list_permissions(
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """权限点目录：code 仍是接口契约，name 是给管理台看的中文。"""
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "perm:role:read")
    runtime = request.app.state.runtime
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT code, name, module FROM ekbs_permission ORDER BY module, code"
        ).fetchall()
    return JSONResponse({
        "items": [
            {"code": str(r["code"]), "name": str(r["name"]), "module": str(r["module"])}
            for r in rows
        ]
    })


class RoleCreate(BaseModel):
    code: str = Field(min_length=2, max_length=32, pattern="^[a-z0-9_]+$")
    name: str = Field(min_length=1, max_length=64)
    description: str = ""
    permissions: list[str] = Field(default_factory=list)


class RoleUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    status: int | None = Field(default=None, ge=0, le=1)
    permissions: list[str] | None = None


def _apply_role_permissions(runtime: Any, role_id: int, codes: list[str]) -> int:
    """全量替换某角色的权限点（不存在的 code 直接报错，避免静默漏配）。"""
    with runtime.db.sync_connect() as conn:
        known = {
            str(r["code"]) for r in conn.execute("SELECT code FROM ekbs_permission").fetchall()
        }
        unknown = sorted(set(codes) - known)
        if unknown:
            raise HTTPException(status_code=422, detail=f"未知权限点：{unknown}")
        conn.execute("DELETE FROM ekbs_role_permission WHERE role_id = ?", (role_id,))
        for code in sorted(set(codes)):
            conn.execute(
                "INSERT INTO ekbs_role_permission(role_id, perm_id) "
                "SELECT ?, perm_id FROM ekbs_permission WHERE code = ?",
                (role_id, code),
            )
        # 角色权限点集合变化 → 全量缓存失效（03 §6.4）
        version = bump_policy_version(conn, reason=f"role_permissions:{role_id}")
        conn.execute(
            "INSERT INTO ekbs_perm_change_log(object_type, object_id, change_type, field, "
            "old_value, new_value, reason) VALUES ('role', ?, 'role', 'permissions', '', ?, ?)",
            (str(role_id), json.dumps(sorted(set(codes)), ensure_ascii=False),
             f"policy_version={version}"),
        )
        conn.commit()
    return len(set(codes))


@router.post("/perm/roles")
async def create_role(
    payload: RoleCreate,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """创建自定义角色（内置五个角色由 `00` §3.3 固定，不在此列）。"""
    principal = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "perm:role:create"
    )
    runtime = request.app.state.runtime
    with runtime.db.sync_connect() as conn:
        if conn.execute("SELECT 1 FROM ekbs_role WHERE code = ?", (payload.code,)).fetchone():
            raise HTTPException(status_code=409, detail=f"角色 code 已存在：{payload.code}")
        conn.execute(
            "INSERT INTO ekbs_role(code, name, description, is_builtin, status, created_at) "
            "VALUES (?, ?, ?, 0, 1, datetime('now'))",
            (payload.code, payload.name, payload.description),
        )
        role_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.commit()
    applied = _apply_role_permissions(runtime, role_id, payload.permissions)
    log.info("role_created", role_id=role_id, code=payload.code, permissions=applied,
             operator=principal.user_id)
    return JSONResponse({"role_id": role_id, "code": payload.code, "permissions": applied},
                        status_code=201)


@router.put("/perm/roles/{role_id}")
async def update_role(
    role_id: int,
    payload: RoleUpdate,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """编辑角色（内置角色可改权限点，但**不允许改 code / 删除**）。"""
    principal = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "perm:role:update"
    )
    runtime = request.app.state.runtime
    with runtime.db.sync_connect() as conn:
        row = conn.execute("SELECT code FROM ekbs_role WHERE role_id = ?", (role_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="role_not_found")
        fields = {k: v for k, v in
                  {"name": payload.name, "description": payload.description,
                   "status": payload.status}.items() if v is not None}
        if fields:
            assignments = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(
                f"UPDATE ekbs_role SET {assignments} WHERE role_id = ?",
                (*fields.values(), role_id),
            )
            conn.commit()
    applied = None
    if payload.permissions is not None:
        applied = _apply_role_permissions(runtime, role_id, payload.permissions)
    log.info("role_updated", role_id=role_id, code=str(row["code"]), permissions=applied,
             operator=principal.user_id)
    return JSONResponse({"role_id": role_id, "updated_fields": sorted(fields), "permissions": applied})


@router.delete("/perm/roles/{role_id}")
async def delete_role(
    role_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """删除**自定义**角色（内置角色禁止删除）。"""
    principal = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "perm:role:delete"
    )
    runtime = request.app.state.runtime
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT code, is_builtin FROM ekbs_role WHERE role_id = ?", (role_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="role_not_found")
        if int(row["is_builtin"]) == 1:
            raise HTTPException(
                status_code=409,
                detail=f"内置角色不可删除（{row['code']}，00 §3.3 固定五个）",
            )
        conn.execute("DELETE FROM ekbs_role WHERE role_id = ?", (role_id,))
        version = bump_policy_version(conn, reason=f"role_delete:{role_id}")
        conn.execute(
            "INSERT INTO ekbs_perm_change_log(operator_id, object_type, object_id, change_type, "
            "field, old_value, new_value, reason) "
            "VALUES (?, 'role', ?, 'role', 'deleted', ?, '', ?)",
            (principal.user_id, str(role_id), str(row["code"]), f"policy_version={version}"),
        )
        conn.commit()
    return JSONResponse({"role_id": role_id, "deleted": True, "policy_version": version})


class RoleAssign(BaseModel):
    user_ids: list[int] = Field(min_length=1, max_length=200)
    scope_type: str = Field(default="global", pattern="^(global|kb)$")
    scope_id: int = 0


@router.post("/perm/roles/{role_id}/assign")
async def assign_role(
    role_id: int,
    payload: RoleAssign,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """把角色分配给用户（写入 ``ekbs_user_role`` 并让策略版本自增）。"""
    principal = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "perm:role:assign"
    )
    runtime = request.app.state.runtime
    with runtime.db.sync_connect() as conn:
        role = conn.execute("SELECT code FROM ekbs_role WHERE role_id = ?", (role_id,)).fetchone()
        if role is None:
            raise HTTPException(status_code=404, detail="role_not_found")
        granted = 0
        for user_id in payload.user_ids:
            exists = conn.execute(
                "SELECT 1 FROM ekbs_user WHERE user_id = ? AND deleted_at IS NULL", (user_id,)
            ).fetchone()
            if exists is None:
                continue
            cursor = conn.execute(
                "INSERT OR IGNORE INTO ekbs_user_role(user_id, role_id, scope_type, scope_id, "
                "granted_by, granted_at) VALUES (?, ?, ?, ?, ?, datetime('now'))",
                (user_id, role_id, payload.scope_type, int(payload.scope_id), principal.user_id),
            )
            granted += int(cursor.rowcount or 0)
            if int(cursor.rowcount or 0):
                # 该用户的权限上下文缓存立刻失效（04 §4.6：不能等 300s TTL）
                if runtime.cache is not None:
                    runtime.cache.invalidate(f"ekbs:perm:user:{user_id}")
        version = bump_policy_version(conn, reason=f"role_assign:{role_id}")
        conn.execute(
            "INSERT INTO ekbs_perm_change_log(operator_id, object_type, object_id, change_type, "
            "field, old_value, new_value, reason) "
            "VALUES (?, 'role', ?, 'role', 'assign', '', ?, ?)",
            (principal.user_id, str(role_id), json.dumps(payload.user_ids),
             f"policy_version={version}"),
        )
        conn.commit()
    return JSONResponse(
        {"role_id": role_id, "code": str(role["code"]), "granted": granted,
         "policy_version": version}
    )


# ---------------------------------------------------------------------------
# 用户
# ---------------------------------------------------------------------------


@router.get("/perm/users")
async def list_users(
    request: Request,
    q: str | None = Query(default=None, description="昵称/钉钉 ID 模糊匹配"),
    status: str | None = Query(default=None),
    auth_source: str | None = Query(default=None, description="local 只看管理台登记的账号"),
    limit: int = Query(default=50, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """用户列表（含角色、密级、部门链与本机登录名）。

    ``demo_`` 种子身份不出现在管理台：管理端登录走本机账号，不走演示身份。
    """
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "perm:user:read")
    runtime = request.app.state.runtime
    where = ["u.deleted_at IS NULL", "u.dingtalk_userid NOT LIKE 'demo_%'"]
    params: list[Any] = []
    if q:
        where.append("(u.nick LIKE ? OR u.dingtalk_userid LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    if status:
        where.append("u.status = ?")
        params.append(status)
    if auth_source:
        where.append("u.auth_source = ?")
        params.append(auth_source)
    sql = (
        "SELECT u.user_id, u.dingtalk_userid, u.nick, u.title, u.clearance, u.status, "
        "u.auth_source, u.dept_chain, u.perm_version, u.synced_at, "
        "(SELECT ud.dept_id FROM ekbs_user_dept ud "
        " JOIN ekbs_dept d ON d.dept_id = ud.dept_id AND d.deleted_at IS NULL "
        " WHERE ud.user_id = u.user_id "
        " ORDER BY ud.is_primary DESC, ud.dept_id LIMIT 1) AS dept_id, "
        "(SELECT a.username FROM ekbs_local_account a WHERE a.user_id = u.user_id "
        " ORDER BY a.username LIMIT 1) AS username, "
        "(SELECT GROUP_CONCAT(r.code) FROM ekbs_user_role ur JOIN ekbs_role r "
        " ON r.role_id = ur.role_id WHERE ur.user_id = u.user_id) AS roles "
        f"FROM ekbs_user u WHERE {' AND '.join(where)} ORDER BY u.user_id LIMIT ? OFFSET ?"
    )
    with runtime.db.sync_connect() as conn:
        rows = []
        for record in conn.execute(sql, (*params, limit, offset)).fetchall():
            item = dict(record)
            try:
                item["dept_chain"] = json.loads(item.get("dept_chain") or "[]")
            except ValueError:
                item["dept_chain"] = []
            item["roles"] = (item.get("roles") or "").split(",") if item.get("roles") else []
            item["dept_id"] = int(item["dept_id"]) if item.get("dept_id") is not None else None
            rows.append(item)
        total = int(
            conn.execute(
                f"SELECT COUNT(*) AS c FROM ekbs_user u WHERE {' AND '.join(where)}", tuple(params)
            ).fetchone()["c"]
        )
    return JSONResponse({"items": rows, "total": total, "limit": limit, "offset": offset})


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=6, max_length=128)
    nick: str = Field(min_length=1, max_length=64)
    clearance: int = Field(default=2, ge=1, le=4)
    role_code: str | None = None
    title: str = ""


@router.post("/perm/users")
async def create_local_user(
    payload: UserCreate,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """新建一个用登录名和口令进入管理台的账号。

    钉钉通讯录仍由同步维护。这里建的人 ``auth_source=local``，钉钉同步不会覆盖口令。
    """
    from ekbs.local_auth import MIN_PASSWORD_LENGTH, hash_password

    principal = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "perm:user:create"
    )
    username = payload.username.strip()
    nick = payload.nick.strip()
    if not username or any(ch.isspace() for ch in username) or ":" in username:
        raise HTTPException(status_code=422, detail="登录名不能有空格或冒号")
    if username.lower().startswith("demo"):
        raise HTTPException(status_code=422, detail="登录名不能使用 demo 前缀")
    if len(payload.password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=422, detail=f"口令至少 {MIN_PASSWORD_LENGTH} 位")
    try:
        password_hash = hash_password(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    runtime = request.app.state.runtime
    dingtalk_userid = f"local:{username}"
    with runtime.db.sync_connect() as conn:
        taken = conn.execute(
            "SELECT 1 FROM ekbs_local_account WHERE username = ? "
            "UNION ALL SELECT 1 FROM ekbs_user WHERE dingtalk_userid = ?",
            (username, dingtalk_userid),
        ).fetchone()
        if taken is not None:
            raise HTTPException(status_code=409, detail=f"登录名已存在：{username}")
        role_id = None
        if payload.role_code:
            role = conn.execute(
                "SELECT role_id FROM ekbs_role WHERE code = ? AND status = 1",
                (payload.role_code,),
            ).fetchone()
            if role is None:
                raise HTTPException(status_code=422, detail=f"角色不存在：{payload.role_code}")
            role_id = int(role["role_id"])
        conn.execute(
            "INSERT INTO ekbs_user(dingtalk_userid, nick, title, clearance, status, "
            "auth_source, dept_chain) VALUES (?, ?, ?, ?, 'active', 'local', '[]')",
            (dingtalk_userid, nick, payload.title.strip(), int(payload.clearance)),
        )
        user_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "INSERT INTO ekbs_local_account(username, user_id, password_hash, must_change) "
            "VALUES (?, ?, ?, 0)",
            (username, user_id, password_hash),
        )
        if role_id is not None:
            conn.execute(
                "INSERT INTO ekbs_user_role(user_id, role_id, scope_type, scope_id, granted_at) "
                "VALUES (?, ?, 'global', 0, datetime('now'))",
                (user_id, role_id),
            )
        version = bump_policy_version(conn, reason=f"user_create:{user_id}")
        conn.commit()
    log.info(
        "local_user_created", user_id=user_id, username=username,
        role=payload.role_code, operator=principal.user_id,
    )
    return JSONResponse(
        {
            "user_id": user_id,
            "username": username,
            "nick": nick,
            "clearance": int(payload.clearance),
            "role_code": payload.role_code,
            "policy_version": version,
        },
        status_code=201,
    )


class PasswordReset(BaseModel):
    password: str = Field(min_length=6, max_length=128)


@router.put("/perm/users/{user_id}/password")
async def reset_user_password(
    user_id: int,
    payload: PasswordReset,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """管理员重置某个本机账号的口令。钉钉同事没有管理台口令，不能在这里设。"""
    from ekbs.local_auth import MIN_PASSWORD_LENGTH, LocalAccountService

    principal = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "perm:user:update"
    )
    if len(payload.password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=422, detail=f"口令至少 {MIN_PASSWORD_LENGTH} 位")
    runtime = request.app.state.runtime
    service = LocalAccountService(runtime.db, runtime.settings)
    try:
        changed = service.set_password_for_user(user_id, payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not changed:
        raise HTTPException(status_code=404, detail="该用户没有管理台登录名，不能重置口令")
    log.info("local_password_reset", user_id=user_id, operator=principal.user_id)
    return JSONResponse({"user_id": user_id, "ok": True})


class UserUpdate(BaseModel):
    status: str | None = Field(default=None, pattern="^(active|disabled|left)$")
    reason: str = ""


@router.put("/perm/users/{user_id}")
async def update_user(
    user_id: int,
    payload: UserUpdate,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """编辑用户：**只允许改 ``status``**（启用/禁用/离职）。

    昵称、部门、职务等由钉钉同步拥有（`04` §4.2），在管理台改会被下一次同步覆盖 ——
    与其做一个人改完就被冲掉的输入框，不如直接不允许。
    """
    principal = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "perm:user:update"
    )
    runtime = request.app.state.runtime
    if payload.status is None:
        raise HTTPException(status_code=422, detail="本接口只支持修改 status")
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT status FROM ekbs_user WHERE user_id = ? AND deleted_at IS NULL", (user_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="user_not_found")
        conn.execute(
            "UPDATE ekbs_user SET status = ?, perm_version = perm_version + 1, "
            "synced_at = datetime('now') WHERE user_id = ?",
            (payload.status, user_id),
        )
        version = bump_policy_version(conn, reason=f"user_status:{user_id}")
        conn.execute(
            "INSERT INTO ekbs_perm_change_log(operator_id, object_type, object_id, change_type, "
            "field, old_value, new_value, reason) "
            "VALUES (?, 'user', ?, 'status', 'status', ?, ?, ?)",
            (principal.user_id, str(user_id), str(row["status"]), payload.status,
             payload.reason or "api:user_update"),
        )
        conn.commit()
    if runtime.cache is not None:
        runtime.cache.invalidate(f"ekbs:perm:user:{user_id}")
    return JSONResponse(
        {"user_id": user_id, "status": payload.status, "policy_version": version}
    )


class ClearanceUpdate(BaseModel):
    clearance: int = Field(ge=1, le=4)
    reason: str = ""


@router.put("/perm/users/{user_id}/clearance")
async def set_clearance(
    user_id: int,
    payload: ClearanceUpdate,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """设置用户密级上限 —— **走权限变更服务**（策略版本 + 缓存失效 + 审计一体）。"""
    principal = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id),
        "perm:user:set_clearance",
    )
    runtime = request.app.state.runtime
    try:
        result = await _changes(runtime).change_user_clearance(
            user_id, payload.clearance, operator_id=principal.user_id,
            reason=payload.reason or "api:set_clearance",
        )
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result.as_dict())


# ---------------------------------------------------------------------------
# 通讯录同步（手动全量；人员变动由钉钉事件推送，见 ekbs.dingtalk.org_events）
# ---------------------------------------------------------------------------


@router.get("/perm/org/sync")
async def org_sync_status(
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """最近一次通讯录同步的结果。"""
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "perm:user:read")
    runtime = request.app.state.runtime
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT log_id, mode, started_at, ended_at, created, updated, deleted, failed, "
            "error, detail FROM ekbs_sync_log ORDER BY log_id DESC LIMIT 1"
        ).fetchone()
    latest = dict(row) if row is not None else None
    if latest is not None:
        try:
            latest["detail"] = json.loads(latest.get("detail") or "{}")
        except ValueError:
            latest["detail"] = {}
    return JSONResponse(
        {
            "latest": latest,
            "schedule": read_org_schedule(runtime.db),
            "note": (
                "按钉钉应用当前的通讯录授权范围，同时更新部门和人员。"
                "定时更新按这里保存的小时间隔重复做这一次。"
                "平时的入职、离职、调岗仍可由钉钉事件推送。"
            ),
        }
    )


@router.post("/perm/org/sync")
async def org_sync_now(
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """按当前授权范围拉一次通讯录并登记。"""
    principal = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "perm:user:update"
    )
    runtime = request.app.state.runtime
    from ekbs.dingtalk.sync import build_sync_service

    try:
        service = build_sync_service(
            runtime.db, runtime.settings, runtime.secrets, cache=runtime.cache
        )
    except ConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        report = await service.sync_full(operator_id=principal.user_id)
    except Exception as exc:  # noqa: BLE001 - 钉钉侧失败要原样回到页面
        log.error("org_sync_manual_failed", operator_id=principal.user_id, error=str(exc))
        raise HTTPException(status_code=502, detail=f"通讯录同步失败：{exc}") from exc
    log.info("org_sync_manual", operator_id=principal.user_id, users=report.users,
             created=report.created, updated=report.updated)
    touch_org_sync_clock(runtime.db)
    return JSONResponse(report.as_dict())


class OrgSyncScheduleUpdate(BaseModel):
    enabled: bool
    interval_hours: int = Field(ge=1, le=720)


@router.put("/perm/org/sync/schedule")
async def org_sync_schedule(
    payload: OrgSyncScheduleUpdate,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """保存定时更新。间隔从保存这一刻起算，到点由调度进程拉部门和人员。"""
    _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "perm:user:update"
    )
    runtime = request.app.state.runtime
    try:
        saved = save_org_schedule(
            runtime.db, enabled=payload.enabled, interval_hours=payload.interval_hours
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(saved)


# ---------------------------------------------------------------------------
# 部门与映射
# ---------------------------------------------------------------------------


@router.get("/perm/depts")
async def list_depts(
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """部门树 + 闭包 + 当前生效的 dept_code 映射。"""
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "perm:dept:read")
    runtime = request.app.state.runtime
    from ekbs.permission.dept_tree import attach_dept_codes

    with runtime.db.sync_connect() as conn:
        depts = [
            dict(r)
            for r in conn.execute(
                "SELECT dept_id, dingtalk_dept_id, name, parent_dept_id, dept_path, depth, status "
                "FROM ekbs_dept WHERE deleted_at IS NULL"
            ).fetchall()
        ]
        closure = int(conn.execute("SELECT COUNT(*) FROM ekbs_dept_closure").fetchone()[0])
        members_by_dept: dict[int, list[dict[str, Any]]] = {}
        for record in conn.execute(
            "SELECT ud.dept_id, ud.is_primary, u.user_id, u.dingtalk_userid, u.nick, "
            "u.title, u.clearance, u.status, "
            "(SELECT GROUP_CONCAT(r.code) FROM ekbs_user_role ur "
            " JOIN ekbs_role r ON r.role_id = ur.role_id WHERE ur.user_id = u.user_id) AS roles "
            "FROM ekbs_user_dept ud "
            "JOIN ekbs_user u ON u.user_id = ud.user_id "
            "WHERE u.deleted_at IS NULL AND IFNULL(u.auth_source, '') != 'local' "
            "ORDER BY u.nick"
        ):
            members_by_dept.setdefault(int(record["dept_id"]), []).append(
                {
                    "user_id": int(record["user_id"]),
                    "dingtalk_userid": record["dingtalk_userid"],
                    "nick": record["nick"],
                    "title": record["title"],
                    "clearance": int(record["clearance"] or 1),
                    "status": record["status"],
                    "is_primary": int(record["is_primary"] or 0),
                    "roles": (record["roles"] or "").split(",") if record["roles"] else [],
                }
            )
    depts = attach_dept_codes(depts, runtime.settings.permission.dept_code_map or {})
    for dept in depts:
        dept["members"] = members_by_dept.get(int(dept["dept_id"]), [])
    return JSONResponse(
        {
            "items": depts,
            "closure_rows": closure,
            "dept_code_map": runtime.settings.permission.dept_code_map or {},
            "note": "dept_code 映射是开放项 O6-1（06 §7.3），当前由 application.yaml 承载",
        }
    )


@router.put("/perm/depts/{dept_id}/mapping")
async def set_dept_mapping(dept_id: int, request: Request) -> JSONResponse:
    """**未实现**：部门↔分片映射的存放位置是开放项 O6-1（`06` §7.3）。

    现在它由 ``application.yaml`` 承载，由 API 写 YAML 会造成"线上配置与 Git 版本漂移"
    （与 ``PUT /parse/config`` 同类）。文档定稿后：或加表、或明确允许写 YAML，再实现。
    """
    raise HTTPException(
        status_code=501,
        detail=(
            "部门↔分片映射（O6-1，06 §7.3）存放位置未定稿：当前由 application.yaml 承载，"
            "不支持由 API 修改配置（避免线上配置与 Git 版本漂移）。"
            "定稿后请同步 01 §5 的 Schema 或明确允许写 YAML。"
        ),
    )


# ---------------------------------------------------------------------------
# ACL
# ---------------------------------------------------------------------------


@router.get("/perm/acl")
async def list_acl(
    request: Request,
    object_type: str | None = Query(default=None),
    object_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """ACL 条目列表（allow/deny 都在这里，deny 优先）。"""
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "perm:acl:read")
    runtime = request.app.state.runtime
    where = ["1=1"]
    params: list[Any] = []
    if object_type:
        where.append("object_type = ?")
        params.append(object_type)
    if object_id:
        where.append("object_id = ?")
        params.append(str(object_id))
    with runtime.db.sync_connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT acl_id, object_type, object_id, subject_type, subject_id, permission, "
                "effect, granted_by, expire_at, created_at FROM ekbs_acl_entry "
                f"WHERE {' AND '.join(where)} ORDER BY acl_id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        ]
        total = int(
            conn.execute(
                f"SELECT COUNT(*) AS c FROM ekbs_acl_entry WHERE {' AND '.join(where)}",
                tuple(params),
            ).fetchone()["c"]
        )
    return JSONResponse({"items": rows, "total": total})


class AclCreate(BaseModel):
    object_type: str = Field(pattern="^(kb|folder|document|shard)$")
    object_id: str = Field(min_length=1)
    subject_type: str = Field(pattern="^(user|dept|role|group)$")
    subject_id: str = Field(min_length=1)
    permission: str = Field(default="read", pattern="^(read|write|manage|audit)$")
    effect: str = Field(default="allow", pattern="^(allow|deny)$")
    expire_at: str | None = None
    reason: str = ""


@router.post("/perm/acl")
async def create_acl(
    payload: AclCreate,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """新增授权 —— 走权限变更服务（ACL 与 ``policy_version`` 必须同事务，`06` §2.6）。"""
    principal = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "perm:acl:create"
    )
    runtime = request.app.state.runtime
    result = await _changes(runtime).grant_acl(
        object_type=payload.object_type, object_id=payload.object_id,
        subject_type=payload.subject_type, subject_id=payload.subject_id,
        permission=payload.permission, effect=payload.effect,
        operator_id=principal.user_id, expire_at=payload.expire_at,
        reason=payload.reason or "api:acl_create",
    )
    return JSONResponse(result.as_dict(), status_code=201)


@router.delete("/perm/acl/{acl_id}")
async def delete_acl(
    acl_id: int,
    request: Request,
    reason: str = Query(default=""),
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """撤销授权（按条目回查主体后调用服务的 revoke，保证同事务自增）。"""
    principal = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "perm:acl:delete"
    )
    runtime = request.app.state.runtime
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT object_type, object_id, subject_type, subject_id, permission "
            "FROM ekbs_acl_entry WHERE acl_id = ?",
            (acl_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="acl_not_found")
    try:
        result = await _changes(runtime).revoke_acl(
            object_type=str(row["object_type"]), object_id=str(row["object_id"]),
            subject_type=str(row["subject_type"]), subject_id=str(row["subject_id"]),
            permission=str(row["permission"]), operator_id=principal.user_id,
            reason=reason or "api:acl_delete",
        )
    except ConfigError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse({"acl_id": acl_id, **result.as_dict()})


class AclSimulate(BaseModel):
    user_id: int = Field(gt=0)
    doc_ids: list[int] = Field(default_factory=list, max_length=200)
    reason: str = Field(min_length=1, max_length=200)


@router.post("/perm/acl/simulate")
async def simulate_acl(
    payload: AclSimulate,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """权限模拟：某用户**可见哪些分片**、以及**对指定文档的 C5 判定**（仅 SA）。

    与检索调试台的 ``/rag/debug/simulate`` 分工不同：那个跑完整检索，
    这个只回答权限问题（不召回、不生成），用于改完 ACL 后立刻验证效果。
    同样落安全事件与审计。
    """
    principal = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "perm:acl:simulate"
    )
    runtime = request.app.state.runtime
    try:
        target = await runtime.perm_context(payload.user_id)
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail=f"目标用户不可用：{exc}") from exc

    runtime.gate.record_security_event(
        "permission_simulated",
        user_id=principal.user_id,
        severity="high",
        detail={
            "operator": principal.user_id,
            "simulated_user": payload.user_id,
            "scope": "acl_only",
            "doc_ids": payload.doc_ids[:20],
            "reason": payload.reason,
        },
    )
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_perm_change_log(operator_id, object_type, object_id, change_type, "
            "field, old_value, new_value, reason) "
            "VALUES (?, 'user', ?, 'simulate', 'acl', '', ?, ?)",
            (principal.user_id, str(payload.user_id), payload.reason[:100], "api:acl_simulate"),
        )
        conn.commit()

    decisions: list[dict[str, Any]] = []
    if payload.doc_ids:
        facts = runtime.gate.load_facts(payload.doc_ids)
        entries = runtime.gate.load_document_acl(payload.doc_ids)
        for doc_id in payload.doc_ids:
            decision = runtime.gate.can_read_document(
                target, facts.get(int(doc_id)), entries.get(int(doc_id), [])
            )
            decisions.append(
                {"doc_id": int(doc_id), "allowed": decision.allowed, "reason": decision.reason}
            )
    return JSONResponse(
        {
            "simulated_user": payload.user_id,
            "operator": principal.user_id,
            "clearance": target.clearance,
            "dept_chain": list(target.dept_chain),
            "visible_shards": list(target.visible_shard_ids),
            "acl_allow_shards": sorted(target.acl_allow_shards),
            "acl_deny_shards": sorted(target.acl_deny_shards),
            "document_decisions": decisions,
            "fingerprint": target.fingerprint,
        }
    )


# ---------------------------------------------------------------------------
# 权限变更审计
# ---------------------------------------------------------------------------


@router.get("/perm/audit")
async def permission_audit(
    request: Request,
    object_type: str | None = Query(default=None),
    change_type: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """权限变更审计（``ekbs_perm_change_log``，留存 ≥365 天，`00` §3.5）。"""
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "perm:audit:read")
    runtime = request.app.state.runtime
    where = ["1=1"]
    params: list[Any] = []
    if object_type:
        where.append("object_type = ?")
        params.append(object_type)
    if change_type:
        where.append("change_type = ?")
        params.append(change_type)
    with runtime.db.sync_connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT change_id, operator_id, object_type, object_id, change_type, field, "
                "old_value, new_value, reason, policy_version_before, policy_version_after, "
                "created_at FROM ekbs_perm_change_log "
                f"WHERE {' AND '.join(where)} ORDER BY change_id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        ]
        total = int(
            conn.execute(
                f"SELECT COUNT(*) AS c FROM ekbs_perm_change_log WHERE {' AND '.join(where)}",
                tuple(params),
            ).fetchone()["c"]
        )
    return JSONResponse({"items": rows, "total": total, "retention_days": 365})
