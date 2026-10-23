"""身份与权限上下文端点（一期必需项）。

* ``GET /auth/me`` —— 当前身份的公开字段（不含权限明细）；
* ``GET /auth/perm`` —— **服务端**算出的权限上下文（可见分片、指纹、策略版本）；
  客户端**永远不能**声明指纹（`05` 已删除该路径）；
* ``POST /auth/dev-token`` —— **仅 dev 环境**签发测试令牌，生产一律 404；
* ``GET /auth/dingtalk-config`` —— 前端免登所需的**公开**配置（corpId / clientId / 是否可用）；
* ``POST /auth/dingtalk-login`` —— 钉钉 H5 免登：authCode → 身份 → JWT（`04` §3）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ekbs.api.auth import INTERNAL_TOKEN_HEADER, INTERNAL_USER_HEADER
from ekbs.api.qa import _authenticate
from ekbs.errors import PermissionDenied
from ekbs.local_auth import LocalAccountService, is_loopback
from ekbs.observability.metrics import AUTH_LOGIN, inc

router = APIRouter(tags=["auth"])


@router.get("/auth/me")
async def me(
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> dict[str, Any]:
    """当前用户 + ``permissions[]`` + ``policy_version`` + ``perm_fingerprint``。

    `05` §2.1：管理台**前端据此动态生成菜单**（无权限的菜单直接不渲染），
    因此这里必须回权限点集合，而不是角色码 —— 角色是内部实现，权限点才是契约。
    """
    user = _authenticate(
        request,
        {
            "authorization": authorization,
            INTERNAL_TOKEN_HEADER.lower(): x_internal_token,
            INTERNAL_USER_HEADER.lower(): x_internal_user_id,
        },
    )
    runtime = request.app.state.runtime
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT user_id, dingtalk_userid, nick, clearance, dept_chain, status "
            "FROM ekbs_user WHERE user_id = ?",
            (user.user_id,),
        ).fetchone()
        dept_names: list[str] = []
        raw_chain = []
        if row and row["dept_chain"]:
            try:
                raw_chain = json.loads(str(row["dept_chain"]))
            except Exception:
                raw_chain = []
            if isinstance(raw_chain, list) and raw_chain:
                placeholders = ",".join("?" for _ in raw_chain)
                name_rows = conn.execute(
                    f"SELECT dingtalk_dept_id, name FROM ekbs_dept "
                    f"WHERE dingtalk_dept_id IN ({placeholders})",
                    [str(x) for x in raw_chain],
                ).fetchall()
                by_id = {str(r["dingtalk_dept_id"]): str(r["name"]) for r in name_rows}
                dept_names = [by_id.get(str(x), str(x)) for x in raw_chain]
    perm = await runtime.perm_context(user.user_id)
    return {
        "user_id": user.user_id,
        "dingtalk_userid": user.dingtalk_userid,
        "nick": str(row["nick"]) if row else "",
        "clearance": int(row["clearance"]) if row else 0,
        "dept_chain": [str(x) for x in raw_chain] if isinstance(raw_chain, list) else [],
        "dept_names": dept_names,
        "channel": user.channel,
        "via": user.via,
        # 权限点集合（前端菜单过滤依据）
        "permissions": runtime.authz.granted_permissions(user.user_id),
        "roles": sorted(runtime.authz.load(user.user_id).roles),
        # 权限上下文（服务端计算，客户端不得声明）
        "perm_fingerprint": perm.fingerprint,
        "policy_version": perm.policy_version,
        "visible_shard_count": len(perm.visible_shard_ids),
    }


@router.get("/auth/perm")
@router.get("/perm/me")  # 05 §8.2 的权威路径（/auth/perm 为兼容别名）
async def perm_view(
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> dict[str, Any]:
    """返回服务端算出的权限上下文（指纹在这里产生，不来自请求头）。"""
    user = _authenticate(
        request,
        {
            "authorization": authorization,
            INTERNAL_TOKEN_HEADER.lower(): x_internal_token,
            INTERNAL_USER_HEADER.lower(): x_internal_user_id,
        },
    )
    runtime = request.app.state.runtime
    perm = await runtime.perm_context(user.user_id)
    body = perm.key_fields()
    body["visible_kb_ids"] = list(perm.visible_kb_ids)
    body["warnings"] = list(perm.warnings)
    return body


class DevTokenRequest(BaseModel):
    user_id: int = Field(gt=0)
    channel: str = "web"


# 管理台快捷登录只认种子身份。钉钉通讯录同步进来的同事走免登，不进这个名单。
# 前缀与 ekbs.personal.purge_demo_accounts、seed_demo 的 dingtalk_userid 一致。
_DEV_LOGIN_SQL = (
    "u.status = 'active' AND u.deleted_at IS NULL AND u.dingtalk_userid LIKE 'demo_%'"
)


@router.get("/auth/dev-users")
async def dev_users(request: Request) -> JSONResponse:
    """列出可用于**开发身份切换**的种子用户（生产 404）。

    为什么需要它：管理台登录页原本硬编码演示身份的用户 ID，而 ID 是自增的
    （实测补种子后 Dave/Root 变成 7/8，硬编码的 4/5 直接 401）。
    这类"ID 猜错"的故障只有真点一下才暴露，所以改成让界面**动态取**。

    只返回 ``demo_`` 种子身份。钉钉同步写入的在职同事不在此列 —— 否则通讯录
    一同步，登录下拉就变成全公司名单，而且那些账号默认只有普通用户权限。
    """
    runtime = request.app.state.runtime
    if not runtime.settings.is_dev:
        raise HTTPException(status_code=404, detail="not_found")
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT u.user_id, u.dingtalk_userid, u.nick, u.clearance, "
            "       GROUP_CONCAT(r.code) AS roles "
            "FROM ekbs_user u "
            "LEFT JOIN ekbs_user_role ur ON ur.user_id = u.user_id "
            "LEFT JOIN ekbs_role r ON r.role_id = ur.role_id "
            f"WHERE {_DEV_LOGIN_SQL} "
            "GROUP BY u.user_id ORDER BY u.user_id"
        ).fetchall()
    return JSONResponse(
        {
            "items": [
                {
                    "user_id": int(r["user_id"]),
                    "dingtalk_userid": str(r["dingtalk_userid"]),
                    "nick": r["nick"],
                    "clearance": int(r["clearance"] or 1),
                    "roles": str(r["roles"] or "").split(",") if r["roles"] else [],
                }
                for r in rows
            ]
        }
    )


@router.post("/auth/dev-token")
async def dev_token(payload: DevTokenRequest, request: Request) -> JSONResponse:
    """开发环境签发令牌（生产 404）。用于本地联调与冒烟，不是产品能力。"""
    runtime = request.app.state.runtime
    if not runtime.settings.is_dev:
        raise HTTPException(status_code=404, detail="not_found")
    with runtime.db.sync_connect() as conn:
        allowed = conn.execute(
            f"SELECT u.user_id FROM ekbs_user u WHERE u.user_id = ? AND {_DEV_LOGIN_SQL}",
            (payload.user_id,),
        ).fetchone()
    if allowed is None:
        raise HTTPException(
            status_code=403,
            detail="管理台快捷登录只接受种子身份。钉钉同事不能登录管理台，请使用登录名和口令。",
        )
    try:
        token = runtime.authenticator.issue_token(payload.user_id, channel=payload.channel)
    except PermissionDenied as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return JSONResponse({"token": token, "token_type": "Bearer", "user_id": payload.user_id})


class DingTalkLoginRequest(BaseModel):
    auth_code: str = Field(min_length=1, description="前端 dd.requestAuthCode 拿到的 code")


@router.get("/auth/dingtalk-config")
async def dingtalk_config(request: Request) -> JSONResponse:
    """前端免登所需的公开配置：``corpId`` / ``clientId`` / 是否可用。

    为什么要有这个端点（而不是把 corpId 编进前端产物）：
    换企业、换应用时前端不必重新构建；前端也**不落任何密钥**。
    返回的都是公开标识 —— ``corpId`` 与 AppKey(``clientId``) 本就不是密钥，
    **AppSecret 永不外发**（它只存在于 ``~/.ekbs/secrets.env``）。
    """
    runtime = request.app.state.runtime
    secrets = runtime.secrets
    corp_id = secrets.get("DINGTALK_CORP_ID")
    client_id = secrets.get("DINGTALK_CLIENT_ID")
    enabled = bool(
        corp_id and client_id and secrets.is_configured("DINGTALK_CLIENT_SECRET")
    )
    return JSONResponse(
        {
            "enabled": enabled,
            "corp_id": corp_id,
            "client_id": client_id,
            # 凭据齐备但 corp_id 缺失时给出可操作的原因，避免前端只显示"不可用"
            "missing": [
                key
                for key, ok in (
                    ("DINGTALK_CORP_ID", bool(corp_id)),
                    ("DINGTALK_CLIENT_ID", bool(client_id)),
                    ("DINGTALK_CLIENT_SECRET", secrets.is_configured("DINGTALK_CLIENT_SECRET")),
                )
                if not ok
            ],
        }
    )


@router.post("/auth/dingtalk-login")
async def dingtalk_login(payload: DingTalkLoginRequest, request: Request) -> JSONResponse:
    """管理台不开放钉钉免登。

    钉钉同事继续在机器人里提问。管理台只允许「用户管理」里建好的登录名和口令。
    端点保留，避免旧页面拿到含糊的 500；一律拒绝签发管理台令牌。
    """
    del payload, request
    raise HTTPException(
        status_code=403,
        detail="管理台不接受钉钉免登。请使用登录名和口令；钉钉同事不能登录管理台。",
    )


class LocalLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


@router.post("/auth/local-login")
async def local_login(payload: LocalLoginRequest, request: Request) -> JSONResponse:
    """本机账号密码登录（管理台第二种登录方式：**仅服务器本机**）。

    三道闸：
    1. ``security.local_login_enabled=false`` → 404（不暴露端点存在）；
    2. **回环校验**：``client.host`` 是回环 **且没有 X-Forwarded-For**
       （经反向代理时 client.host 恒为 127.0.0.1，只看它会把内网所有人都放进来）；
       需要远程时用钉钉免登，或显式打开 ``local_login_allow_remote``（不推荐）；
    3. 账号维度失败锁定（默认 5 次锁 15 分钟）。

    登录成功即签发与钉钉免登**同一套 JWT** —— 权限、密级、审计链路完全一致，
    区别只是认证方式（``channel="local"``）。
    """
    runtime = request.app.state.runtime
    service = LocalAccountService(runtime.db, runtime.settings)

    if not service.enabled:
        raise HTTPException(status_code=404, detail="not_found")

    forwarded_for = request.headers.get("x-forwarded-for")
    client_host = request.client.host if request.client else None
    if not service.allow_remote and not is_loopback(client_host, forwarded_for):
        inc(AUTH_LOGIN, 1, result="failure")
        raise HTTPException(
            status_code=403,
            detail=(
                "管理台口令登录只允许在这台服务器本机（127.0.0.1）使用。"
                "钉钉同事不能登录管理台。"
            ),
        )

    outcome = await asyncio.to_thread(service.authenticate, payload.username, payload.password)
    if not outcome.ok:
        inc(AUTH_LOGIN, 1, result="failure")
        if outcome.reason == "locked":
            raise HTTPException(
                status_code=423,
                detail=f"账号已锁定（连续失败过多），解锁时间：{outcome.locked_until}",
            )
        # 账号不存在 / 已禁用 / 密码错 一律同一句话，避免账号枚举
        raise HTTPException(status_code=401, detail="用户名或密码不正确，或账号不可用")

    assert outcome.user_id is not None
    inc(AUTH_LOGIN, 1, result="success")
    token = runtime.authenticator.issue_token(int(outcome.user_id), channel="local")
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT nick FROM ekbs_user WHERE user_id = ?", (int(outcome.user_id),)
        ).fetchone()
    return JSONResponse(
        {
            "token": token,
            "token_type": "Bearer",
            "user_id": outcome.user_id,
            "nick": str(row["nick"]) if row else "",
            "must_change": outcome.must_change,
        }
    )


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


@router.post("/auth/password")
async def change_password(
    payload: PasswordChangeRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """本人修改管理台口令。必须先用当前口令证明是本人。"""
    from ekbs.local_auth import MIN_PASSWORD_LENGTH

    user = _authenticate(request, {"authorization": authorization})
    if len(payload.new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(status_code=422, detail=f"新口令至少 {MIN_PASSWORD_LENGTH} 位")
    runtime = request.app.state.runtime
    service = LocalAccountService(runtime.db, runtime.settings)
    result = await asyncio.to_thread(
        service.change_own_password,
        user.user_id,
        payload.current_password,
        payload.new_password,
    )
    if result == "no_account":
        raise HTTPException(status_code=404, detail="当前身份没有管理台口令")
    if result == "bad_password":
        raise HTTPException(status_code=401, detail="旧口令不正确")
    if result == "same_password":
        raise HTTPException(status_code=422, detail="新口令不能和旧口令相同")
    return JSONResponse({"ok": True})
