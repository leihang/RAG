"""LangGraph 节点适配：state 进、带 ekbs 字段的 state 出。

也可直接把 ``build_langchain_tool()`` 丢进 ``ToolNode``，与本节点二选一即可。
"""

from __future__ import annotations

from typing import Any, Mapping

from .client import EkbsClient


def ekbs_ask_node(
    state: Mapping[str, Any],
    *,
    client: EkbsClient | None = None,
    user_id: int | None = None,
    question_key: str = "question",
    conversation_key: str = "conversation_id",
) -> dict[str, Any]:
    """从图 state 读问题，写入 ``ekbs_answer`` / ``ekbs_refused`` / ``ekbs_text``。"""
    ekbs = client or EkbsClient.from_env()
    question = str(state.get(question_key) or state.get("input") or "").strip()
    if not question:
        raise ValueError(f"state 缺少问题字段：{question_key}")
    conversation_id = state.get(conversation_key)
    uid = user_id if user_id is not None else state.get("user_id")
    if uid is not None:
        uid = int(uid)
    result = ekbs.ask(
        question,
        user_id=uid,
        conversation_id=str(conversation_id) if conversation_id else None,
    )
    return {
        "ekbs_answer": result.answer,
        "ekbs_refused": result.refused,
        "ekbs_refusal_reason": result.refusal_reason,
        "ekbs_citations": result.citations,
        "ekbs_text": result.as_tool_text(),
        "ekbs_trace_id": result.trace_id,
    }
