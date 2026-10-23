"""知识库管理（`方案/05` §8.1 的 ``/kb/knowledge-bases``）。

补这个的原因很直接：在此之前**只有 CLI 能建知识库**（`ekbs.tools.import.ensure_kb`），
管理台里只有一个种子造的 `demo` 可选 —— 用起来就像个演示程序。知识库是业务对象，
必须能在界面里建。

权限点（都在 `方案/03` §3.2 里，本轮已补齐）：
``kb:knowledge_base:read`` / ``:create`` / ``:update`` / ``:delete``。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ekbs.api.auth import INTERNAL_TOKEN_HEADER, INTERNAL_USER_HEADER
from ekbs.api.qa import _authenticate
from ekbs.errors import PermissionDenied
from ekbs.logging import get_logger
from ekbs.permission.router import bump_policy_version

log = get_logger(component="api.kb")

router = APIRouter(tags=["kb"])

#: 知识库 code 的合法形态：小写字母/数字/下划线/连字符，2–32 位。
#: 它会进 URL 与历史审计，所以不接受中文与空格。（⚠️ 它**不进分片名**：分片名只由
#: 部门与密级决定，见 `ekbs/index/shard.py::make_shard_id` → `kb_<dept>_L<level>`。）
CODE_PATTERN = r"^[a-z0-9][a-z0-9_-]{1,31}$"

#: 界面「N 篇」：回收站（status=deleted）不计入；硬删/过期清理后才会真正从库里消失。
_LIVE_DOC_COUNT = (
    "(SELECT COUNT(*) FROM ekbs_document d "
    "WHERE d.kb_id = k.kb_id AND d.status != 'deleted')"
)


def _h(authorization: str | None, token: str | None, user_id: str | None) -> dict[str, str | None]:
    return {
        "authorization": authorization,
        INTERNAL_TOKEN_HEADER.lower(): token,
        INTERNAL_USER_HEADER.lower(): user_id,
    }


def _principal(request: Request, headers: dict[str, str | None], code: str) -> Any:
    runtime = request.app.state.runtime
    user = _authenticate(request, headers)
    try:
        return runtime.authz.require(user.user_id, code)
    except PermissionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "kb_id": int(row["kb_id"]),
        "code": str(row["code"]),
        "name": str(row["name"]),
        "description": row["description"],
        "default_level": int(row["default_level"] or 2),
        "status": int(row["status"] or 1),
        "created_at": row["created_at"],
        # 便于界面显示"这个库有多少文档"，避免为了一个数字再发一次请求
        "document_count": int(row["document_count"] or 0),
    }


@router.get("/kb/knowledge-bases")
async def list_knowledge_bases(
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """知识库列表（含文档数）。任何登录用户都可读 —— 它只是"有哪些库"，
    真正的行级可见性由文档层与检索层的权限下推负责。"""
    runtime = request.app.state.runtime
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "kb:knowledge_base:read")
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT k.kb_id, k.code, k.name, k.description, k.default_level, k.status, "
            "       k.created_at, "
            f"       {_LIVE_DOC_COUNT} AS document_count "
            "FROM ekbs_kb k WHERE k.status != 0 ORDER BY k.kb_id"
        ).fetchall()
        items = [_row_to_dict(r) for r in rows]
        from ekbs.personal import attach_personal_owners

        attach_personal_owners(conn, items)
    return JSONResponse({"items": items, "total": len(items)})


class KbCreate(BaseModel):
    code: str = Field(min_length=2, max_length=32, pattern=CODE_PATTERN)
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    default_level: int = Field(default=2, ge=1, le=4)


@router.post("/kb/knowledge-bases")
async def create_knowledge_base(
    payload: KbCreate,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """新建知识库。code 会进 URL 与命令行参数，所以限定为小写字母数字下划线连字符。"""
    user = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "kb:knowledge_base:create"
    )
    runtime = request.app.state.runtime
    with runtime.db.sync_connect() as conn:
        exists = conn.execute("SELECT kb_id FROM ekbs_kb WHERE code = ?", (payload.code,)).fetchone()
        if exists is not None:
            raise HTTPException(status_code=409, detail=f"知识库 code 已存在：{payload.code}")
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, description, default_level, status, created_at) "
            "VALUES (?, ?, ?, ?, 1, datetime('now'))",
            (payload.code, payload.name, payload.description, int(payload.default_level)),
        )
        kb_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        # 与权限变更一样自增策略版本：知识库集合变了，缓存里的旧上下文不该继续用
        version = bump_policy_version(conn, reason=f"kb:create:{payload.code}")
        conn.commit()
        row = conn.execute(
            "SELECT k.kb_id, k.code, k.name, k.description, k.default_level, k.status, "
            "       k.created_at, 0 AS document_count FROM ekbs_kb k WHERE k.kb_id = ?",
            (kb_id,),
        ).fetchone()
    log.info("kb_created", kb_id=kb_id, code=payload.code, operator=user.user_id, version=version)
    return JSONResponse({**_row_to_dict(row), "policy_version": version}, status_code=201)


class KbUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=512)
    default_level: int | None = Field(default=None, ge=1, le=4)


@router.put("/kb/knowledge-bases/{kb_id}")
async def update_knowledge_base(
    kb_id: int,
    payload: KbUpdate,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """改名称/描述/默认密级。**code 不可改** —— 它已经进了历史审计与接口路径。"""
    _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "kb:knowledge_base:update"
    )
    runtime = request.app.state.runtime
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    with runtime.db.sync_connect() as conn:
        row = conn.execute("SELECT kb_id FROM ekbs_kb WHERE kb_id = ?", (kb_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"知识库不存在：{kb_id}")
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(
                f"UPDATE ekbs_kb SET {sets} WHERE kb_id = ?", (*fields.values(), kb_id)
            )
            conn.commit()
        updated = conn.execute(
            "SELECT k.kb_id, k.code, k.name, k.description, k.default_level, k.status, "
            f"       k.created_at, {_LIVE_DOC_COUNT} "
            "       AS document_count FROM ekbs_kb k WHERE k.kb_id = ?",
            (kb_id,),
        ).fetchone()
    return JSONResponse(_row_to_dict(updated))


@router.delete("/kb/knowledge-bases/{kb_id}")
async def delete_knowledge_base(
    kb_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """删除知识库。**库里还有文档时拒绝** —— 连库一起删掉几百份文档太危险，
    请先逐篇下线/删除，或明确走另一个"级联删除"流程（当前未提供）。"""
    user = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "kb:knowledge_base:delete"
    )
    runtime = request.app.state.runtime
    with runtime.db.sync_connect() as conn:
        row = conn.execute("SELECT code FROM ekbs_kb WHERE kb_id = ?", (kb_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"知识库不存在：{kb_id}")
        live = int(
            conn.execute(
                "SELECT COUNT(*) FROM ekbs_document WHERE kb_id = ? AND status != 'deleted'",
                (kb_id,),
            ).fetchone()[0]
        )
        in_trash = int(
            conn.execute(
                "SELECT COUNT(*) FROM ekbs_document WHERE kb_id = ? AND status = 'deleted'",
                (kb_id,),
            ).fetchone()[0]
        )
        if live or in_trash:
            if live:
                detail = f"知识库里还有 {live} 篇文档，请先删除它们再删库（避免误删整库语料）"
            else:
                detail = (
                    f"回收站里还有 {in_trash} 篇文档，请先在回收站永久删除"
                    "（或等 30 天自动清理）后再删库"
                )
            raise HTTPException(status_code=409, detail=detail)
        conn.execute("DELETE FROM ekbs_kb WHERE kb_id = ?", (kb_id,))
        version = bump_policy_version(conn, reason=f"kb:delete:{row['code']}")
        conn.commit()
    log.warning("kb_deleted", kb_id=kb_id, code=str(row["code"]), operator=user.user_id)
    return JSONResponse({"kb_id": kb_id, "deleted": True, "policy_version": version})
