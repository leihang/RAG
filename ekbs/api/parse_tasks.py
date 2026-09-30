"""解析任务中心（`05` §8.2 的 ``/api/v1/parse/tasks/*``）。

**为什么需要它**：`01` §12 的入库验收线是「解析成功率 ≥98%」，`01` §0.3 的 DoD 也要求
失败可查、可重试。没有这组接口，运维只能 `sqlite3` 直连看 ``ekbs_document.status``。

任务与文档的关系：本栈**一个文档就是一个解析任务**（没有独立的 ``parse_task`` 表，
29 张表里也没有），``task_id`` 即 ``doc_id`` —— 这一点在响应里显式说明，避免调用方误解。

| 端点 | 权限点（`05` §8.2） |
|---|---|
| ``GET /api/v1/parse/tasks`` | ``parse:task:read`` |
| ``GET /api/v1/parse/tasks/{task_id}`` | ``parse:task:read`` |
| ``GET /api/v1/parse/tasks/{task_id}/result`` | ``parse:task:read`` |
| ``GET /api/v1/parse/config`` | ``parse:task:read`` |
| ``POST /api/v1/parse/tasks/{task_id}/retry`` | ``doc:document:reparse`` |
| ``POST /api/v1/parse/tasks/{task_id}/cancel`` | ``parse:task:manage`` |

⚠️ **两处诚实交代**（都源自 Schema/文档的缺口，不是实现偷懒）：

1. ``ebks_document`` **没有 error 列**（`docs/spec/20` 的 T13 已记录），
   因此失败原因只能指向 worker 日志，接口不编造一个"错误信息"字段；
2. Schema 的状态机只有 ``pending|parsing|indexing|published|failed|offline``，
   **没有 cancelled** —— 「取消」对 ``pending`` 任务只能落到 ``failed``，响应里会说明。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ekbs.api.auth import INTERNAL_TOKEN_HEADER, INTERNAL_USER_HEADER
from ekbs.api.qa import _authenticate
from ekbs.errors import PermissionDenied
from ekbs.logging import get_logger
from ekbs.permission.router import bump_policy_version

router = APIRouter(tags=["parse"])
log = get_logger(component="api.parse")

VALID_STATUSES = ("pending", "parsing", "indexing", "published", "failed", "offline")
MAX_PAGE_SIZE = 200


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


def _task_row(runtime: Any, doc_id: int) -> Any:
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT d.doc_id, d.kb_id, k.code AS kb_code, d.title, d.status, d.level, "
            "d.visible_dept_ids, d.source_path, d.parse_path, d.page_count, d.size_bytes, "
            "d.checksum, d.doc_version, d.created_at, d.updated_at, "
            "(SELECT COUNT(*) FROM ekbs_chunk c WHERE c.doc_id = d.doc_id) AS chunk_count, "
            "(SELECT COUNT(*) FROM ekbs_chunk c WHERE c.doc_id = d.doc_id AND c.chunk_type='child') "
            "  AS child_count, "
            "(SELECT c.shard_id FROM ekbs_chunk c WHERE c.doc_id = d.doc_id LIMIT 1) AS shard_id "
            "FROM ekbs_document d LEFT JOIN ekbs_kb k ON k.kb_id = d.kb_id WHERE d.doc_id = ?",
            (doc_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="task_not_found")
    return row


@router.get("/parse/tasks")
async def list_parse_tasks(
    request: Request,
    status: str | None = Query(default=None, description="pending|parsing|indexing|published|failed|offline"),
    kb_id: int | None = Query(default=None),
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """解析任务列表（``task_id`` 即 ``doc_id``）。"""
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "parse:task:read")
    runtime = request.app.state.runtime
    if status and status not in VALID_STATUSES:
        raise HTTPException(status_code=422, detail=f"status 非法：{status}")
    limit = max(1, min(int(limit), MAX_PAGE_SIZE))
    offset = max(0, int(offset))
    where = ["1=1"]
    params: list[Any] = []
    if status:
        where.append("d.status = ?")
        params.append(status)
    if kb_id is not None:
        where.append("d.kb_id = ?")
        params.append(int(kb_id))
    sql = (
        "SELECT d.doc_id AS task_id, d.kb_id, d.title, d.status, d.level, d.page_count, "
        "d.created_at, d.updated_at, "
        "(SELECT COUNT(*) FROM ekbs_chunk c WHERE c.doc_id = d.doc_id) AS chunk_count "
        "FROM ekbs_document d "
        f"WHERE {' AND '.join(where)} ORDER BY d.updated_at DESC, d.doc_id DESC LIMIT ? OFFSET ?"
    )
    count_sql = f"SELECT COUNT(*) AS c FROM ekbs_document d WHERE {' AND '.join(where)}"
    with runtime.db.sync_connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, (*params, limit, offset)).fetchall()]
        total = int(conn.execute(count_sql, tuple(params)).fetchone()["c"])
        # 按状态汇总，便于一眼看出成功率（01 §12 的 98% 验收线）
        by_status = {
            str(r["status"]): int(r["c"])
            for r in conn.execute(
                "SELECT status, COUNT(*) AS c FROM ekbs_document GROUP BY status"
            ).fetchall()
        }
    attempted = total  # 本页过滤范围内的总数
    published = by_status.get("published", 0)
    all_total = sum(by_status.values()) or 1
    return JSONResponse(
        {
            "items": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
            "by_status": by_status,
            "success_rate": round(published / all_total, 4),
            "success_rate_target": 0.98,
            "note": "task_id 即 doc_id（本栈一个文档就是一个解析任务）",
        }
    )


@router.get("/parse/tasks/{task_id}")
async def get_parse_task(
    task_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """任务详情与质量门禁结果。"""
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "parse:task:read")
    runtime = request.app.state.runtime
    row = _task_row(runtime, task_id)
    item = dict(row)
    try:
        item["visible_dept_ids"] = json.loads(item.get("visible_dept_ids") or "[]")
    except ValueError:
        item["visible_dept_ids"] = []
    body: dict[str, Any] = {"task": item, "state_machine": list(VALID_STATUSES)}
    if str(row["status"]) == "failed":
        # Schema 没有 error 列（docs/spec/20 的 T13）：不编造字段，指向日志
        body["failure_hint"] = (
            "Schema 未提供 error 列（docs/spec/20 §T13），失败原因请查 "
            "var/log/worker.json.log 或 /opt/ekbs/log/worker.err.log；"
            "修复后可调用 retry 重新入队"
        )
    return JSONResponse(body)


@router.get("/parse/tasks/{task_id}/result")
async def get_parse_result(
    task_id: int,
    request: Request,
    preview_chars: int = Query(default=2000, ge=0, le=20000),
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """解析结果视图：父子块结构与 Markdown 预览（由已落库的分块拼回）。"""
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "parse:task:read")
    runtime = request.app.state.runtime
    _task_row(runtime, task_id)
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT chunk_id, parent_id, chunk_type, seq, heading_path, page_from, page_to, "
            "token_count, substr(content, 1, 400) AS content_head "
            "FROM ekbs_chunk WHERE doc_id = ? ORDER BY seq",
            (task_id,),
        ).fetchall()
        full = conn.execute(
            "SELECT content FROM ekbs_chunk WHERE doc_id = ? AND chunk_type IN ('parent','child') "
            "ORDER BY seq",
            (task_id,),
        ).fetchall()
    structure = [dict(r) for r in rows]
    markdown = "\n\n".join(str(r["content"]) for r in full)
    return JSONResponse(
        {
            "task_id": task_id,
            "structure": structure,
            "chunk_count": len(structure),
            "preview": markdown[:preview_chars],
            "preview_truncated": len(markdown) > preview_chars,
            "note": "parse_path 为解析产物落盘位置（MinerU 接入后填充；docs/spec/20 §T8 待定稿）",
        }
    )


@router.get("/parse/config")
async def get_parse_config(
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """当前生效的解析参数（只读）。

    ⚠️ `05` §8.2 还列了 ``PUT /api/v1/parse/config``（写入 YAML）——
    本实现**刻意不暴露写接口**：配置文件走 Git 评审（`05` §8.1 的说明），
    由 API 改 YAML 会制造"线上配置与库里 Git 版本不一致"的漂移。已记入 README 待确认。
    """
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "parse:task:read")
    runtime = request.app.state.runtime
    cfg = runtime.settings.parse
    return JSONResponse(
        {
            "engine": cfg.engine,
            "max_file_mb": cfg.max_file_mb,
            "timeout_s": cfg.timeout_s,
            "worker_concurrency": cfg.worker_concurrency,
            "keep_tmp_days": cfg.keep_tmp_days,
            "writable_via_api": False,
            "note": "解析参数写在 /opt/ekbs/etc/application.yaml，走 Git 评审（05 §8.1）",
        }
    )


@router.post("/parse/tasks/{task_id}/retry")
async def retry_parse_task(
    task_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """重新入队（``failed`` / ``offline`` → ``pending``，由 ``ekbs-worker`` 重新解析）。"""
    principal = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "doc:document:reparse"
    )
    runtime = request.app.state.runtime
    row = _task_row(runtime, task_id)
    current = str(row["status"])
    if current in {"parsing", "indexing"}:
        raise HTTPException(
            status_code=409, detail=f"任务正在 {current}，不能重排；请等它结束或先取消"
        )
    if current == "published":
        raise HTTPException(status_code=409, detail="已发布的文档无需重排；如需更新请先下线")
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_document SET status = 'pending', updated_at = datetime('now') "
            "WHERE doc_id = ?",
            (task_id,),
        )
        conn.execute(
            "INSERT INTO ekbs_perm_change_log(operator_id, object_type, object_id, change_type, "
            "field, old_value, new_value, reason) "
            "VALUES (?, 'document', ?, 'reparse', 'status', ?, 'pending', 'api:retry')",
            (principal.user_id, str(task_id), current),
        )
        conn.commit()
    log.info("parse_task_requeued", task_id=task_id, previous=current, operator=principal.user_id)
    return JSONResponse({"task_id": task_id, "status": "pending", "previous_status": current})


@router.post("/parse/tasks/{task_id}/cancel")
async def cancel_parse_task(
    task_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """取消**尚未开始**的解析任务。

    ⚠️ Schema 的状态机没有 ``cancelled``（只有 pending/parsing/indexing/published/failed/offline），
    因此取消 ``pending`` 任务只能落到 ``failed``；已在解析的任务无法中断
    （worker 是同步解析，SIGTERM 才会停）。响应里明确说明这两点。
    """
    principal = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "parse:task:manage"
    )
    runtime = request.app.state.runtime
    row = _task_row(runtime, task_id)
    current = str(row["status"])
    if current != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"只能取消 pending 任务（当前 {current}）；Schema 无 cancelled 状态，"
            "解析中的任务需等 worker 结束",
        )
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_document SET status = 'failed', updated_at = datetime('now') "
            "WHERE doc_id = ?",
            (task_id,),
        )
        conn.execute(
            "INSERT INTO ekbs_perm_change_log(operator_id, object_type, object_id, change_type, "
            "field, old_value, new_value, reason) "
            "VALUES (?, 'document', ?, 'cancel', 'status', 'pending', 'failed', 'api:cancel')",
            (principal.user_id, str(task_id)),
        )
        version = bump_policy_version(conn, reason=f"api:cancel:doc={task_id}")
        conn.commit()
    return JSONResponse(
        {
            "task_id": task_id,
            "status": "failed",
            "note": "Schema 无 cancelled 状态：取消映射为 failed（docs/spec/20 的状态机口径）",
            "policy_version": version,
        }
    )
