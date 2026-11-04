"""检索调试台（`05` §8.2 的 ``/api/v1/rag/debug/*``）—— 一期 SLO 的验收工具。

存在的理由：`00` §4 要求 **Recall@10 ≥0.85**、检索 P95 ≤2s，`00` §11 的 M3 还要求
「rerank 有/无的 Recall@5 差异」。这些都必须**用真实查询逐环节看清召回**才能调，
所以调试台不是彩蛋，是验收手段。

| 端点 | 权限点 |
|---|---|
| ``POST /api/v1/rag/debug/rewrite`` | ``rag:playground:run`` |
| ``POST /api/v1/rag/debug/retrieve`` | ``rag:playground:run`` |
| ``POST /api/v1/rag/debug/query`` | ``rag:playground:run`` |
| ``POST /api/v1/rag/debug/simulate`` | ``rag:playground:simulate``（**仅 super_admin**） |
| ``GET /api/v1/rag/debug/history`` | ``rag:playground:run`` |

两条硬约束：

1. **调参只作用于本次请求**（``RetrievalOverrides``），绝不改全局配置 ——
   配置是进程级单例，per-request 改写会串到并发请求上；
2. **``simulate`` 是跨权限操作**（以别人身份检索）：必须 ``rag:playground:simulate``、
   **落安全事件与审计**、并在响应里回显被模拟者与操作者（`05` 称之为"高危"）。

⚠️ Schema 里**没有调试历史表**（31 张表已冻结），因此 ``history`` 用 ``ekbs_cache``
做环形缓冲（TTL 7 天、最多 50 条）—— 它是**临时排障数据**。

``POST /rag/debug/query``（完整作答）会**额外**写入 ``ekbs_qa_*`` 审计表
（``channel='playground'``），与正式 ``/qa/chat`` 同口径，便于持续改进与分析；
仅改写/仅检索仍只进环形缓冲，避免刷爆审计。
参数模板（``/rag/param-presets``）与反馈（``/debug/feedback``）同样缺表，
本实现**不做**并在 README 的待确认里列出（新增表需先改 `01` §5 与 `00` §9.1）。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ekbs.api.auth import INTERNAL_TOKEN_HEADER, INTERNAL_USER_HEADER
from ekbs.api.qa import _authenticate
from ekbs.audit import AuditService, TurnRecord
from ekbs.errors import PermissionDenied
from ekbs.logging import get_logger, trace_id_var
from ekbs.permission.console import is_console_role, widen_for_console
from ekbs.retrieval.pipeline import RetrievalOverrides

router = APIRouter(tags=["playground"])
log = get_logger(component="api.playground")

HISTORY_KEY = "ekbs:rag:debug_history"
HISTORY_LIMIT = 50
HISTORY_TTL = 7 * 86400


def _principal(request: Request, headers: dict[str, str | None], code: str) -> tuple[Any, Any]:
    runtime = request.app.state.runtime
    user = _authenticate(request, headers)
    try:
        principal = runtime.authz.require(user.user_id, code)
    except PermissionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return user, principal


def _h(authorization: str | None, token: str | None, user_id: str | None) -> dict[str, str | None]:
    return {
        "authorization": authorization,
        INTERNAL_TOKEN_HEADER.lower(): token,
        INTERNAL_USER_HEADER.lower(): user_id,
    }


class DebugRequest(BaseModel):
    query: str = Field(min_length=1)
    conversation_id: str | None = None
    #: 调参覆盖（都不给就用配置值）
    dense_top_k: int | None = Field(default=None, ge=1, le=500)
    sparse_top_k: int | None = Field(default=None, ge=1, le=500)
    rrf_k: int | None = Field(default=None, ge=1, le=1000)
    rerank_enabled: bool | None = None
    rerank_top_n: int | None = Field(default=None, ge=1, le=100)
    rerank_keep: int | None = Field(default=None, ge=1, le=100)
    rerank_min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    parent_expand: bool | None = None
    #: 作答模式：prose（默认）| numeric（取数模式，见 ekbs/retrieval/numeric.py）
    #: 管理台手动切换：仅 prose / numeric（不用 auto，避免调试时意图路由干扰对照）
    mode: str = Field(default="prose", pattern="^(prose|numeric)$")

    def overrides(self) -> RetrievalOverrides:
        return RetrievalOverrides(
            dense_top_k=self.dense_top_k, sparse_top_k=self.sparse_top_k, rrf_k=self.rrf_k,
            rerank_enabled=self.rerank_enabled, rerank_top_n=self.rerank_top_n,
            rerank_keep=self.rerank_keep, rerank_min_score=self.rerank_min_score,
            parent_expand=self.parent_expand,
        )


class SimulateRequest(DebugRequest):
    user_id: int = Field(gt=0, description="被模拟用户的 user_id")
    reason: str = Field(min_length=1, max_length=200, description="模拟原因（写审计）")


def _chunk_view(chunk: Any) -> dict[str, Any]:
    return {
        "chunk_id": chunk.chunk_id,
        "doc_id": chunk.doc_id,
        "shard_id": chunk.shard_id,
        "doc_title": chunk.doc_title,
        "chunk_type": chunk.chunk_type,
        "heading_path": chunk.heading_path,
        "page_from": chunk.page_from,
        "page_to": chunk.page_to,
        "dense_rank": chunk.dense_rank,
        "sparse_rank": chunk.sparse_rank,
        "rrf_score": round(chunk.rrf_score, 8),
        "rerank_score": chunk.rerank_score,
        "figure_id": getattr(chunk, "figure_id", None),
        "image_path": getattr(chunk, "image_path", None),
        # 父块/整表扩展来的上下文块：记录它由哪个精排候选扩展而来（None = 自己就是候选）
        "expanded_from": getattr(chunk, "expanded_from", None),
    }


def _rerank_rank_map(result: Any) -> dict[int, int]:
    """``chunk_id → 1-based 精排名次``。

    ``stage_debug['rerank']['scores']`` 已按 rerank 降序，序号即名次；精排被关闭/
    降级时该字段为空，此时名次一律 ``None``（不伪造）。
    """
    scores = ((getattr(result, "stage_debug", None) or {}).get("rerank") or {}).get("scores") or []
    ranks: dict[int, int] = {}
    for index, item in enumerate(scores, start=1):
        chunk_id = item.get("chunk_id") if isinstance(item, dict) else None
        if chunk_id is not None:
            ranks[int(chunk_id)] = index
    return ranks


def _chunk_views_with_source_rank(result: Any) -> list[dict[str, Any]]:
    """最终上下文块视图 + ``source_rerank_rank``（扩展块看来源块的名次）。"""
    ranks = _rerank_rank_map(result)
    views: list[dict[str, Any]] = []
    for chunk in result.chunks:
        view = _chunk_view(chunk)
        source_id = (
            chunk.expanded_from if getattr(chunk, "expanded_from", None) is not None
            else chunk.chunk_id
        )
        view["source_rerank_rank"] = ranks.get(int(source_id))
        views.append(view)
    return views


def _citation_views(runtime: Any, citations: list[Any]) -> list[dict[str, Any]]:
    """调试台引用：补 doc_id / 多模态索引标识，便于直达看块。"""
    figure_ids = [
        int(c.chunk_id)
        for c in citations
        if getattr(c, "figure_id", None) or getattr(c, "image_path", None)
    ]
    indexed: set[int] = set()
    figure_index = getattr(runtime, "figure_index", None)
    if figure_ids and figure_index is not None:
        try:
            indexed = figure_index.contains(figure_ids)
        except Exception as exc:  # noqa: BLE001
            log.warning("figure_mm_contains_failed", error=str(exc)[:160])
    views: list[dict[str, Any]] = []
    for c in citations:
        is_figure = bool(getattr(c, "figure_id", None) or getattr(c, "image_path", None))
        views.append(
            {
                "cite_index": c.cite_index,
                "chunk_id": c.chunk_id,
                "doc_id": c.doc_id,
                "doc_title": c.doc_title,
                "page_from": c.page_from,
                "heading_path": getattr(c, "heading_path", None),
                "acl_result": c.acl_result,
                "page_check": c.page_check,
                # ⚠️ 图片字段不能省：前端靠 chunk_id 取 `GET /assets/figure/{chunk_id}`
                "figure_id": c.figure_id,
                "image_path": c.image_path,
                "figure_mm_indexed": (int(c.chunk_id) in indexed) if is_figure else False,
                # chunk_id = 上下文块；source_chunk_id = 精排来源块（扩展过才有值）
                "source_chunk_id": getattr(c, "source_chunk_id", None),
            }
        )
    return views


async def _record_history(runtime: Any, entry: dict[str, Any]) -> None:
    """调试历史（`ekbs_cache` 环形缓冲；Schema 无专用表，见模块 docstring）。"""
    history = await runtime.cache.get(HISTORY_KEY)
    items = history if isinstance(history, list) else []
    items.insert(0, entry)
    await runtime.cache.set(HISTORY_KEY, items[:HISTORY_LIMIT], ttl=HISTORY_TTL)


@router.post("/rag/debug/rewrite")
async def debug_rewrite(
    payload: DebugRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """只跑查询改写（多轮指代消解），看检索到底用了什么查询。"""
    user, _ = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "rag:playground:run"
    )
    runtime = request.app.state.runtime
    history: list[dict[str, str]] = []
    if payload.conversation_id:
        history = runtime.sessions.load(payload.conversation_id, user.dingtalk_userid)
    rewrite = await runtime.pipeline._rewrite(payload.query, history)  # noqa: SLF001
    return JSONResponse(
        {
            "original": rewrite.original,
            "rewritten": rewrite.rewritten,
            "strategy": rewrite.strategy,
            "used_history": rewrite.used_history,
            "history_turns": len(history),
        }
    )


@router.post("/rag/debug/retrieve")
async def debug_retrieve(
    payload: DebugRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """逐环节看召回：分片路由 → 稠密/稀疏召回 → RRF → rerank → C5。"""
    user, _ = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "rag:playground:run"
    )
    runtime = request.app.state.runtime
    perm = await runtime.perm_context(user.user_id)
    if is_console_role(runtime.authz.load(user.user_id).roles):
        perm = widen_for_console(perm, runtime.db)
    result = await runtime.pipeline.retrieve(
        payload.query, perm, overrides=payload.overrides(), collect_debug=True
    )
    body = {
        "query": payload.query,
        "operator": user.user_id,
        "perm_fingerprint": perm.fingerprint,
        "visible_shards": list(perm.visible_shard_ids),
        "clearance": perm.clearance,
        "refused": result.refused,
        "refusal_reason": result.refusal_reason,
        "candidate_count": result.candidate_count,
        "c5_dropped": result.c5_dropped,
        "top_score": result.top_score,
        "degraded": result.degraded,
        "timings_ms": result.timings_ms,
        "stages": result.stage_debug,
        "chunks": _chunk_views_with_source_rank(result),
    }
    # 上下文扩展（父块/整表）**在精排之后**发生：精排只给检索单元打分，父块/表容器
    # 本来就不在候选里。把「上下文块 ← 精排来源 + 精排名次」显式暴露，调试台/审计
    # 才不会把从没参与 rerank 的父块 id 误当成召回来源（doc 205 的 35274 ← 35277）。
    ranks = _rerank_rank_map(result)
    params = (result.stage_debug.get("params") or {}) if result.stage_debug else {}
    expanded_entries: list[dict[str, Any]] = []
    for chunk in result.chunks:
        source_id = getattr(chunk, "expanded_from", None)
        if source_id is None:
            continue
        expanded_entries.append(
            {
                "context_chunk_id": chunk.chunk_id,
                "source_chunk_id": source_id,
                "chunk_type": chunk.chunk_type,
                "heading_path": chunk.heading_path,
                "rerank_score": chunk.rerank_score,
                "rerank_rank": ranks.get(int(source_id)),
            }
        )
    result.stage_debug["expand"] = {
        "enabled": bool(params.get("parent_expand")),
        "deduped": result.deduped_chunks,
        "expanded": expanded_entries,
    }
    await _record_history(
        runtime,
        {
            "at": int(time.time()),
            "kind": "retrieve",
            "operator": user.user_id,
            "query": payload.query,
            "chunks": len(result.chunks),
            "refused": result.refused,
        },
    )
    return JSONResponse(body)


@router.post("/rag/debug/query")
async def debug_query(
    payload: DebugRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """完整链路（改写 → 检索 → 生成），返回各阶段结构化结果。

    传入 ``conversation_id`` 时走正式多轮：读/写 ``ekbs_session_ctx``、查询改写
    （历史只用于改写，不进生成 prompt）。不传则仍是单轮（与改前行为兼容）。
    """
    user, _ = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "rag:playground:run"
    )
    runtime = request.app.state.runtime
    perm = await runtime.perm_context(user.user_id)
    if is_console_role(runtime.authz.load(user.user_id).roles):
        perm = widen_for_console(perm, runtime.db)
    # 调试台必须看见 stages：关答案缓存 + collect_debug；调参仍只作用于本次
    answer = await runtime.pipeline.answer(
        payload.query,
        perm,
        conversation_id=payload.conversation_id,
        use_cache=False,
        overrides=payload.overrides(),
        collect_debug=True,
        mode=payload.mode,
    )
    result = answer.retrieval
    # 落正式审计（channel=playground），管理台「问答日志」可筛到；此前只进 cache 环形缓冲会丢
    audit: AuditService = runtime.audit
    session_id, turn = audit.resolve_session(
        perm,
        channel="playground",
        conversation_id=payload.conversation_id,
        ttl_seconds=runtime.sessions.ttl_seconds,
    )
    audit.record_turn(
        TurnRecord(
            session_id=session_id,
            user_id=perm.user_id,
            dt_userid=perm.dingtalk_userid,
            channel="playground",
            conversation_id=payload.conversation_id,
            perm_fingerprint=perm.fingerprint,
            clearance=perm.clearance,
            turn=turn,
        ),
        question=payload.query,
        answer=answer,
        trace_id=trace_id_var.get() or "",
    )
    await _record_history(
        runtime,
        {
            "at": int(time.time()),
            "kind": "query",
            "operator": user.user_id,
            "query": payload.query,
            "rewritten": answer.rewritten_query,
            "history_used": answer.history_used,
            "conversation_id": payload.conversation_id,
            "chunks": len(result.chunks),
            "refused": answer.refused,
            "session_id": session_id,
        },
    )
    answer_text = answer.answer
    suggested_questions: list[str] = []
    if answer.refused:
        from ekbs.discovery import format_refusal_hints, suggested_questions as _suggest

        hints = _suggest(runtime, perm, limit=3, query=payload.query, for_refusal=True)
        suggested_questions = [s["question"] for s in hints]
        footer = format_refusal_hints(hints)
        if footer and "你可以试试问" not in (answer_text or ""):
            answer_text = (answer_text or "未找到可靠依据。").rstrip() + footer

    return JSONResponse(
        {
            "query": payload.query,
            "rewritten_query": answer.rewritten_query,
            "history_used": answer.history_used,
            "conversation_id": payload.conversation_id,
            "session_id": session_id,
            "turn": turn,
            "answer": answer_text,
            "refused": answer.refused,
            "refusal_reason": answer.refusal_reason,
            "suggested_questions": suggested_questions,
            "model": answer.model,
            "prompt_tokens": answer.prompt_tokens,
            "completion_tokens": answer.completion_tokens,
            # 取数模式（mode=numeric）的结构化结果：值/单位/页码/原文片段/是否通过抄写校验
            "numeric": answer.numeric,
            "mode": payload.mode,
            "mode_used": answer.mode_used,
            "intent": answer.intent,
            "citations": _citation_views(runtime, answer.citations),
            "figure_attachments": list(answer.figure_attachments or []),
            "faithfulness": answer.faithfulness,
            "stages": result.stage_debug,
            "timings_ms": {**result.timings_ms, "generate_ms": answer.ttft_ms},
        }
    )


@router.post("/rag/debug/simulate")
async def debug_simulate(
    payload: SimulateRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """**以指定用户身份检索**（`05` 标注为高危）。

    安全处理三件事，缺一不可：

    1. 权限点是 ``rag:playground:simulate``（**仅 super_admin**）；
    2. 写 ``ekbs_security_event``（high）与 ``ekbs_perm_change_log``，让"谁模拟了谁"可回溯；
    3. 响应里同时回显 ``operator`` 与 ``simulated_user``，结果里带被模拟者的可见分片 ——
       这样排障时能直接看出"他为什么看不到/看得到"。
    """
    user, _ = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id),
        "rag:playground:simulate",
    )
    runtime = request.app.state.runtime
    try:
        target = await runtime.perm_context(payload.user_id)
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail=f"目标用户不可用：{exc}") from exc

    runtime.gate.record_security_event(
        "permission_simulated",
        user_id=user.user_id,
        severity="high",
        detail={
            "operator": user.user_id,
            "simulated_user": payload.user_id,
            "query": payload.query[:200],
            "reason": payload.reason,
        },
    )
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_perm_change_log(operator_id, object_type, object_id, change_type, "
            "field, old_value, new_value, reason) "
            "VALUES (?, 'user', ?, 'simulate', 'retrieval', '', ?, ?)",
            (user.user_id, str(payload.user_id), payload.query[:100], payload.reason),
        )
        conn.commit()

    # 以被模拟用户的身份走**完整问答**（检索 + 生成），不只是检索：
    # 管理台要验证的是"他问这句会得到什么答复"，只回 chunks 验证不了拒答/引用/忠实度。
    answer = await runtime.pipeline.answer(
        payload.query,
        target,
        use_cache=False,
        overrides=payload.overrides(),
        collect_debug=True,
        mode=payload.mode,
    )
    result = answer.retrieval
    return JSONResponse(
        {
            "operator": user.user_id,
            "simulated_user": payload.user_id,
            "reason": payload.reason,
            "simulated_perm": {
                "clearance": target.clearance,
                "dept_chain": list(target.dept_chain),
                "visible_shards": list(target.visible_shard_ids),
                "fingerprint": target.fingerprint,
            },
            "query": payload.query,
            # 与 /rag/debug/query **同形**：前端复用同一套答案/引用/阶段/忠实度渲染
            "rewritten_query": answer.rewritten_query,
            "history_used": answer.history_used,
            "answer": answer.answer,
            "refused": answer.refused,
            "refusal_reason": answer.refusal_reason,
            "model": answer.model,
            "citations": _citation_views(runtime, answer.citations),
            "figure_attachments": list(answer.figure_attachments or []),
            "degraded": [*result.degraded, *answer.degraded],
            "mode": payload.mode,
            "mode_used": answer.mode_used,
            "intent": answer.intent,
            "numeric": answer.numeric,
            "faithfulness": answer.faithfulness,
            "stages": result.stage_debug,
            "timings_ms": {**result.timings_ms, "generate_ms": answer.ttft_ms},
            "chunks": [_chunk_view(chunk) for chunk in result.chunks],
        }
    )


@router.get("/rag/debug/history")
async def debug_history(
    request: Request,
    limit: int = Query(default=20, ge=1, le=50),
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """调试历史（临时排障数据，``ekbs_cache`` 里 TTL 7 天；Schema 无专用表）。"""
    _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "rag:playground:run"
    )
    runtime = request.app.state.runtime
    items = await runtime.cache.get(HISTORY_KEY)
    items = items if isinstance(items, list) else []
    return JSONResponse(
        {
            "items": items[:limit],
            "count": len(items),
            "retention": "ekbs_cache TTL 7 天 / 最多 50 条（Schema 无调试历史表）",
        }
    )


@router.get("/rag/debug/shards")
async def debug_shards(
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """当前用户的分片路由结果与逐条原因（排障「他为什么命不中」的第一步）。"""
    user, _ = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "rag:playground:run"
    )
    runtime = request.app.state.runtime
    routing = await __import__("asyncio").to_thread(runtime.router.route, user.user_id)
    return JSONResponse(
        {
            "user_id": user.user_id,
            "clearance": routing.clearance,
            "dept_chain": list(routing.dept_chain),
            "visible_shards": list(routing.visible_shard_ids),
            "acl_allow_shards": sorted(routing.acl_allow_shards),
            "acl_deny_shards": sorted(routing.acl_deny_shards),
            "matched_dept_codes": sorted(routing.matched_dept_codes),
            "registered_shards": [
                {
                    "shard_id": row.shard_id, "dept_code": row.dept_code, "level": row.level,
                    "status": row.status,
                }
                for row in runtime.shards.registry()
            ],
            "warnings": list(routing.warnings),
        }
    )
