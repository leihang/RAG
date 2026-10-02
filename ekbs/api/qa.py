"""问答端点 —— `05` 的 API 清单里一期必需的部分。

路径前缀：``smoke.sh`` **同时**尝试 ``/api/v1/qa/chat`` 与 ``/v1/qa/chat``（`01` §14 与 `04` §2.5
的口径冲突），因此本应用把同一 router 挂两次；权威口径待定稿（见 ``docs/spec/30`` C-项）。

响应字段（``smoke.sh`` 断言 ``answer`` 与 ``citations[0].chunk_id``，字段名不可改）：

.. code-block:: json

    {"answer": "…", "citations": [{"chunk_id": 12, "doc_id": 3, "page_from": 2, …}],
     "refused": false, "refusal_reason": "", "model": "…", "trace_id": "…",
     "timings_ms": {"embed": 3.1, "recall": 8.0, "rerank": 120.0, "total": 900.0},
     "perm_fingerprint": "…"}

**权限红线**：无凭证 → 401；带凭证但无可见分片 → 200 且 ``refused=true``（不是 500，也不假装正常）。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from ekbs.api.auth import (
    INTERNAL_CHANNEL_HEADER,
    INTERNAL_DT_USER_HEADER,
    INTERNAL_TOKEN_HEADER,
    INTERNAL_USER_HEADER,
)
from ekbs.audit import AuditService, TurnRecord
from ekbs.errors import PermissionDenied
from ekbs.logging import get_logger, trace_id_var
from ekbs.retrieval.pipeline import REFUSE_NO_SHARD

router = APIRouter(tags=["qa"])
log = get_logger(component="api.qa")


class QaRequest(BaseModel):
    question: str = Field(min_length=1)
    stream: bool = False
    conversation_id: str | None = None
    use_cache: bool = True
    #: 作答模式：``prose``（默认，业务问答）| ``numeric``（取数：值/单位/页码 + 抄写校验）。
    #: 取数模式见 ekbs/retrieval/numeric.py 与 README §四 第 77 条；两种模式各自独立缓存。
    mode: str = Field(default="prose", pattern="^(prose|numeric)$")


class RetrieveRequest(BaseModel):
    query: str = Field(min_length=1)


def _authenticate(request: Request, headers: dict[str, str | None]) -> Any:
    runtime = request.app.state.runtime
    try:
        return runtime.authenticator.authenticate(
            authorization=headers.get("authorization"),
            internal_token=headers.get(INTERNAL_TOKEN_HEADER.lower()),
            internal_user_id=headers.get(INTERNAL_USER_HEADER.lower()),
            internal_dt_userid=headers.get(INTERNAL_DT_USER_HEADER.lower()),
            internal_channel=headers.get(INTERNAL_CHANNEL_HEADER.lower()),
        )
    except PermissionDenied as exc:
        # 红线：未授权一律 401，且不泄露「该文档是否存在」
        raise HTTPException(status_code=401, detail="unauthorized") from exc


def _enforce_rate_limit(runtime: Any, *, user_id: int, conversation_id: str | None) -> None:
    """限流（04 §5.2）：超限返回 429 + Retry-After，而不是让请求打到云端烧钱。"""
    decision = runtime.rate_limiter.check(user_id=user_id, conversation_id=conversation_id)
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail=f"rate_limited:{decision.bucket}",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )


def _record_qa_metrics(answer: Any, perm: Any) -> None:
    """问答埋点（`04` §7.7 的 MESSAGE_LATENCY / FIRST_TOKEN + 应用自身指标）。"""
    from ekbs.observability.metrics import (
        FIRST_TOKEN,
        MESSAGE_LATENCY,
        MESSAGE_PROCESSED,
        QA_TOTAL,
        RETRIEVAL_LATENCY,
        observe,
        inc,
    )

    result = "refused" if answer.refused else "ok"
    inc(QA_TOTAL, 1, result=result)
    inc(MESSAGE_PROCESSED, 1, status=result)
    if answer.refused:
        inc("ekbs_dingtalk_message_failed_total", 1, reason=answer.refusal_reason or "refused")
    latency_s = max(0.0, float(answer.total_ms) / 1000.0)
    if latency_s:
        observe(MESSAGE_LATENCY, latency_s)
    if answer.ttft_ms:
        observe(FIRST_TOKEN, max(0.0, float(answer.ttft_ms) / 1000.0))
    retrieve_ms = float(answer.retrieval.timings_ms.get("total", 0.0) or 0.0)
    if retrieve_ms:
        observe(RETRIEVAL_LATENCY, retrieve_ms / 1000.0)


def _guard_input(runtime: Any, perm: Any, question: str) -> tuple[str, bool]:
    """内容安全输入闸门（04 §6.4）。

    返回 ``(可用的问题文本, 是否允许生成)``：

    * 命中敏感词 → **400 拦截** + 写 ``ekbs_security_event``；
    * 疑似提示词注入 → 放行但**禁用生成**（纯检索答复）；
    * 命中 PII → **打码后继续**（打码后的文本才进检索、才算审计留痕）。
    """
    decision = runtime.content_guard.check_input(question)
    if decision.blocked:
        runtime.gate.record_security_event(
            "content_blocked", user_id=perm.user_id, severity="medium",
            detail={"hits": decision.hits[:5], "reason": decision.reason},
        )
        raise HTTPException(status_code=400, detail=decision.reason)
    if decision.action == "retrieve_only":
        runtime.gate.record_security_event(
            "injection_suspected", user_id=perm.user_id, severity="high",
            detail={"patterns": decision.hits[:5], "action": "generation_disabled"},
        )
        return question, False
    return (decision.text or question), True


def _check_question_length(runtime: Any, question: str) -> None:
    limit = int(runtime.settings.limits.get("max_question_chars", 2000) or 2000)
    if len(question) > limit:
        raise HTTPException(status_code=413, detail=f"question_too_long（上限 {limit} 字）")


def _citation_payload(citations: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "cite_index": c.cite_index,
            "chunk_id": c.chunk_id,
            "doc_id": c.doc_id,
            "doc_title": c.doc_title,
            "doc_version": c.doc_version,
            "heading_path": c.heading_path,
            "page_from": c.page_from,
            "page_to": c.page_to,
            "rerank_score": c.rerank_score,
            "acl_result": c.acl_result,
            "page_check": c.page_check,
            "figure_id": c.figure_id,
            "image_path": c.image_path,
        }
        for c in citations
    ]


@router.post("/qa/chat")
async def qa_chat(
    payload: QaRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
    x_internal_dt_userid: str | None = Header(default=None, alias=INTERNAL_DT_USER_HEADER),
    x_internal_channel: str | None = Header(default=None, alias=INTERNAL_CHANNEL_HEADER),
) -> JSONResponse:
    runtime = request.app.state.runtime
    user = _authenticate(
        request,
        {
            "authorization": authorization,
            INTERNAL_TOKEN_HEADER.lower(): x_internal_token,
            INTERNAL_USER_HEADER.lower(): x_internal_user_id,
            INTERNAL_DT_USER_HEADER.lower(): x_internal_dt_userid,
            INTERNAL_CHANNEL_HEADER.lower(): x_internal_channel,
        },
    )
    _check_question_length(runtime, payload.question)
    _enforce_rate_limit(runtime, user_id=user.user_id, conversation_id=payload.conversation_id)

    trace_id = str(uuid.uuid4())
    trace_id_var.set(trace_id)
    started = time.perf_counter()
    perm = await runtime.perm_context(user.user_id)
    # 内容安全闸门：拦截 / 打码 / 降级（先过闸，再进检索与云端）
    safe_question, allow_generation = _guard_input(runtime, perm, payload.question)
    answer = await runtime.pipeline.answer(
        safe_question, perm,
        conversation_id=payload.conversation_id,
        use_cache=payload.use_cache,
        allow_generation=allow_generation,
        mode=payload.mode,
    )

    # 审计（append-only；引用只存 hash 与页码）
    audit: AuditService = runtime.audit
    # 同一 (user, conversation_id) 复用会话，turn 在会话内递增（否则每轮都新建、turn 恒为 1）
    session_id, turn = audit.resolve_session(
        perm,
        channel=user.channel,
        conversation_id=payload.conversation_id,
        ttl_seconds=runtime.sessions.ttl_seconds,
    )
    _record_qa_metrics(answer, perm)

    audit.record_turn(
        TurnRecord(
            session_id=session_id, user_id=perm.user_id, dt_userid=perm.dingtalk_userid,
            channel=user.channel, conversation_id=payload.conversation_id,
            perm_fingerprint=perm.fingerprint, clearance=perm.clearance, turn=turn,
        ),
        question=safe_question,
        answer=answer,
        trace_id=trace_id,
    )

    body = {
        "answer": answer.answer,
        "question": safe_question,
        "rewritten_query": answer.rewritten_query,
        "history_used": answer.history_used,
        "citations": _citation_payload(answer.citations),
        "refused": answer.refused,
        "refusal_reason": answer.refusal_reason,
        "model": answer.model,
        "session_id": session_id,
        "turn": turn,
        "trace_id": trace_id,
        "timings_ms": {**answer.retrieval.timings_ms, "total_request": round((time.perf_counter() - started) * 1000, 2)},
        "candidate_count": answer.retrieval.candidate_count,
        "rerank_top_score": answer.retrieval.top_score,
        "c5_dropped": answer.retrieval.c5_dropped,
        "degraded": [*answer.retrieval.degraded, *answer.degraded],
        # 取数模式的结构化结果（prose 模式下为 None）
        "numeric": answer.numeric,
        "perm_fingerprint": perm.fingerprint,
        "visible_shard_count": len(perm.visible_shard_ids),
        "warnings": list(perm.warnings),
    }
    return JSONResponse(body)


@router.post("/qa/retrieve")
async def qa_retrieve(
    payload: RetrieveRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
    x_internal_channel: str | None = Header(default=None, alias=INTERNAL_CHANNEL_HEADER),
) -> JSONResponse:
    """只检索不生成（评测与调参用；不写审计答案，仅返回候选与分数）。"""
    runtime = request.app.state.runtime
    user = _authenticate(
        request,
        {
            "authorization": authorization,
            INTERNAL_TOKEN_HEADER.lower(): x_internal_token,
            INTERNAL_USER_HEADER.lower(): x_internal_user_id,
            INTERNAL_CHANNEL_HEADER.lower(): x_internal_channel,
        },
    )
    perm = await runtime.perm_context(user.user_id)
    result = await runtime.pipeline.retrieve(payload.query, perm)
    return JSONResponse(
        {
            "query": payload.query,
            "refused": result.refused,
            "refusal_reason": result.refusal_reason,
            "candidate_count": result.candidate_count,
            "c5_dropped": result.c5_dropped,
            "top_score": result.top_score,
            "degraded": result.degraded,
            "timings_ms": result.timings_ms,
            "perm_fingerprint": perm.fingerprint,
            "chunks": [
                {
                    "chunk_id": c.chunk_id,
                    "doc_id": c.doc_id,
                    "shard_id": c.shard_id,
                    "doc_title": c.doc_title,
                    "heading_path": c.heading_path,
                    "page_from": c.page_from,
                    "page_to": c.page_to,
                    "chunk_type": c.chunk_type,
                    "dense_rank": c.dense_rank,
                    "sparse_rank": c.sparse_rank,
                    "rrf_score": round(c.rrf_score, 6),
                    "rerank_score": c.rerank_score,
                }
                for c in result.chunks
            ],
        }
    )


@router.post("/qa/chat/stream")
async def qa_chat_stream(
    payload: QaRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
    x_internal_channel: str | None = Header(default=None, alias=INTERNAL_CHANNEL_HEADER),
) -> StreamingResponse:
    """SSE 流式回答（钉钉卡片流式与 H5 共用）。

    当前实现先完成检索与生成、再按块推送增量；接入云端流式后只需替换
    :meth:`ekbs.retrieval.pipeline.RetrievalPipeline.generate` 的产出方式，
    审计与引用校验逻辑不变。
    """
    runtime = request.app.state.runtime
    user = _authenticate(
        request,
        {
            "authorization": authorization,
            INTERNAL_TOKEN_HEADER.lower(): x_internal_token,
            INTERNAL_USER_HEADER.lower(): x_internal_user_id,
            INTERNAL_CHANNEL_HEADER.lower(): x_internal_channel,
        },
    )
    _check_question_length(runtime, payload.question)
    perm = await runtime.perm_context(user.user_id)
    safe_question, allow_generation = _guard_input(runtime, perm, payload.question)
    answer = await runtime.pipeline.answer(
        safe_question, perm, conversation_id=payload.conversation_id, use_cache=False,
        allow_generation=allow_generation,
    )

    async def event_stream() -> Any:
        yield f"event: meta\ndata: {json.dumps({'citations': _citation_payload(answer.citations), 'refused': answer.refused, 'refusal_reason': answer.refusal_reason, 'perm_fingerprint': perm.fingerprint}, ensure_ascii=False)}\n\n"
        text = answer.answer or ""
        step = 24
        for index in range(0, len(text), step):
            chunk = text[index : index + step]
            yield f"event: delta\ndata: {json.dumps({'text': chunk}, ensure_ascii=False)}\n\n"
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/qa/session/clear")
async def clear_session(
    request: Request,
    conversation_id: str = Query(min_length=1),
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> dict[str, Any]:
    """清空**当前用户**在该会话里的多轮上下文（不接受指定别的用户，防越权清别人的上下文）。"""
    runtime = request.app.state.runtime
    user = _authenticate(
        request,
        {
            "authorization": authorization,
            INTERNAL_TOKEN_HEADER.lower(): x_internal_token,
            INTERNAL_USER_HEADER.lower(): x_internal_user_id,
        },
    )
    perm = await runtime.perm_context(user.user_id)
    removed = runtime.sessions.clear(conversation_id, perm.dingtalk_userid)
    return {"removed": removed, "conversation_id": conversation_id}


@router.get("/qa/health")
async def qa_health() -> dict[str, Any]:
    """问答子系统的轻量自述（便于联调；不是探针）。"""
    return {"status": "ok", "refuse_on_no_shard": REFUSE_NO_SHARD}
