"""解析任务中心（`05` §8.2 的 ``/api/v1/parse/tasks/*``）。

**为什么需要它**：`01` §12 的入库验收线是「解析成功率 ≥98%」，`01` §0.3 的 DoD 也要求
失败可查、可重试。没有这组接口，运维只能 `sqlite3` 直连看 ``ekbs_document.status``。

任务与文档的关系：本栈**一个文档就是一个解析任务**（没有独立的 ``parse_task`` 表，
31 张表里也没有），``task_id`` 即 ``doc_id`` —— 这一点在响应里显式说明，避免调用方误解。

| 端点 | 权限点（`05` §8.2） |
|---|---|
| ``GET /api/v1/parse/tasks`` | ``parse:task:read`` |
| ``GET /api/v1/parse/tasks/{task_id}`` | ``parse:task:read`` |
| ``GET /api/v1/parse/tasks/{task_id}/result`` | ``parse:task:read`` |
| ``GET /api/v1/parse/tasks/{task_id}/draft`` | ``parse:task:read``（待复核可改；已入库只读对照） |
| ``GET /api/v1/parse/tasks/{task_id}/figures/{figure_id}`` | ``parse:task:read``（复核期看图） |
| ``PUT /api/v1/parse/tasks/{task_id}/draft`` | ``parse:task:manage`` |
| ``POST /api/v1/parse/tasks/{task_id}/approve`` | ``parse:task:manage`` |
| ``POST /api/v1/parse/tasks/{task_id}/reject`` | ``parse:task:manage`` |
| ``GET /api/v1/parse/config`` | ``parse:task:read`` |
| ``POST /api/v1/parse/tasks/{task_id}/retry`` | ``doc:document:reparse`` |
| ``POST /api/v1/parse/tasks/{task_id}/cancel`` | ``parse:task:manage`` |

⚠️ **两处诚实交代**（都源自 Schema/文档的缺口，不是实现偷懒）：

1. ``ekbs_document`` **没有 error 列**（`docs/spec/20` 的 T13 已记录），
   因此失败原因只能指向 worker 日志，接口不编造一个"错误信息"字段；
2. 扫描件软状态 ``review``（无 SQL CHECK）：解析清洗后等人审，通过后再分块；
   「取消」对 ``pending`` 仍落到 ``failed``（Schema 无 cancelled）。
"""

from __future__ import annotations

import json
import mimetypes
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from ekbs.api.auth import INTERNAL_TOKEN_HEADER, INTERNAL_USER_HEADER
from ekbs.api.qa import _authenticate
from ekbs.errors import PermissionDenied
from ekbs.ingest.pipeline import IngestPipeline
from ekbs.ingest.retrievable import RETRIEVABLE_PREDICATE
from ekbs.ingest.figure_enhance import classify_figure, enhance_figure_file
from ekbs.ingest.scan_review import (
    STATUS_REVIEW,
    figures_from_sidecar,
    load_review_markdown,
    load_review_sidecar,
    page_map_from_sidecar,
    tables_from_sidecar,
    upgrade_review_draft_seals,
    upgrade_review_draft_tables,
    upgrade_review_draft_tail,
    save_review_asset_edits,
    write_review_markdown,
)
from ekbs.logging import get_logger
from ekbs.permission.router import bump_policy_version

router = APIRouter(tags=["parse"])
log = get_logger(component="api.parse")

VALID_STATUSES = (
    "pending",
    "parsing",
    "review",
    "chunked",
    "indexing",
    "published",
    "failed",
    "offline",
)
MAX_PAGE_SIZE = 200
MAX_DRAFT_CHARS = 2_000_000


class DraftTableEdit(BaseModel):
    table_id: str = ""
    caption: str = ""
    body_html: str = ""
    page: int | None = None


class DraftFigureEdit(BaseModel):
    figure_id: str = ""
    caption: str = ""
    ocr_text: str | None = None


class DraftBody(BaseModel):
    markdown: str = Field(..., description="复核后的全文 Markdown")
    tables: list[DraftTableEdit] | None = Field(
        default=None, description="传入则覆盖表格清单；不传保持原表"
    )
    figures: list[DraftFigureEdit] | None = Field(
        default=None, description="传入则按 figure_id 保留并改题注；不传保持原图"
    )


class ApproveBody(BaseModel):
    markdown: str | None = Field(default=None, description="可选：通过前覆盖草稿")


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
            "d.checksum, d.doc_version, d.created_at, d.updated_at, d.parse_ms, "
            "(SELECT COUNT(*) FROM ekbs_chunk c WHERE c.doc_id = d.doc_id) AS chunk_count, "
            "(SELECT COUNT(*) FROM ekbs_chunk c WHERE c.doc_id = d.doc_id AND c.chunk_type='child') "
            "  AS child_count, "
            # 真正进了向量/FTS 的块数（= 叶子块）。代码文档的 child_count 会是 0，
            # 用这个字段才不会误判成"没索引"（口径见 ingest.retrievable）
            "(SELECT COUNT(*) FROM ekbs_chunk c WHERE c.doc_id = d.doc_id AND "
            f"{RETRIEVABLE_PREDICATE}) AS retrievable_count, "
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
    status: str | None = Query(
        default=None,
        description="pending|parsing|review|indexing|published|failed|offline",
    ),
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
    else:
        # 清洗解析是"进行中"工作台：已发布/已下线不再出现在这里；
        # 它们去文档管理/分块预览，避免已完成的文档一直占着流程列表。
        where.append("d.status NOT IN ('published', 'offline', 'deleted')")
    if kb_id is not None:
        where.append("d.kb_id = ?")
        params.append(int(kb_id))
    sql = (
        "SELECT d.doc_id AS task_id, d.kb_id, d.title, d.status, d.level, d.page_count, "
        "d.created_at, d.updated_at, d.parse_ms, "
        "(SELECT COUNT(*) FROM ekbs_chunk c WHERE c.doc_id = d.doc_id) AS chunk_count "
        "FROM ekbs_document d "
        f"WHERE {' AND '.join(where)} ORDER BY d.updated_at DESC, d.doc_id DESC LIMIT ? OFFSET ?"
    )
    count_sql = f"SELECT COUNT(*) AS c FROM ekbs_document d WHERE {' AND '.join(where)}"
    with runtime.db.sync_connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, (*params, limit, offset)).fetchall()]
        total = int(conn.execute(count_sql, tuple(params)).fetchone()["c"])
        # 按状态汇总（只汇总进行中）；成功率另用全量终态算
        all_by_status = {
            str(r["status"]): int(r["c"])
            for r in conn.execute(
                "SELECT status, COUNT(*) AS c FROM ekbs_document GROUP BY status"
            ).fetchall()
        }
        by_status = {
            k: v for k, v in all_by_status.items()
            if k not in ("published", "offline", "deleted")
        }
    # 成功率只看终态（published/failed/offline），排除 pending/parsing/review/indexing，
    # 否则「待复核」堆积会把成功率虚低，误触 98% 验收线。
    published = all_by_status.get("published", 0)
    terminal = published + all_by_status.get("failed", 0) + all_by_status.get("offline", 0)
    success_rate = round(published / terminal, 4) if terminal else 0.0
    return JSONResponse(
        {
            "items": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
            "by_status": by_status,
            "success_rate": success_rate,
            "success_rate_target": 0.98,
            "note": "task_id 即 doc_id；列表只含进行中（pending/parsing/review/chunked/indexing/failed）；"
            "已发布/已下线请到文档管理；success_rate = published / (published+failed+offline)",
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
    """解析结果视图：待复核时读草稿；已分块后由分块拼回预览。"""
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "parse:task:read")
    runtime = request.app.state.runtime
    row = _task_row(runtime, task_id)
    status = str(row["status"])
    if status == STATUS_REVIEW:
        try:
            draft = load_review_markdown(runtime.settings.data_dir, task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        sidecar = load_review_sidecar(runtime.settings.data_dir, task_id)
        return JSONResponse(
            {
                "task_id": task_id,
                "status": status,
                "structure": [],
                "chunk_count": 0,
                # 只给短预览：全文走 /draft 与双栏页，避免旧前端 alert/Drawer 再倾倒整篇
                "preview": draft[: min(preview_chars, 500)],
                "preview_truncated": True,
                "engine": sidecar.engine,
                "needs_review": True,
                "note": "扫描件待人工复核：请打开 /admin/parse/review/{task_id} 双栏对照；"
                "全文仅通过 GET /draft 提供，不再由本接口回传。",
            }
        )
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
            "status": status,
            "structure": structure,
            "chunk_count": len(structure),
            "preview": markdown[:preview_chars],
            "preview_truncated": len(markdown) > preview_chars,
            "needs_review": False,
            "note": "已分块预览由 ekbs_chunk 拼回；扫描件复核草稿在 status=review 时可读",
        }
    )


@router.get("/parse/tasks/{task_id}/draft")
async def get_parse_draft(
    task_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """读取扫描件复核草稿（全文）。"""
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "parse:task:read")
    runtime = request.app.state.runtime
    row = _task_row(runtime, task_id)
    status = str(row["status"])
    if status not in (STATUS_REVIEW, "published"):
        raise HTTPException(
            status_code=409,
            detail=f"仅待复核或已入库文档可看对照（当前 {status}）",
        )
    # 旧草稿只在待复核时升级。已入库的稿子不能再改磁盘。
    if status == STATUS_REVIEW:
        seal_stats = upgrade_review_draft_seals(runtime.settings.data_dir, task_id)
        if seal_stats.get("seal_figures_dropped"):
            log.info("review_draft_seals_upgraded", task_id=task_id, **seal_stats)
        upgrade_stats = upgrade_review_draft_tables(runtime.settings.data_dir, task_id)
        if upgrade_stats.get("tables_merged_pairs") or upgrade_stats.get("tables_repaired"):
            log.info("review_draft_tables_upgraded", task_id=task_id, **upgrade_stats)
        tail_stats = upgrade_review_draft_tail(
            runtime.settings.data_dir,
            task_id,
            str(row["source_path"] or ""),
        )
        if tail_stats:
            log.info("review_draft_tail_upgraded", task_id=task_id, **tail_stats)
    try:
        markdown = load_review_markdown(runtime.settings.data_dir, task_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    sidecar = load_review_sidecar(runtime.settings.data_dir, task_id)
    tables = []
    for item in sidecar.tables:
        body_html = str(item.get("body") or "")
        tables.append(
            {
                "table_id": str(item.get("table_id") or ""),
                "caption": str(item.get("caption") or ""),
                "page": item.get("page"),
                "body_html": body_html,
                "chars": len(body_html),
            }
        )
    figures = []
    parse_root = Path(runtime.settings.data_paths()["parse"]).resolve()
    for item in sidecar.figures:
        fid = str(item.get("figure_id") or "")
        rel = str(item.get("image_path") or "").strip()
        kind = "handwriting" if fid.startswith("handwrite") else "normal"
        ink_frac = None
        if fid or rel:
            candidates = []
            if rel:
                candidates.append((runtime.settings.data_dir / rel).resolve())
            if fid:
                candidates.extend(
                    [
                        parse_root / str(task_id) / "images" / fid,
                        parse_root / str(task_id) / "images" / f"{fid}.jpg",
                        parse_root / str(task_id) / "images" / f"{fid}.png",
                    ]
                )
            for cand in candidates:
                try:
                    cand.relative_to(parse_root)
                except Exception:
                    continue
                if cand.is_file():
                    if kind != "handwriting":
                        try:
                            meta = classify_figure(cand)
                            kind = meta.kind
                            ink_frac = round(meta.ink_frac, 4)
                        except Exception:
                            pass
                    break
        figures.append(
            {
                "figure_id": fid,
                "caption": str(item.get("caption") or ""),
                "ocr_text": str(item.get("ocr_text") or ""),
                "page": item.get("page"),
                "image_path": rel,
                "has_image": bool(fid),
                "kind": kind,
                "ink_frac": ink_frac,
                "display_hint": (
                    "淡色附图，预览已自动增强对比"
                    if kind in ("faint", "blank")
                    else ""
                ),
            }
        )
    return JSONResponse(
        {
            "task_id": task_id,
            "status": status,
            "read_only": status != STATUS_REVIEW,
            "markdown": markdown,
            "chars": len(markdown),
            "engine": sidecar.engine,
            "parse_ms": row["parse_ms"],
            "figure_count": len(figures),
            "table_count": len(tables),
            "tables": tables,
            "figures": figures,
            "clean_stats": sidecar.clean_stats,
            "note": "正文里的 [表格 N] 是占位；完整表格见 tables[].body_html。"
            "图片在 figures[]，用 /parse/tasks/{id}/figures/{figure_id} 取图。",
        }
    )


_FIGURE_ID_SAFE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


@router.get("/parse/tasks/{task_id}/figures/{figure_id}", response_model=None)
async def get_parse_draft_figure(
    task_id: int,
    figure_id: str,
    request: Request,
    enhance: int = Query(default=1, ge=0, le=1, description="1=淡色附图增强预览（默认开）"),
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
):
    """复核期回源解析产物图（落在 ``parse/<doc_id>/images/``）。越权/越界一律 404。

    默认 ``enhance=1``：淡色附图自动抬升墨迹并衬深色底。印章图在清洗时已丢弃，不会出现在这里。
    """
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "parse:task:read")
    if not _FIGURE_ID_SAFE.match(figure_id or ""):
        raise HTTPException(status_code=404, detail="not_found")
    runtime = request.app.state.runtime
    row = _task_row(runtime, task_id)
    if str(row["status"]) not in (STATUS_REVIEW, "published"):
        raise HTTPException(
            status_code=409,
            detail=f"仅待复核或已入库文档可看解析期图片（当前 {row['status']}）",
        )
    sidecar = load_review_sidecar(runtime.settings.data_dir, task_id)
    match = next(
        (
            item
            for item in sidecar.figures
            if str(item.get("figure_id") or "") == figure_id
        ),
        None,
    )
    if match is None:
        raise HTTPException(status_code=404, detail="not_found")

    parse_root = Path(runtime.settings.data_paths()["parse"]).resolve()
    rel = str(match.get("image_path") or "").strip()
    if not rel:
        candidates = [
            parse_root / str(task_id) / "images" / figure_id,
            parse_root / str(task_id) / "images" / f"{figure_id}.jpg",
            parse_root / str(task_id) / "images" / f"{figure_id}.png",
        ]
    else:
        candidates = [(runtime.settings.data_dir / rel).resolve()]

    candidate: Path | None = None
    for path in candidates:
        try:
            resolved = path.resolve()
            resolved.relative_to(parse_root)
        except Exception:
            continue
        if resolved.is_file():
            candidate = resolved
            break
    if candidate is None:
        raise HTTPException(status_code=404, detail="not_found")

    if enhance:
        try:
            data, media_type, meta = enhance_figure_file(candidate, slate_bg=True)
            return Response(
                content=data,
                media_type=media_type,
                headers={
                    "Content-Disposition": f'inline; filename="{candidate.stem}-enhanced.jpg"',
                    "X-EKBS-Figure-Kind": meta.kind,
                    "Cache-Control": "private, max-age=60",
                },
            )
        except Exception as exc:
            log.warning("figure_enhance_failed", path=str(candidate), error=str(exc))

    media_type, _ = mimetypes.guess_type(candidate.name)
    return FileResponse(
        candidate,
        media_type=media_type or "application/octet-stream",
        filename=candidate.name,
        content_disposition_type="inline",
    )


@router.put("/parse/tasks/{task_id}/draft")
async def put_parse_draft(
    task_id: int,
    body: DraftBody,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """保存人工修订后的草稿（尚不分块）。"""
    principal = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "parse:task:manage"
    )
    runtime = request.app.state.runtime
    row = _task_row(runtime, task_id)
    if str(row["status"]) != STATUS_REVIEW:
        raise HTTPException(
            status_code=409,
            detail=f"仅 review 状态可改草稿（当前 {row['status']}）",
        )
    text = body.markdown or ""
    if len(text) > MAX_DRAFT_CHARS:
        raise HTTPException(status_code=422, detail=f"草稿过长（>{MAX_DRAFT_CHARS} 字符）")
    try:
        save_review_asset_edits(
            runtime.settings.data_dir,
            task_id,
            text,
            tables=None if body.tables is None else [item.model_dump() for item in body.tables],
            figures=None if body.figures is None else [item.model_dump() for item in body.figures],
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_document SET updated_at = datetime('now') WHERE doc_id = ?",
            (task_id,),
        )
        conn.execute(
            "INSERT INTO ekbs_perm_change_log(operator_id, object_type, object_id, change_type, "
            "field, old_value, new_value, reason) "
            "VALUES (?, 'document', ?, 'review_edit', 'draft', NULL, ?, 'api:draft')",
            (principal.user_id, str(task_id), f"chars={len(text)}"),
        )
        conn.commit()
    return JSONResponse({"task_id": task_id, "status": STATUS_REVIEW, "chars": len(text)})


@router.post("/parse/tasks/{task_id}/approve")
async def approve_parse_task(
    task_id: int,
    request: Request,
    body: ApproveBody | None = None,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """人工确认复核通过 → **只分块**（状态 chunked），不向量、不发布。

    向量入库由清洗解析页的"向量入库/批量向量入库"触发。
    可选 body.markdown：通过前最后一次覆盖草稿（便于粘贴 WPS 识别结果）。
    """
    principal = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "parse:task:manage"
    )
    runtime = request.app.state.runtime
    row = _task_row(runtime, task_id)
    if str(row["status"]) != STATUS_REVIEW:
        raise HTTPException(
            status_code=409,
            detail=f"仅 review 状态可确认通过（当前 {row['status']}）",
        )
    if body is not None and body.markdown is not None:
        text = body.markdown
        if len(text) > MAX_DRAFT_CHARS:
            raise HTTPException(status_code=422, detail=f"草稿过长（>{MAX_DRAFT_CHARS} 字符）")
        write_review_markdown(runtime.settings.data_dir, task_id, text)
    try:
        markdown = load_review_markdown(runtime.settings.data_dir, task_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not markdown.strip():
        raise HTTPException(status_code=422, detail="草稿为空，无法分块入库")

    sidecar = load_review_sidecar(runtime.settings.data_dir, task_id)
    try:
        visible = json.loads(row["visible_dept_ids"] or "[]")
    except ValueError:
        visible = []

    # 复核确认只做"分块落库"：不嵌入、不写分片、不发布。
    # 向量入库由清洗解析页的"向量入库/批量向量入库"触发（单写者约束下由 worker 执行）。
    ingest = IngestPipeline(
        db=runtime.db,
        settings=runtime.settings,
        providers=runtime.providers,
        shards=runtime.shards,
        fts=runtime.fts,
        figure_index=runtime.figure_index,
    )
    try:
        result = await ingest.ingest_markdown(
            title=str(row["title"]),
            markdown=markdown,
            kb_id=int(row["kb_id"]),
            level=int(row["level"]),
            visible_dept_ids=list(visible),
            source_path=str(row["source_path"] or ""),
            doc_version=str(row["doc_version"] or "1"),
            page_map=page_map_from_sidecar(sidecar),
            figures=[
                fig for fig in figures_from_sidecar(sidecar)
                if not str(fig.figure_id or "").startswith("handwrite")
            ],
            tables=tables_from_sidecar(sidecar),
            existing_doc_id=task_id,
            checksum=(str(row["checksum"]) if row["checksum"] else None),
            defer_vector=True,
        )
    except Exception as exc:
        log.error("review_approve_failed", task_id=task_id, error=str(exc)[:240])
        with runtime.db.sync_connect() as conn:
            conn.execute(
                "UPDATE ekbs_document SET status = 'failed', updated_at = datetime('now') "
                "WHERE doc_id = ?",
                (task_id,),
            )
            conn.commit()
        raise HTTPException(status_code=500, detail=f"分块失败：{exc}") from exc

    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_perm_change_log(operator_id, object_type, object_id, change_type, "
            "field, old_value, new_value, reason) "
            "VALUES (?, 'document', ?, 'review_approve', 'status', 'review', 'chunked', "
            "'api:approve:chunked')",
            (principal.user_id, str(task_id)),
        )
        conn.commit()
    log.info(
        "review_approved",
        task_id=task_id,
        operator=principal.user_id,
        doc_id=getattr(result, "doc_id", task_id),
    )
    return JSONResponse(
        {
            "task_id": task_id,
            "status": "chunked",
            "previous_status": STATUS_REVIEW,
            "parent_count": result.parent_count,
            "child_count": result.child_count,
            "vector_count": 0,
        }
    )


@router.post("/parse/tasks/{task_id}/vectorize")
async def vectorize_parse_task(
    task_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """对已分块（chunked）文档做向量入库。

    - single 布局：API 同步对现有块 embedding 并发布；
    - segmented 布局：置 pending_index，由 worker 执行（单写者）。
    """
    principal = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "parse:task:manage"
    )
    runtime = request.app.state.runtime
    row = _task_row(runtime, task_id)
    if str(row["status"]) != "chunked":
        raise HTTPException(
            status_code=409,
            detail=f"仅已分块待入库（chunked）状态可向量入库（当前 {row['status']}）",
        )
    if runtime.settings.index.layout == "segmented":
        with runtime.db.sync_connect() as conn:
            conn.execute(
                "UPDATE ekbs_document SET status = 'pending_index', updated_at = datetime('now') "
                "WHERE doc_id = ? AND status = 'chunked'",
                (task_id,),
            )
            conn.execute(
                "INSERT INTO ekbs_perm_change_log(operator_id, object_type, object_id, change_type, "
                "field, old_value, new_value, reason) "
                "VALUES (?, 'document', ?, 'vectorize_queued', 'status', 'chunked', 'pending_index', "
                "'api:vectorize:queued')",
                (principal.user_id, str(task_id)),
            )
            conn.commit()
        log.info("vectorize_queued", task_id=task_id, operator=principal.user_id)
        return JSONResponse({"task_id": task_id, "status": "pending_index"})

    ingest = IngestPipeline(
        db=runtime.db,
        settings=runtime.settings,
        providers=runtime.providers,
        shards=runtime.shards,
        fts=runtime.fts,
        figure_index=runtime.figure_index,
    )
    try:
        result = await ingest.index_existing_document(task_id)
    except Exception as exc:
        log.error("vectorize_failed", task_id=task_id, error=str(exc)[:240])
        with runtime.db.sync_connect() as conn:
            conn.execute(
                "UPDATE ekbs_document SET status = 'failed', updated_at = datetime('now') "
                "WHERE doc_id = ?",
                (task_id,),
            )
            conn.commit()
        raise HTTPException(status_code=500, detail=f"向量入库失败：{exc}") from exc
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_perm_change_log(operator_id, object_type, object_id, change_type, "
            "field, old_value, new_value, reason) "
            "VALUES (?, 'document', ?, 'vectorize', 'status', 'chunked', 'published', "
            "'api:vectorize')",
            (principal.user_id, str(task_id)),
        )
        conn.commit()
    log.info("vectorized", task_id=task_id, operator=principal.user_id, vector_count=result.vector_count)
    return JSONResponse(
        {
            "task_id": task_id,
            "status": "published",
            "previous_status": "chunked",
            "parent_count": result.parent_count,
            "child_count": result.child_count,
            "vector_count": result.vector_count,
        }
    )


class ApproveBatchBody(BaseModel):
    task_ids: list[int] = Field(min_length=1, max_length=200)


@router.post("/parse/tasks/approve-batch")
async def approve_parse_tasks_batch(
    payload: ApproveBatchBody,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """批量"向量入库"：把 chunked 文档置 pending_index，由 worker 统一处理。

    仅 ``index.layout=segmented`` 可用（单写者约束）；single 布局仍走单篇同步 approve。
    """
    principal = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "parse:task:manage"
    )
    runtime = request.app.state.runtime
    if runtime.settings.index.layout != "segmented":
        raise HTTPException(
            status_code=409,
            detail="批量向量入库需要 index.layout=segmented（单写者模式）",
        )
    queued: list[int] = []
    skipped: list[dict[str, Any]] = []
    with runtime.db.sync_connect() as conn:
        for task_id in payload.task_ids:
            row = conn.execute(
                "SELECT status FROM ekbs_document WHERE doc_id = ?", (task_id,)
            ).fetchone()
            if row is None or str(row["status"]) != "chunked":
                skipped.append({"task_id": task_id, "reason": "not_chunked"})
                continue
            conn.execute(
                "UPDATE ekbs_document SET status = 'pending_index', updated_at = datetime('now') "
                "WHERE doc_id = ? AND status = 'chunked'",
                (task_id,),
            )
            conn.execute(
                "INSERT INTO ekbs_perm_change_log(operator_id, object_type, object_id, change_type, "
                "field, old_value, new_value, reason) "
                "VALUES (?, 'document', ?, 'vectorize_batch_queued', 'status', 'chunked', 'pending_index', "
                "'api:vectorize_batch:queued')",
                (principal.user_id, str(task_id)),
            )
            queued.append(task_id)
        conn.commit()
    log.info("vectorize_batch_queued", operator=principal.user_id, queued=len(queued), skipped=len(skipped))
    return JSONResponse({"queued": queued, "skipped": skipped, "status": "pending_index"})


@router.post("/parse/tasks/{task_id}/reject")
async def reject_parse_task(
    task_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """驳回复核（review → failed）；可再 retry 重新解析。"""
    principal = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "parse:task:manage"
    )
    runtime = request.app.state.runtime
    row = _task_row(runtime, task_id)
    if str(row["status"]) != STATUS_REVIEW:
        raise HTTPException(
            status_code=409,
            detail=f"仅 review 状态可驳回（当前 {row['status']}）",
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
            "VALUES (?, 'document', ?, 'review_reject', 'status', 'review', 'failed', "
            "'api:reject')",
            (principal.user_id, str(task_id)),
        )
        version = bump_policy_version(conn, reason=f"api:reject:doc={task_id}")
        conn.commit()
    return JSONResponse(
        {
            "task_id": task_id,
            "status": "failed",
            "previous_status": STATUS_REVIEW,
            "policy_version": version,
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
            "index_layout": runtime.settings.index.layout,
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
    # review / failed / offline / pending → pending（扫描件可放弃草稿重跑 OCR）
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
