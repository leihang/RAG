"""管理台**只读**运维接口 —— 路径与权限点严格取自 `05` §8.1/§8.2 的权威映射表。

为什么先做只读的那几条：没有它们，运维只能靠 SQL 直连生产库（`01` §16 的运维命令清单就是
这么写的），既容易误改、又无法按角色授权。有了这几条，「看审计 / 看趋势 / 看分片健康」
就能交给 auditor 角色去做，而不用给他数据库账号。

| 端点 | 权限点（`05` §8.2） |
|---|---|
| ``GET /api/v1/logs/qa/sessions`` | ``audit:qa:read`` |
| ``GET /api/v1/logs/qa/sessions/{session_id}`` | ``audit:qa:read`` |
| ``GET /api/v1/audit/security-events`` | ``audit:qa:read_all`` |
| ``GET /api/v1/dashboard/overview`` | ``sys:dashboard:read`` |
| ``GET /api/v1/system/health`` | ``sys:health:read`` |
| ``GET /api/v1/system/shards`` | ``sys:health:read`` |
| ``GET /api/v1/docs/documents`` | ``doc:document:read`` |
| ``GET /api/v1/docs/documents/{doc_id}/source`` | ``doc:document:read``（原件回源，复核对照） |
| ``GET /api/v1/docs/documents/{doc_id}/source-preview`` | ``doc:document:read``（Word 等转 HTML，嵌左栏） |

**两条安全约束**（都在实现里）：

* 审计与文档接口**不接受调用方指定「以谁的身份看」** —— 管理台看到的是**全量**审计，
  因此必须由服务端按权限点判定，而不是按请求参数；
* 审计内容**不回传片段全文**（`ekbs_qa_citation` 只存 hash 与页码，`01` §5.4 的设计意图），
  本模块照此返回，不做"顺手查一下 chunk 正文"这种事。
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from ekbs.api.auth import INTERNAL_TOKEN_HEADER, INTERNAL_USER_HEADER
from ekbs.api.qa import _authenticate
from ekbs.authz import SUPER_ADMIN
from ekbs.errors import PermissionDenied
from ekbs.ingest.office_preview import OfficePreviewError, render_office_preview
from ekbs.logging import get_logger
from ekbs.permission.console import is_console_role, widen_for_console
from ekbs.observability.alerts import ALERT_RULES, evaluate_all
import contextlib

router = APIRouter(tags=["admin"])
log = get_logger(component="api.admin")

#: 分页护栏：再大也不许一次拉全库
MAX_PAGE_SIZE = 200


def _principal(request: Request, headers: dict[str, str | None], code: str) -> Any:
    """鉴权（401）→ 授权（403）→ 返回 ``Principal``。"""
    runtime = request.app.state.runtime
    user = _authenticate(request, headers)
    try:
        return runtime.authz.require(user.user_id, code)
    except PermissionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(int(limit), MAX_PAGE_SIZE)), max(0, int(offset))


# ---------------------------------------------------------------------------
# 问答日志与审计（05 §8.1 的 /logs/qa/*）
# ---------------------------------------------------------------------------


@router.get("/logs/qa/sessions")
async def list_qa_sessions(
    request: Request,
    user_id: int | None = Query(default=None),
    channel: str | None = Query(default=None),
    since: str | None = Query(default=None, description="YYYY-MM-DD"),
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """问答会话列表（审计只读）。"""
    principal = _principal(
        request,
        {
            "authorization": authorization,
            INTERNAL_TOKEN_HEADER.lower(): x_internal_token,
            INTERNAL_USER_HEADER.lower(): x_internal_user_id,
        },
        "audit:qa:read",
    )
    runtime = request.app.state.runtime
    limit, offset = _page(limit, offset)
    where = ["1=1"]
    params: list[Any] = []
    if user_id is not None:
        where.append("s.user_id = ?")
        params.append(int(user_id))
    if channel:
        where.append("s.channel = ?")
        params.append(str(channel))
    if since:
        where.append("s.started_at >= ?")
        params.append(f"{since} 00:00:00")
    # 按**最后活动时间**排序：同一会话可以持续很久（多轮追问），按 started_at 排会把
    # "刚问完、但会话开得早"的记录压到列表底部，用户会以为"最新的问答没进日志"。
    # 同时把逐轮问题/拒答从 ekbs_qa_message 汇总上来（会话表本身没有这两列），
    # 并 JOIN 用户表带出昵称 —— 只给数字 user_id 时运维分不清是钉钉用户还是管理台本地账号。
    #
    # 拒答按**答案内容**判定，不直接数 `refused=1`：历史记录里有一批旧版
    # `missing_citation` 硬拒留下的误判（refused=1 但答案正常），照搬标记会把
    # 正常回答算成拒答（实测「部分拒答 1/13」里那 1 轮其实是正常回答）。
    refusal_like = "ltrim(TRIM(m.answer), '*#>-—・ 「『') LIKE "
    refusal_cond = (
        "m.answer IS NULL OR TRIM(m.answer) = '' "
        f"OR {refusal_like}'未找到可靠依据%' "
        f"OR {refusal_like}'对不起%' "
        f"OR {refusal_like}'没有找到%'"
    )
    sql = (
        "SELECT s.session_id, s.user_id, s.dt_userid, u.nick AS user_nick, u.auth_source, "
        "s.channel, s.conversation_id, "
        "s.perm_fingerprint, s.clearance_at, s.started_at, s.ended_at, s.message_count, "
        "(SELECT COUNT(*) FROM ekbs_qa_message m WHERE m.session_id = s.session_id "
        f" AND m.role = 'assistant' AND ({refusal_cond})) AS refused_count, "
        "(SELECT COUNT(*) FROM ekbs_qa_message m WHERE m.session_id = s.session_id "
        " AND m.role = 'assistant') AS turn_count, "
        "(SELECT m.question FROM ekbs_qa_message m WHERE m.session_id = s.session_id "
        " AND m.role = 'assistant' ORDER BY m.message_id DESC LIMIT 1) AS last_question, "
        # 最新一轮是否拒答：行上展示的是「最新问题」，拒答状态也必须取**最新一轮**——
        # 否则 22 轮里第 9 轮因问题太模糊被拒，最新一问回答正常，整行仍显示「部分拒答 1/22」。
        "(SELECT CASE WHEN "
        f"({refusal_cond}) THEN 1 ELSE 0 END "
        "FROM ekbs_qa_message m WHERE m.session_id = s.session_id "
        " AND m.role = 'assistant' ORDER BY m.message_id DESC LIMIT 1) AS last_refused "
        "FROM ekbs_qa_session s LEFT JOIN ekbs_user u ON u.user_id = s.user_id "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY COALESCE(s.ended_at, s.started_at) DESC LIMIT ? OFFSET ?"
    )
    with runtime.db.sync_connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, (*params, limit, offset)).fetchall()]
        total = int(
            conn.execute(
                f"SELECT COUNT(*) FROM ekbs_qa_session s WHERE {' AND '.join(where)}",
                tuple(params),
            ).fetchone()[0]
        )
    # 指纹只回前 12 位：它是缓存分区键，完整值对审计无用但可用于关联
    for row in rows:
        row["perm_fingerprint"] = str(row["perm_fingerprint"])[:12]
    return JSONResponse(
        {
            "items": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
            "requested_by": principal.user_id,
        }
    )


@router.get("/logs/qa/sessions/{session_id}")
async def get_qa_session(
    session_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """会话详情：消息（含耗时拆解）与引用。"""
    _principal(
        request,
        {
            "authorization": authorization,
            INTERNAL_TOKEN_HEADER.lower(): x_internal_token,
            INTERNAL_USER_HEADER.lower(): x_internal_user_id,
        },
        "audit:qa:read",
    )
    runtime = request.app.state.runtime
    with runtime.db.sync_connect() as conn:
        session = conn.execute(
            "SELECT * FROM ekbs_qa_session WHERE session_id = ?", (session_id,)
        ).fetchone()
        if session is None:
            raise HTTPException(status_code=404, detail="not_found")
        messages = [
            dict(r)
            for r in conn.execute(
                "SELECT message_id, turn, role, question, rewritten_query, answer, model, "
                "prompt_tokens, completion_tokens, ttft_ms, total_ms, retrieve_ms, rerank_ms, "
                "generate_ms, candidate_count, rerank_top_score, refused, trace_id, "
                "context_chunk_ids, created_at "
                "FROM ekbs_qa_message WHERE session_id = ? ORDER BY turn, message_id",
                (session_id,),
            ).fetchall()
        ]
        citations = [
            dict(r)
            for r in conn.execute(
                "SELECT c.* FROM ekbs_qa_citation c JOIN ekbs_qa_message m "
                "ON m.message_id = c.message_id WHERE m.session_id = ? ORDER BY c.message_id, "
                "c.cite_index",
                (session_id,),
            ).fetchall()
        ]
    body = dict(session)
    body["perm_fingerprint"] = str(body["perm_fingerprint"])[:12]
    return JSONResponse({"session": body, "messages": messages, "citations": citations})


@router.get("/audit/security-events")
async def list_security_events(
    request: Request,
    event_type: str | None = Query(default=None),
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """安全事件（越权尝试 / 幻觉引用 / 内容拦截 / 指标告警）。

    权限点是 ``audit:qa:read_all``（`05` §8.2）—— 比普通审计更高一档，
    因为它包含越权与注入尝试的细节。
    """
    _principal(
        request,
        {
            "authorization": authorization,
            INTERNAL_TOKEN_HEADER.lower(): x_internal_token,
            INTERNAL_USER_HEADER.lower(): x_internal_user_id,
        },
        "audit:qa:read_all",
    )
    runtime = request.app.state.runtime
    limit, offset = _page(limit, offset)
    where = ["1=1"]
    params: list[Any] = []
    if event_type:
        where.append("event_type = ?")
        params.append(str(event_type))
    with runtime.db.sync_connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT event_id, event_type, user_id, severity, detail, created_at "
                f"FROM ekbs_security_event WHERE {' AND '.join(where)} "
                "ORDER BY event_id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        ]
    return JSONResponse({"items": rows, "limit": limit, "offset": offset})


@router.get("/logs/doc-access")
async def list_doc_access(
    request: Request,
    doc_id: int | None = Query(default=None),
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """文档访问日志（权限点 ``audit:trace:read``）。"""
    _principal(
        request,
        {
            "authorization": authorization,
            INTERNAL_TOKEN_HEADER.lower(): x_internal_token,
            INTERNAL_USER_HEADER.lower(): x_internal_user_id,
        },
        "audit:trace:read",
    )
    runtime = request.app.state.runtime
    limit, offset = _page(limit, offset)
    where = ["1=1"]
    params: list[Any] = []
    if doc_id is not None:
        where.append("doc_id = ?")
        params.append(int(doc_id))
    with runtime.db.sync_connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT log_id, user_id, doc_id, action, channel, ip, ua, result, created_at "
                f"FROM ekbs_doc_access_log WHERE {' AND '.join(where)} "
                "ORDER BY log_id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
        ]
    return JSONResponse({"items": rows, "limit": limit, "offset": offset})


# ---------------------------------------------------------------------------
# 看板与系统健康
# ---------------------------------------------------------------------------


@router.get("/dashboard/overview")
async def dashboard_overview(
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """核心指标卡（权限点 ``sys:dashboard:read``）。

    数据来源是 ``ekbs_metric``（采集作业每 60s 写入）+ 实时聚合。
    """
    _principal(
        request,
        {
            "authorization": authorization,
            INTERNAL_TOKEN_HEADER.lower(): x_internal_token,
            INTERNAL_USER_HEADER.lower(): x_internal_user_id,
        },
        "sys:dashboard:read",
    )
    runtime = request.app.state.runtime
    with runtime.db.sync_connect() as conn:
        latest = {
            str(r["name"]): float(r["value"])
            for r in conn.execute(
                "SELECT name, value FROM ekbs_metric WHERE metric_id IN "
                "(SELECT MAX(metric_id) FROM ekbs_metric GROUP BY name)"
            ).fetchall()
        }
        qa_total = int(conn.execute("SELECT COUNT(*) FROM ekbs_qa_message WHERE answer IS NOT NULL").fetchone()[0])
        refused = int(
            conn.execute("SELECT COUNT(*) FROM ekbs_qa_message WHERE refused = 1").fetchone()[0]
        )
        docs_published = int(
            conn.execute("SELECT COUNT(*) FROM ekbs_document WHERE status = 'published'").fetchone()[0]
        )
        docs_failed = int(
            conn.execute("SELECT COUNT(*) FROM ekbs_document WHERE status = 'failed'").fetchone()[0]
        )
        pending_review = int(
            conn.execute("SELECT COUNT(*) FROM ekbs_document WHERE status = 'pending'").fetchone()[0]
        )
        recent_alerts = [
            dict(r)
            for r in conn.execute(
                "SELECT event_id, event_type, severity, detail, created_at "
                "FROM ekbs_security_event WHERE event_type = 'metric_alert' "
                "ORDER BY event_id DESC LIMIT 10"
            ).fetchall()
        ]
    active_alerts = [hit.as_dict() for hit in evaluate_all(runtime.db)]
    return JSONResponse(
        {
            "qa_total": qa_total,
            "refused_total": refused,
            "refusal_rate": round(refused / qa_total, 4) if qa_total else 0.0,
            "documents": {
                "published": docs_published,
                "failed": docs_failed,
                "pending": pending_review,
            },
            "shards": {
                "active": runtime.shards.count_active(),
                "vectors": runtime.shards.total_vectors(),
            },
            "disk_used_pct": runtime.disk_used_pct(),
            "metrics": latest,
            "active_alerts": active_alerts,
            "recent_alert_events": recent_alerts,
        }
    )


@router.get("/system/health")
async def system_health(
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """系统健康（含 SQLite / 磁盘水位）—— 权限点 ``sys:health:read``。

    注意：这是**管理台视图**，与 ``/healthz`` ``/readyz`` 的探针语义不同
    （那三个是给编排系统用的，不出网、不查库统计）。
    """
    _principal(
        request,
        {
            "authorization": authorization,
            INTERNAL_TOKEN_HEADER.lower(): x_internal_token,
            INTERNAL_USER_HEADER.lower(): x_internal_user_id,
        },
        "sys:health:read",
    )
    runtime = request.app.state.runtime
    schema = runtime.db.verify_schema()
    checks = runtime.shards.self_check()
    return JSONResponse(
        {
            "schema": {
                "ok": schema.ok,
                "tables": schema.tables,
                "triggers": schema.triggers,
                "policy_version": schema.policy_version,
                "journal_mode": schema.journal_mode,
                "problems": schema.problems(),
            },
            "db_reachable": await runtime.db.ping(),
            "data_dir_writable": runtime.data_dir_writable(),
            "disk_used_pct": runtime.disk_used_pct(),
            "embedding": {
                "model": runtime.providers.embed.ref.ident,
                "fingerprint": runtime.providers.embed.ref.fingerprint[:12],
                "fingerprint_match": runtime.fingerprint_match(),
            },
            "shards": [
                {"shard_id": c.shard_id, "ok": c.ok, "detail": c.detail} for c in checks
            ],
            "gates": {
                "corpus_egress_ack": runtime.settings.permission.corpus_egress_ack,
                "l3_ingest_enabled": runtime.settings.permission.l3_ingest_enabled,
                "l4_enabled": runtime.settings.permission.l4_enabled,
            },
        }
    )


@router.get("/system/shards")
async def system_shards(
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """分片清单（向量数/维度/指纹/状态）—— 权限点 ``sys:health:read``。"""
    _principal(
        request,
        {
            "authorization": authorization,
            INTERNAL_TOKEN_HEADER.lower(): x_internal_token,
            INTERNAL_USER_HEADER.lower(): x_internal_user_id,
        },
        "sys:health:read",
    )
    runtime = request.app.state.runtime
    checks = {c.shard_id: c for c in runtime.shards.self_check()}
    items = []
    for row in runtime.shards.registry():
        check = checks.get(row.shard_id)
        items.append(
            {
                "shard_id": row.shard_id,
                "dept_code": row.dept_code,
                "level": row.level,
                "dim": row.dim,
                "vector_count": row.vector_count,
                "index_type": row.index_type,
                "status": row.status,
                "embedding_fingerprint": row.embedding_fingerprint[:12],
                "self_check_ok": bool(check and check.ok),
                "self_check_detail": check.detail if check else "",
            }
        )
    from ekbs.personal import attach_personal_owners

    with runtime.db.sync_connect() as conn:
        attach_personal_owners(conn, items)
    return JSONResponse({"items": items, "count": len(items)})


# ---------------------------------------------------------------------------
# 文档只读
# ---------------------------------------------------------------------------


def _documents_where(
    *, kb_id: int | None, status: str | None, include_chunked: bool,
    level: int | None, q: str | None,
) -> tuple[list[str], list[Any]]:
    where = ["1=1"]
    params: list[Any] = []
    if kb_id is not None:
        where.append("d.kb_id = ?")
        params.append(int(kb_id))
    if status:
        where.append("d.status = ?")
        params.append(str(status))
    elif include_chunked:
        # 分块预览：已分块待入库也要能看块（但文档管理默认仍不显示）。
        where.append("d.status IN ('chunked', 'published', 'offline')")
    else:
        # 文档管理默认只展示"完成解析+清洗+分块+向量"的文档；
        # 未完成的 pending/parsing/review/indexing/failed 只在「清洗解析」页处理。
        where.append("d.status IN ('published', 'offline')")
    if level is not None:
        where.append("d.level = ?")
        params.append(int(level))
    if q:
        where.append("d.title LIKE ?")
        params.append(f"%{q}%")
    return where, params


def _apply_perm_scope(
    where: list[str], params: list[Any], perm, *, admin_bypass: bool,
) -> bool:
    """SQL 先收窄：密级 + 可见知识库（超管跳过，否则跨部门运维看不到库）。

    返回 False 表示用户没有任何可见知识库（调用方直接给空结果）。
    """
    if admin_bypass:
        return True
    where.append("d.level <= ?")
    params.append(int(perm.clearance))
    if not perm.visible_kb_ids:
        return False
    placeholders = ",".join("?" for _ in perm.visible_kb_ids)
    where.append(f"d.kb_id IN ({placeholders})")
    params.extend(int(k) for k in perm.visible_kb_ids)
    return True


def _filter_visible_docs(
    runtime, perm, raw_rows: list[dict[str, Any]], *,
    allow_deleted: bool, admin_bypass: bool,
) -> list[dict[str, Any]]:
    """逐行 ACL（行级可见性）。"""
    import json as _json

    from ekbs.permission.acl import DocumentFacts

    doc_ids = [int(r["doc_id"]) for r in raw_rows]
    acl_map = runtime.gate.load_document_acl(doc_ids)
    visible: list[dict[str, Any]] = []
    for record in raw_rows:
        try:
            record["visible_dept_ids"] = _json.loads(record.get("visible_dept_ids") or "[]")
        except ValueError:
            record["visible_dept_ids"] = []
        facts = DocumentFacts.from_row(record)
        decision = runtime.gate.can_see_document(
            perm, facts, acl_map.get(int(record["doc_id"]), []),
            for_retrieval=False, allow_deleted=allow_deleted, admin_bypass=admin_bypass,
        )
        if decision.allowed:
            visible.append(record)
    return visible


def _count_visible_docs(
    runtime, conn, perm, where: list[str], params: list[Any], *,
    allow_deleted: bool, admin_bypass: bool,
) -> int:
    """若触顶仍可能漏计：再做一次全量精滤计数（试点文档量可接受）。"""
    from ekbs.permission.acl import DocumentFacts

    count_sql = (
        "SELECT d.doc_id, d.kb_id, d.title, d.level, d.status, d.visible_dept_ids, "
        "d.effective_date, d.expire_date, d.doc_version, "
        "(SELECT c.shard_id FROM ekbs_chunk c WHERE c.doc_id = d.doc_id LIMIT 1) AS shard_id "
        f"FROM ekbs_document d WHERE {' AND '.join(where)}"
    )
    all_rows = conn.execute(count_sql, tuple(params)).fetchall()
    all_ids = [int(r["doc_id"]) for r in all_rows]
    all_acl = runtime.gate.load_document_acl(all_ids)
    total = 0
    for row in all_rows:
        facts = DocumentFacts.from_row(row)
        if runtime.gate.can_see_document(
            perm, facts, all_acl.get(int(row["doc_id"]), []),
            for_retrieval=False, allow_deleted=allow_deleted, admin_bypass=admin_bypass,
        ):
            total += 1
    return total


@router.get("/docs/documents")
async def list_documents(
    request: Request,
    kb_id: int | None = Query(default=None),
    status: str | None = Query(default=None),
    include_chunked: bool = Query(default=False, description="分块预览：同时返回已分块待入库"),
    level: int | None = Query(default=None),
    q: str | None = Query(default=None, description="标题模糊匹配"),
    limit: int = Query(default=50),
    offset: int = Query(default=0),
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """文档列表（分页/过滤）—— 权限点 ``doc:document:read`` + **行级可见性**。"""
    headers = {
        "authorization": authorization,
        INTERNAL_TOKEN_HEADER.lower(): x_internal_token,
        INTERNAL_USER_HEADER.lower(): x_internal_user_id,
    }
    runtime = request.app.state.runtime
    user = _authenticate(request, headers)
    try:
        principal = runtime.authz.require(user.user_id, "doc:document:read")
    except PermissionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    perm = await runtime.perm_context(user.user_id)
    admin_bypass = SUPER_ADMIN in principal.roles
    if is_console_role(principal.roles) and not admin_bypass:
        perm = widen_for_console(perm, runtime.db)
    limit, offset = _page(limit, offset)
    allow_deleted = str(status or "") == "deleted"
    where, params = _documents_where(
        kb_id=kb_id, status=status, include_chunked=include_chunked, level=level, q=q
    )
    if not _apply_perm_scope(where, params, perm, admin_bypass=admin_bypass):
        return JSONResponse({"items": [], "total": 0, "limit": limit, "offset": offset})

    # 试点规模：先取足够候选再精滤分页（避免 total 与可见行不一致）
    fetch_cap = min(max(offset + limit * 5, 200), 2000)
    sql = (
        "SELECT d.doc_id, d.kb_id, k.code AS kb_code, k.name AS kb_name, d.title, d.level, d.status, "
        "d.visible_dept_ids, d.page_count, d.doc_version, d.effective_date, d.expire_date, "
        "d.updated_at, d.deleted_at, "
        "(SELECT c.shard_id FROM ekbs_chunk c WHERE c.doc_id = d.doc_id LIMIT 1) AS shard_id, "
        "(SELECT COUNT(*) FROM ekbs_chunk c WHERE c.doc_id = d.doc_id) AS chunk_count "
        "FROM ekbs_document d LEFT JOIN ekbs_kb k ON k.kb_id = d.kb_id "
        f"WHERE {' AND '.join(where)} ORDER BY d.doc_id DESC LIMIT ?"
    )
    with runtime.db.sync_connect() as conn:
        raw_rows = [dict(r) for r in conn.execute(sql, (*params, fetch_cap)).fetchall()]
        visible = _filter_visible_docs(
            runtime, perm, raw_rows, allow_deleted=allow_deleted, admin_bypass=admin_bypass
        )
        total = len(visible)
        from ekbs.personal import attach_personal_owners

        attach_personal_owners(conn, visible)
        if len(raw_rows) >= fetch_cap:
            total = _count_visible_docs(
                runtime, conn, perm, where, params,
                allow_deleted=allow_deleted, admin_bypass=admin_bypass,
            )
        page = visible[offset : offset + limit]
    return JSONResponse({"items": page, "total": total, "limit": limit, "offset": offset})


@router.get("/docs/documents/{doc_id}")
async def get_document(
    doc_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """文档详情（含分块统计与所属分片）；越权返回 404 防枚举。"""
    headers = {
        "authorization": authorization,
        INTERNAL_TOKEN_HEADER.lower(): x_internal_token,
        INTERNAL_USER_HEADER.lower(): x_internal_user_id,
    }
    runtime = request.app.state.runtime
    user = _authenticate(request, headers)
    try:
        principal = runtime.authz.require(user.user_id, "doc:document:read")
    except PermissionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    perm = await runtime.perm_context(user.user_id)
    admin_bypass = SUPER_ADMIN in principal.roles
    if is_console_role(principal.roles) and not admin_bypass:
        perm = widen_for_console(perm, runtime.db)
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT d.*, k.code AS kb_code, k.name AS kb_name, "
            "(SELECT c.shard_id FROM ekbs_chunk c WHERE c.doc_id = d.doc_id "
            " ORDER BY c.chunk_id LIMIT 1) AS shard_id "
            "FROM ekbs_document d "
            "LEFT JOIN ekbs_kb k ON k.kb_id = d.kb_id WHERE d.doc_id = ?",
            (doc_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="not_found")
        from ekbs.permission.acl import DocumentFacts

        facts = DocumentFacts.from_row(row)
        entries = runtime.gate.load_document_acl([doc_id]).get(doc_id, [])
        if not runtime.gate.can_see_document(
            perm, facts, entries, for_retrieval=False, allow_deleted=False,
            admin_bypass=admin_bypass,
        ):
            raise HTTPException(status_code=404, detail="not_found")
        chunks = {
            str(r["chunk_type"]): int(r["c"])
            for r in conn.execute(
                "SELECT chunk_type, COUNT(*) AS c FROM ekbs_chunk WHERE doc_id = ? "
                "GROUP BY chunk_type",
                (doc_id,),
            ).fetchall()
        }
        shards = [
            str(r["shard_id"])
            for r in conn.execute(
                "SELECT DISTINCT shard_id FROM ekbs_chunk WHERE doc_id = ?", (doc_id,)
            ).fetchall()
        ]
        citations = int(
            conn.execute(
                "SELECT COUNT(*) FROM ekbs_qa_citation WHERE doc_id = ?", (doc_id,)
            ).fetchone()[0]
        )
    import json as _json

    item = dict(row)
    with contextlib.suppress(ValueError):
        item["visible_dept_ids"] = _json.loads(item.get("visible_dept_ids") or "[]")
    from ekbs.personal import attach_personal_owners

    with runtime.db.sync_connect() as conn:
        attach_personal_owners(conn, [item])
    return JSONResponse(
        {"document": item, "chunks": chunks, "shards": shards, "citation_count": citations}
    )


def _source_under_allowed(runtime: Any, source: str) -> Path | None:
    """原件必须落在 ``raw`` / ``trash`` 内；越界返回 None（调用方统一 404）。"""
    from ekbs.ingest.path_sanitize import relative_under

    if not source or source.startswith("demo://"):
        return None
    try:
        resolved = Path(source).resolve()
    except OSError:
        return None
    paths = runtime.settings.data_paths()
    for name in ("raw", "trash"):
        root = Path(paths[name]).resolve()
        if relative_under(resolved, root) is None:
            continue
        if resolved.is_file():
            return resolved
    return None


async def _open_document_source(
    request: Request,
    doc_id: int,
    headers: dict[str, str | None],
) -> Path:
    """鉴权后返回原件路径，并记一条 view 审计。"""
    runtime = request.app.state.runtime
    user = _authenticate(request, headers)
    try:
        principal = runtime.authz.require(user.user_id, "doc:document:read")
    except PermissionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    perm = await runtime.perm_context(user.user_id)
    admin_bypass = SUPER_ADMIN in principal.roles
    if is_console_role(principal.roles) and not admin_bypass:
        perm = widen_for_console(perm, runtime.db)
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT * FROM ekbs_document WHERE doc_id = ?", (doc_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")

    from ekbs.permission.acl import DocumentFacts

    facts = DocumentFacts.from_row(row)
    entries = runtime.gate.load_document_acl([doc_id]).get(doc_id, [])
    if not runtime.gate.can_see_document(
        perm, facts, entries, for_retrieval=False, allow_deleted=True,
        admin_bypass=admin_bypass,
    ):
        runtime.audit.record_access(
            user_id=user.user_id, doc_id=doc_id, action="view",
            channel="admin_source", result="denied",
        )
        raise HTTPException(status_code=404, detail="not_found")

    candidate = _source_under_allowed(runtime, str(row["source_path"] or ""))
    if candidate is None:
        runtime.audit.record_access(
            user_id=user.user_id, doc_id=doc_id, action="view",
            channel="admin_source", result="denied",
        )
        raise HTTPException(status_code=404, detail="not_found")

    runtime.audit.record_access(
        user_id=user.user_id, doc_id=doc_id, action="view",
        channel="admin_source", result="ok",
    )
    return candidate


def _source_headers(
    authorization: str | None,
    x_internal_token: str | None,
    x_internal_user_id: str | None,
) -> dict[str, str | None]:
    return {
        "authorization": authorization,
        INTERNAL_TOKEN_HEADER.lower(): x_internal_token,
        INTERNAL_USER_HEADER.lower(): x_internal_user_id,
    }


@router.get("/docs/documents/{doc_id}/source")
async def get_document_source(
    doc_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> FileResponse:
    """文档原件回源（复核左栏的 PDF / 图片）。越权 / 无文件 / 路径越界一律 404。"""
    candidate = await _open_document_source(
        request, doc_id, _source_headers(authorization, x_internal_token, x_internal_user_id),
    )
    media_type, _ = mimetypes.guess_type(candidate.name)
    # inline：嵌入复核左栏，禁止 attachment 触发「另存为/新标签打开」弹层
    return FileResponse(
        candidate,
        media_type=media_type or "application/octet-stream",
        filename=candidate.name,
        content_disposition_type="inline",
    )


@router.get("/docs/documents/{doc_id}/source-preview")
async def get_document_source_preview(
    doc_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> HTMLResponse:
    """Word / Excel / PPT / 文本原件转成 HTML，嵌进复核左栏。PDF 请走 /source。"""
    candidate = await _open_document_source(
        request, doc_id, _source_headers(authorization, x_internal_token, x_internal_user_id),
    )
    try:
        page = render_office_preview(candidate)
    except OfficePreviewError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    return HTMLResponse(page)


# ---------------------------------------------------------------------------
# 告警阈值（只读展示，便于运维核对）
# ---------------------------------------------------------------------------


@router.get("/system/alert-rules")
async def alert_rules(
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """当前生效的告警阈值表（`04` §7.7 的唯一权威定义在代码里，这里只读展示）。"""
    _principal(
        request,
        {
            "authorization": authorization,
            INTERNAL_TOKEN_HEADER.lower(): x_internal_token,
            INTERNAL_USER_HEADER.lower(): x_internal_user_id,
        },
        "sys:health:read",
    )
    return JSONResponse({"items": [rule.as_dict() for rule in ALERT_RULES]})
