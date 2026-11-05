"""发现层 API（D22-P0）—— 可见文档导览 + 示例问 chips。

路径：``GET /api/v1/discovery/catalog``

鉴权：登录即可（钉钉内部令牌或管理台 JWT）；**不做** ``doc:document:read`` 门槛，
以便端用户经 Stream 回环也能拿到导览。行级可见性仍走 ``can_see_document``。
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ekbs.api.auth import (
    INTERNAL_CHANNEL_HEADER,
    INTERNAL_DT_USER_HEADER,
    INTERNAL_TOKEN_HEADER,
    INTERNAL_USER_HEADER,
)
from ekbs.api.qa import _authenticate
from ekbs.authz import SUPER_ADMIN
from ekbs.permission.console import is_console_role, widen_for_console
from ekbs.dingtalk.client import MAX_SAMPLE_FILE_BYTES, sample_file_too_big
from ekbs.discovery import build_catalog, display_doc_title, resolve_file_request

router = APIRouter(tags=["discovery"])

#: 发回钉钉的文件本体上限（请求体 20 MiB 扣掉最长文件名的 multipart 封装）。
_MAX_SEND_BYTES = MAX_SAMPLE_FILE_BYTES


def dingtalk_send_limit_message(title: str, size: int) -> str:
    """超限时说明是钉钉机器人发文件的上限，文件仍在知识库里。"""
    size_mb = int(size) / (1024 * 1024)
    return (
        f"知识库里有「{title}」（原件 {size_mb:.2f} MB）。"
        f"这份文件超过钉钉机器人发文件的上限（不到 20 MB），发不回聊天窗口。"
        f"不是知识库没有这份文件。"
    )


class FileRequestBody(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    #: 群聊（``2``）只允许公开文档；个人库文件必须回单聊处理。
    conversation_type: str | None = Field(default=None, pattern="^[12]$")


def _headers(
    authorization: str | None,
    token: str | None,
    user_id: str | None,
    dt_userid: str | None = None,
    channel: str | None = None,
) -> dict[str, str | None]:
    return {
        "authorization": authorization,
        INTERNAL_TOKEN_HEADER.lower(): token,
        INTERNAL_USER_HEADER.lower(): user_id,
        INTERNAL_DT_USER_HEADER.lower(): dt_userid,
        INTERNAL_CHANNEL_HEADER.lower(): channel,
    }


@router.get("/discovery/catalog")
async def discovery_catalog(
    request: Request,
    doc_limit: int = Query(default=20, ge=1, le=100),
    suggestion_limit: int = Query(default=5, ge=1, le=20),
    mode: str = Query(default="full", pattern="^(full|help|docs|permissions|examples)$"),
    conversation_type: str = Query(default="1", pattern="^[12]$"),
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
    x_internal_dt_userid: str | None = Header(default=None, alias=INTERNAL_DT_USER_HEADER),
    x_internal_channel: str | None = Header(default=None, alias=INTERNAL_CHANNEL_HEADER),
) -> JSONResponse:
    """当前用户可见的已发布文档 + 示例问 + 钉钉帮助文案。"""
    runtime = request.app.state.runtime
    user = _authenticate(
        request,
        _headers(
            authorization,
            x_internal_token,
            x_internal_user_id,
            x_internal_dt_userid,
            x_internal_channel,
        ),
    )
    perm = await runtime.perm_context(user.user_id)
    principal = runtime.authz.load(user.user_id)
    admin_bypass = SUPER_ADMIN in principal.roles
    if (
        is_console_role(principal.roles)
        and not admin_bypass
        and (x_internal_channel or "") != "dingtalk_robot"
    ):
        perm = widen_for_console(perm, runtime.db)
    if conversation_type == "2":
        # 群聊公开范围：即使管理员也不能绕过（群回复公开），强制走 ACL 白名单。
        admin_bypass = False
        from ekbs.permission.public_scope import public_scope_perm

        perm = public_scope_perm(perm, runtime.db)
    body: dict[str, Any] = build_catalog(
        runtime,
        perm,
        doc_limit=doc_limit,
        suggestion_limit=suggestion_limit,
        admin_bypass=admin_bypass,
        group_chat=(conversation_type == "2"),
        mode=mode,
    )
    body["user_id"] = user.user_id
    body["clearance"] = perm.clearance
    return JSONResponse(body)


def _source_file(runtime: Any, source: str) -> Path | None:
    """原件必须落在 raw / trash 内。"""
    if not source or source.startswith("demo://"):
        return None
    try:
        resolved = Path(source).resolve()
    except OSError:
        return None
    paths = runtime.settings.data_paths()
    for name in ("raw", "trash"):
        root = Path(paths[name]).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        if resolved.is_file():
            return resolved
    return None


@router.post("/discovery/file")
async def discovery_file(
    body: FileRequestBody,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
    x_internal_dt_userid: str | None = Header(default=None, alias=INTERNAL_DT_USER_HEADER),
    x_internal_channel: str | None = Header(default=None, alias=INTERNAL_CHANNEL_HEADER),
) -> JSONResponse:
    """按当前用户权限，把点名的原文准备好（不走向量摘要）。"""
    runtime = request.app.state.runtime
    user = _authenticate(
        request,
        _headers(
            authorization,
            x_internal_token,
            x_internal_user_id,
            x_internal_dt_userid,
            x_internal_channel,
        ),
    )
    perm = await runtime.perm_context(user.user_id)
    principal = runtime.authz.load(user.user_id)
    admin_bypass = SUPER_ADMIN in principal.roles
    if (
        is_console_role(principal.roles)
        and not admin_bypass
        and (x_internal_channel or "") != "dingtalk_robot"
    ):
        perm = widen_for_console(perm, runtime.db)
    if body.conversation_type == "2":
        # 群聊公开范围：管理员也不能绕过（文件会发到群里）。
        admin_bypass = False
        from ekbs.permission.public_scope import public_scope_perm

        perm = public_scope_perm(perm, runtime.db)
    result = resolve_file_request(
        runtime, perm, body.text, admin_bypass=admin_bypass
    )
    result["user_id"] = user.user_id
    if result.get("action") != "send" or result.get("doc_id") is None:
        return JSONResponse(result)

    doc_id = int(result["doc_id"])
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT source_path, title, status FROM ekbs_document WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
    title = display_doc_title(str(row["title"]) if row else "")
    candidate = _source_file(runtime, str(row["source_path"] or "") if row else "")
    if row is None or str(row["status"]) != "published" or candidate is None:
        runtime.audit.record_access(
            user_id=user.user_id, doc_id=doc_id, action="download",
            channel="dingtalk_robot", result="not_found",
        )
        result["action"] = "none"
        result["content_base64"] = ""
        result["message"] = f"找到了「{title or '该文档'}」，但这份原件不在库里，暂时发不出去。"
        return JSONResponse(result)

    data = candidate.read_bytes()
    filename = candidate.name
    if len(data) > _MAX_SEND_BYTES or sample_file_too_big(len(data), filename):
        result["action"] = "none"
        result["content_base64"] = ""
        result["message"] = dingtalk_send_limit_message(title, len(data))
        return JSONResponse(result)

    runtime.audit.record_access(
        user_id=user.user_id, doc_id=doc_id, action="download",
        channel="dingtalk_robot", result="ok",
    )
    result["filename"] = candidate.name
    result["content_base64"] = base64.b64encode(data).decode("ascii")
    result["message"] = f"已按你的权限发送原文：{title}"
    return JSONResponse(result)
