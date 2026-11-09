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

import asyncio
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
    #: 作答模式：``auto``（默认，按意图路由）| ``prose`` | ``numeric``。
    #: 取数见 ekbs/retrieval/numeric.py；``qa.auto_numeric`` 控制 auto 是否进取数。
    mode: str = Field(default="auto", pattern="^(auto|prose|numeric)$")
    #: 长会话入口（钉钉）重复提问时，忽略历史里该问题的旧回答，避免旧答案污染
    fresh_on_repeat: bool = False
    #: 钉钉会话类型：``1``=单聊（默认），``2``=群聊。群聊只允许公开文档：
    #: 回复会公开到群，不能把提问者的个人库/更高权限内容带进群。
    conversation_type: str | None = Field(default=None, pattern="^[12]$")


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
    from ekbs.discovery import display_doc_title

    return [
        {
            "cite_index": c.cite_index,
            "chunk_id": c.chunk_id,
            "doc_id": c.doc_id,
            "doc_title": c.doc_title,
            "display_title": display_doc_title(c.doc_title),
            "doc_version": c.doc_version,
            "heading_path": c.heading_path,
            "page_from": c.page_from,
            "page_to": c.page_to,
            "rerank_score": c.rerank_score,
            "acl_result": c.acl_result,
            "page_check": c.page_check,
            "figure_id": c.figure_id,
            "image_path": c.image_path,
            # chunk_id 是喂给 LLM 的上下文块；source_chunk_id 是扩展前的精排来源块
            # （父块/整表容器不在精排候选里属正常）。旧缓存条目没有该键 → None。
            "source_chunk_id": getattr(c, "source_chunk_id", None),
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
    if payload.conversation_type == "2":
        # 群聊：回复会公开到群，先剪掉个人私有库（个人分片/KB），只保留公开文档。
        from ekbs.permission.public_scope import public_scope_perm

        perm = public_scope_perm(perm, runtime.db)
        log.info(
            "qa_group_public_scope",
            user_id=perm.user_id,
            conversation_id=payload.conversation_id,
            shards=len(perm.visible_shard_ids),
        )
    # 内容安全闸门：拦截 / 打码 / 降级（先过闸，再进检索与云端）
    safe_question, allow_generation = _guard_input(runtime, perm, payload.question)
    answer = await runtime.pipeline.answer(
        safe_question, perm,
        conversation_id=payload.conversation_id,
        use_cache=payload.use_cache,
        allow_generation=allow_generation,
        mode=payload.mode,
        fresh_on_repeat=payload.fresh_on_repeat,
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

    answer_text = answer.answer
    # 推荐问答：**拒答和正常作答都要算**（延展 / 相关 / 预判）。
    # 拒答时把提示直接拼进答案正文（各通道无需再处理）；正常作答只走结构化字段
    # `suggested_questions`，由钉钉/管理台按自己的排版决定是否展示。
    from ekbs.discovery import format_refusal_hints
    from ekbs.recommend import recommend_questions

    # 成功作答：用本轮引用文档优先抽推荐（仍过权限）；拒答不传 focus，走拒答导流。
    focus_doc_ids: list[int] | None = None
    if not answer.refused:
        seen_docs: set[int] = set()
        focus_doc_ids = []
        for cite in answer.citations or []:
            doc_id = int(getattr(cite, "doc_id", 0) or 0)
            if doc_id > 0 and doc_id not in seen_docs:
                seen_docs.add(doc_id)
                focus_doc_ids.append(doc_id)
    suggested = recommend_questions(
        runtime,
        perm,
        limit=3,
        query=safe_question,
        for_refusal=answer.refused,
        focus_doc_ids=focus_doc_ids,
    )
    if answer.refused:
        hint = format_refusal_hints(suggested)
        if hint and "你可以试试问" not in (answer_text or ""):
            answer_text = (answer_text or "未找到可靠依据。").rstrip() + hint

    body = {
        "answer": answer_text,
        "question": safe_question,
        "rewritten_query": answer.rewritten_query,
        "history_used": answer.history_used,
        # 多意图拆分（P0）：单问时为空列表/空 dict，老客户端不受影响
        "sub_questions": answer.sub_questions,
        "coverage": answer.coverage,
        "citations": _citation_payload(answer.citations),
        "refused": answer.refused,
        # 输出被截断（撞 max_tokens 上限）：答案不完整，前端要显式提示
        "truncated": answer.truncated,
        "refusal_reason": answer.refusal_reason,
        "suggested_questions": [s["question"] for s in suggested],
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
        "mode": payload.mode,
        "mode_used": answer.mode_used,
        "intent": answer.intent,
        "available_figures": list(
            answer.available_figures or answer.figure_attachments or []
        ),
        "figure_attachments": list(
            answer.available_figures or answer.figure_attachments or []
        ),
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
    queue: asyncio.Queue[str] = asyncio.Queue()

    def _on_token(piece: str) -> None:
        queue.put_nowait(piece)

    async def _run_answer() -> Any:
        return await runtime.pipeline.answer(
            safe_question,
            perm,
            conversation_id=payload.conversation_id,
            use_cache=False,
            allow_generation=allow_generation,
            fresh_on_repeat=payload.fresh_on_repeat,
            mode=payload.mode,
            token_callback=_on_token,
        )

    async def event_stream() -> Any:
        # 先发 meta（兼容旧客户端）；最终 meta 再带 citations/refused。
        yield "event: meta\ndata: " + json.dumps({"status": "started"}, ensure_ascii=False) + "\n\n"
        task = asyncio.create_task(_run_answer())
        streamed = ""
        try:
            while True:
                if task.done() and queue.empty():
                    break
                try:
                    piece = await asyncio.wait_for(queue.get(), timeout=0.05)
                except asyncio.TimeoutError:
                    continue
                if piece:
                    streamed += piece
                    yield f"event: delta\ndata: {json.dumps({'text': piece}, ensure_ascii=False)}\n\n"
            answer = await task
        except Exception as exc:  # noqa: BLE001 —— SSE 不能把异常变成连接静默断开
            log.error("qa_stream_failed", error=str(exc)[:300])
            yield "event: error\ndata: " + json.dumps({"message": "流式回答失败"}, ensure_ascii=False) + "\n\n"
            return
        finally:
            if not task.done():
                task.cancel()
        final_text = answer.answer or ""
        # 生成后置门禁可能改写/拒答已流的 token：发 replace，客户端应以最终文本为准。
        if streamed and final_text != streamed:
            yield f"event: replace\ndata: {json.dumps({'text': final_text}, ensure_ascii=False)}\n\n"
        yield "event: final\ndata: " + json.dumps(
            {
                "answer": final_text,
                "citations": _citation_payload(answer.citations),
                "refused": answer.refused,
                "refusal_reason": answer.refusal_reason,
                "perm_fingerprint": perm.fingerprint,
                "timings_ms": answer.retrieval.timings_ms if answer.retrieval else {},
                "numeric": answer.numeric,
                "model": answer.model,
            },
            ensure_ascii=False,
        ) + "\n\n"
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
