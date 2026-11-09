"""EKBS 企业知识库 HTTP 客户端（编排层共用核心）。

只打 ``POST /api/v1/qa/chat``，不在外层重建向量索引。
鉴权与钉钉回环同口径：``X-Internal-Token`` + ``X-Internal-User-Id``，
或 ``Authorization: Bearer <jwt>``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8000/api/v1"
INTERNAL_TOKEN_HEADER = "X-Internal-Token"
INTERNAL_USER_HEADER = "X-Internal-User-Id"
INTERNAL_CHANNEL_HEADER = "X-Internal-Channel"


@dataclass
class EkbsAskResult:
    """``/qa/chat`` 归一化结果；``refused=True`` 时外层 Agent 不得再编制度答案。"""

    answer: str
    refused: bool = False
    refusal_reason: str | None = None
    citations: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    trace_id: str | None = None
    suggested_questions: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def as_tool_text(self) -> str:
        """给 LLM tool 回传的纯文本（含拒答标记与引用摘要）。"""
        if self.refused:
            reason = self.refusal_reason or "unknown"
            return f"[EKBS 拒答 reason={reason}]\n{self.answer}".strip()
        cites = []
        for c in self.citations:
            idx = c.get("cite_index")
            title = c.get("doc_title") or c.get("doc_id") or "?"
            page = c.get("page_from")
            bit = f"[{idx}] {title}"
            if page is not None:
                bit += f" p.{page}"
            cites.append(bit)
        footer = ("\n引用：" + "；".join(cites)) if cites else ""
        return f"{self.answer}{footer}".strip()


@dataclass
class EkbsClient:
    """薄 HTTP 客户端。``user_id`` 决定 ACL，禁止共用超级账号。"""

    base_url: str = DEFAULT_BASE_URL
    internal_token: str | None = None
    bearer_token: str | None = None
    default_user_id: int | None = None
    channel: str = "agent_tool"
    timeout_s: float = 120.0

    @classmethod
    def from_env(cls) -> EkbsClient:
        """读环境变量（与 ``~/.ekbs/secrets.env`` / 编排侧约定对齐）。"""
        user_raw = os.environ.get("EKBS_TOOL_USER_ID") or os.environ.get("EKBS_INTERNAL_USER_ID")
        return cls(
            base_url=(os.environ.get("EKBS_API_BASE") or DEFAULT_BASE_URL).rstrip("/"),
            internal_token=os.environ.get("EKBS_INTERNAL_TOKEN"),
            bearer_token=os.environ.get("EKBS_BEARER_TOKEN"),
            default_user_id=int(user_raw) if user_raw else None,
            channel=os.environ.get("EKBS_TOOL_CHANNEL") or "agent_tool",
            timeout_s=float(os.environ.get("EKBS_TOOL_TIMEOUT_S") or 120),
        )

    def _headers(self, user_id: int | None) -> dict[str, str]:
        headers = {"Content-Type": "application/json", INTERNAL_CHANNEL_HEADER: self.channel}
        uid = user_id if user_id is not None else self.default_user_id
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        if self.internal_token:
            headers[INTERNAL_TOKEN_HEADER] = self.internal_token
            if uid is None:
                raise ValueError(
                    "内部回环鉴权需要 user_id：构造时传 user_id，或设 EKBS_TOOL_USER_ID"
                )
            headers[INTERNAL_USER_HEADER] = str(int(uid))
        if not self.bearer_token and not self.internal_token:
            raise ValueError("未配置鉴权：设 EKBS_INTERNAL_TOKEN 或 EKBS_BEARER_TOKEN")
        return headers

    def ask(
        self,
        question: str,
        *,
        user_id: int | None = None,
        conversation_id: str | None = None,
        mode: str = "auto",
        use_cache: bool = True,
        fresh_on_repeat: bool = False,
    ) -> EkbsAskResult:
        q = (question or "").strip()
        if not q:
            raise ValueError("question 不能为空")
        url = f"{self.base_url}/qa/chat"
        payload: dict[str, Any] = {
            "question": q,
            "mode": mode,
            "use_cache": use_cache,
            "fresh_on_repeat": fresh_on_repeat,
        }
        if conversation_id:
            payload["conversation_id"] = conversation_id
        with httpx.Client(timeout=self.timeout_s) as http:
            resp = http.post(url, headers=self._headers(user_id), json=payload)
            resp.raise_for_status()
            data = resp.json()
        return EkbsAskResult(
            answer=str(data.get("answer") or ""),
            refused=bool(data.get("refused")),
            refusal_reason=data.get("refusal_reason"),
            citations=list(data.get("citations") or []),
            model=data.get("model"),
            trace_id=data.get("trace_id"),
            suggested_questions=[
                str(x) for x in (data.get("suggested_questions") or []) if str(x).strip()
            ],
            raw=data if isinstance(data, dict) else {},
        )

    def ask_text(self, question: str, **kwargs: Any) -> str:
        return self.ask(question, **kwargs).as_tool_text()
