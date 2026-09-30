"""身份与权限上下文端点（一期必需项）。

* ``GET /auth/me`` —— 当前身份的公开字段（不含权限明细）；
* ``GET /auth/perm`` —— **服务端**算出的权限上下文（可见分片、指纹、策略版本）；
  客户端**永远不能**声明指纹（`05` 已删除该路径）；
* ``POST /auth/dev-token`` —— **仅 dev 环境**签发测试令牌，生产一律 404。

钉钉免登（``POST /auth/dingtalk-login``）依赖钉钉凭据与 ``04`` §3 的免登流程，
在拿到企业应用凭据后再接；此处只留 501 占位，不假装实现。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ekbs.api.auth import INTERNAL_TOKEN_HEADER, INTERNAL_USER_HEADER
from ekbs.api.qa import _authenticate
from ekbs.errors import PermissionDenied

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


@router.get("/auth/dev-users")
async def dev_users(request: Request) -> JSONResponse:
    """列出可用于**开发身份切换**的用户（生产 404）。

    为什么需要它：管理台登录页原本硬编码演示身份的用户 ID，而 ID 是自增的
    （实测补种子后 Dave/Root 变成 7/8，硬编码的 4/5 直接 401）。
    这类"ID 猜错"的故障只有真点一下才暴露，所以改成让界面**动态取**。
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
            "WHERE u.status = 'active' "
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
    try:
        token = runtime.authenticator.issue_token(payload.user_id, channel=payload.channel)
    except PermissionDenied as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return JSONResponse({"token": token, "token_type": "Bearer", "user_id": payload.user_id})


class DingTalkLoginRequest(BaseModel):
    auth_code: str = Field(min_length=1, description="前端 dd.requestAuthCode 拿到的 code")


@router.post("/auth/dingtalk-login")
async def dingtalk_login(payload: DingTalkLoginRequest, request: Request) -> JSONResponse:
    """H5 微应用免登：``authCode`` → 钉钉换身份 → 映射 ekbs 用户 → 签发 JWT。

    契约依据 `docs/spec/50` §4（旧版 ``topapi/v2/user/getuserinfo`` 是官方主链路，
    鉴权走 **查询参数** ``access_token``）。authCode 一次性，由服务层防重放。
    """
    runtime = request.app.state.runtime
    secrets = runtime.secrets
    if not (
        secrets.is_configured("DINGTALK_CLIENT_ID")
        and secrets.is_configured("DINGTALK_CLIENT_SECRET")
    ):
        raise HTTPException(
            status_code=503,
            detail="钉钉凭据未配置（DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET）",
        )
    from ekbs.dingtalk.auth import DingTalkAuthService
    from ekbs.dingtalk.token import DingTalkTokenClient

    service = DingTalkAuthService(
        db=runtime.db,
        settings=runtime.settings,
        token_client=DingTalkTokenClient.from_settings(runtime.settings, secrets, runtime.cache),
        cache=runtime.cache,
    )
    try:
        identity = await service.resolve_user(payload.auth_code)
    except PermissionDenied as exc:
        # 越权/未映射一律 401，不泄露该用户是否存在
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    token = runtime.authenticator.issue_token(
        int(identity["user_id"]), channel="dingtalk_h5"
    )
    return JSONResponse(
        {
            "token": token,
            "token_type": "Bearer",
            "user_id": identity["user_id"],
            "nick": identity["nick"],
        }
    )
