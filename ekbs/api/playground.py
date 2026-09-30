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

⚠️ Schema 里**没有调试历史表**（29 张表已冻结），因此 ``history`` 用 ``ekbs_cache``
做环形缓冲（TTL 7 天、最多 50 条）—— 它是**临时排障数据**，不是审计数据。
参数模板（``/rag/param-presets``）与反馈（``/debug/feedback``）同样缺表，
本实现**不做**并在 README 的待确认里列出（新增表需先改 `01` §5 与 `00` §9.1）。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ekbs.api.auth import INTERNAL_TOKEN_HEADER, INTERNAL_USER_HEADER
from ekbs.api.qa import _authenticate
from ekbs.errors import PermissionDenied
from ekbs.logging import get_logger
from ekbs.permission.router import ShardRouter
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
    }


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
        "chunks": [_chunk_view(chunk) for chunk in result.chunks],
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
    """完整链路（检索 + 生成），返回各阶段结构化结果。"""
    user, _ = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "rag:playground:run"
    )
    runtime = request.app.state.runtime
    perm = await runtime.perm_context(user.user_id)
    result = await runtime.pipeline.retrieve(
        payload.query, perm, overrides=payload.overrides(), collect_debug=True
    )
    answer = await runtime.pipeline.generate(payload.query, result, perm)
    return JSONResponse(
        {
            "query": payload.query,
            "answer": answer.answer,
            "refused": answer.refused,
            "refusal_reason": answer.refusal_reason,
            "model": answer.model,
            "prompt_tokens": answer.prompt_tokens,
            "completion_tokens": answer.completion_tokens,
            "citations": [
                {
                    "cite_index": c.cite_index, "chunk_id": c.chunk_id, "doc_title": c.doc_title,
                    "page_from": c.page_from, "acl_result": c.acl_result, "page_check": c.page_check,
                    # ⚠️ 图片字段不能省：图片块被召回时，前端要靠 chunk_id 去
                    # `GET /assets/figure/{chunk_id}` 取原图显示。原先这里漏了
                    # `figure_id` / `image_path`，而 `/qa/chat` 是带的 —— 同一个引用
                    # 两个接口两套字段，调试台上永远看不到图（用户实测反馈）。
                    "figure_id": c.figure_id,
                    "image_path": c.image_path,
                }
                for c in answer.citations
            ],
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

    result = await runtime.pipeline.retrieve(
        payload.query, target, overrides=payload.overrides(), collect_debug=True
    )
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
            "refused": result.refused,
            "refusal_reason": result.refusal_reason,
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
