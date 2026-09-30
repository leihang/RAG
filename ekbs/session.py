"""多轮会话上下文与查询改写（`00` §2.1、`04` §5.5、`01` §5.5 的 ``ekbs_session_ctx``）。

两件事放在一起，因为它们共用同一份历史：

1. **会话上下文**：落 ``ekbs_session_ctx``（**替代 Redis Hash**），主键是
   ``(conversation_id, dingtalk_userid)`` —— 群聊的 ``conversation_id`` 是**共享**的，
   因此必须带用户维度，否则群里的 A 会读到 B 的上下文（`01` §5.5 的注释、
   ``session.group_share_context`` 默认 false）。TTL 由 ``context_ttl_seconds`` 控制，
   保留轮数由 ``context_max_turns`` 控制。
2. **查询改写**：`00` §2.1 把「查询改写」列为链路第一步，触发条件是**多轮对话**。
   策略可配（管理台的 Prompt 模板对应 ``llm`` 策略）：

   * ``off``：不改写；
   * ``heuristic``（默认）：当前问题含指代（它/这个/上述/该…）或过短时，
     把**上一轮问题**并入检索查询，保证脱离上下文也能召回；
   * ``llm``：用云端 LLM 按模板改写成自包含查询（需要真实模型；mock 会返回固定串，
     因此默认不启用，避免把测试与离线环境搞坏）。

**为什么要改写而不是直接把历史塞给检索**：FAISS/FTS5 是对「一个查询串」召回的，
把历史对话整段拼进去会稀释查询向量（相似度被无关轮次拉平），实测通常反而降召回。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from ekbs.config import Settings
from ekbs.db import Database
from ekbs.logging import get_logger

log = get_logger(component="session")

#: 需要上下文的指代词（命中则倾向于改写）
_ANAPHORA = ("它", "他", "她", "这个", "那个", "这些", "那些", "上述", "该", "此", "这项", "那条")

DEFAULT_MAX_TURNS = 6
DEFAULT_TTL_SECONDS = 7200


@dataclass
class RewriteResult:
    original: str
    rewritten: str
    strategy: str
    used_history: bool = False

    @property
    def changed(self) -> bool:
        return self.rewritten.strip() != self.original.strip()


class SessionStore:
    """``ekbs_session_ctx`` 的读写（键含用户维度，防群聊越权）。"""

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    @property
    def max_turns(self) -> int:
        return int(self.settings.session.get("context_max_turns", DEFAULT_MAX_TURNS) or 0)

    @property
    def ttl_seconds(self) -> int:
        return int(self.settings.session.get("context_ttl_seconds", DEFAULT_TTL_SECONDS) or 0)

    # ------------------------------------------------------------------

    def load(self, conversation_id: str, dingtalk_userid: str) -> list[dict[str, str]]:
        """取该用户在该会话里的历史消息（已按 TTL 与轮数裁剪）。"""
        if not conversation_id or not dingtalk_userid:
            return []
        with self.db.sync_connect() as conn:
            row = conn.execute(
                "SELECT payload, updated_at, "
                "       (strftime('%s','now') - strftime('%s', updated_at)) AS age "
                "FROM ekbs_session_ctx WHERE conversation_id = ? AND dingtalk_userid = ?",
                (conversation_id, dingtalk_userid),
            ).fetchone()
        if row is None:
            return []
        if self.ttl_seconds and int(row["age"] or 0) > self.ttl_seconds:
            log.info(
                "session_context_expired",
                conversation_id=conversation_id, age_s=int(row["age"] or 0),
            )
            return []
        try:
            payload = json.loads(row["payload"] or "[]")
        except ValueError:
            return []
        messages = [
            {"role": str(item.get("role", "")), "content": str(item.get("content", ""))}
            for item in payload
            if isinstance(item, dict)
        ]
        # 只保留最近 N 轮（一轮 = 用户 + 助手两条）
        limit = self.max_turns * 2 if self.max_turns else len(messages)
        return messages[-limit:] if limit else messages

    def append(
        self,
        conversation_id: str,
        dingtalk_userid: str,
        messages: Sequence[dict[str, str]],
    ) -> int:
        """把本轮问答追加进上下文并返回当前消息数（超限自动裁剪）。"""
        if not conversation_id or not dingtalk_userid or not messages:
            return 0
        history = self.load(conversation_id, dingtalk_userid)
        history.extend(dict(m) for m in messages)
        limit = self.max_turns * 2 if self.max_turns else len(history)
        if limit:
            history = history[-limit:]
        payload = json.dumps(history, ensure_ascii=False)
        turn = len(history)
        with self.db.sync_connect() as conn:
            conn.execute(
                "INSERT INTO ekbs_session_ctx(conversation_id, dingtalk_userid, turn, payload, "
                "updated_at) VALUES (?, ?, ?, ?, datetime('now')) "
                "ON CONFLICT(conversation_id, dingtalk_userid) DO UPDATE SET "
                "turn = excluded.turn, payload = excluded.payload, updated_at = excluded.updated_at",
                (conversation_id, dingtalk_userid, turn, payload),
            )
            conn.commit()
        return turn

    def clear(self, conversation_id: str, dingtalk_userid: str) -> int:
        with self.db.sync_connect() as conn:
            cursor = conn.execute(
                "DELETE FROM ekbs_session_ctx WHERE conversation_id = ? AND dingtalk_userid = ?",
                (conversation_id, dingtalk_userid),
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    def cleanup_expired(self) -> int:
        """由 ``com.ekbs.scheduler`` 调用（`01` §5.5 的会话清理）。"""
        cutoff = self.ttl_seconds or DEFAULT_TTL_SECONDS
        with self.db.sync_connect() as conn:
            cursor = conn.execute(
                "DELETE FROM ekbs_session_ctx "
                "WHERE strftime('%s','now') - strftime('%s', updated_at) > ?",
                (cutoff,),
            )
            conn.commit()
            return int(cursor.rowcount or 0)


@dataclass
class QueryRewriter:
    """查询改写（策略可配，默认启发式）。"""

    settings: Settings
    llm: Any | None = None
    prompt_template: str = (
        "把用户的最新问题改写成一个不依赖上下文、可独立检索的查询。"
        "只输出改写后的查询本身，不要解释。\n\n对话历史：\n{history}\n\n最新问题：{question}"
    )
    enabled: bool = True
    strategy_override: str | None = None
    _last: RewriteResult | None = field(default=None, init=False, repr=False)

    @property
    def strategy(self) -> str:
        if self.strategy_override:
            return self.strategy_override
        cfg = (self.settings.retrieval.model_extra or {}).get("query_rewrite") or {}
        return str(cfg.get("strategy", "heuristic"))

    async def rewrite(
        self, question: str, history: Sequence[dict[str, str]] = ()
    ) -> RewriteResult:
        if not self.enabled or not history:
            return RewriteResult(question, question, "off", used_history=False)

        previous_question = self._last_user_question(history)
        if not previous_question:
            return RewriteResult(question, question, self.strategy, used_history=False)

        strategy = self.strategy
        if strategy == "off":
            return RewriteResult(question, question, "off", used_history=False)
        if strategy == "llm" and self.llm is not None and not _is_mock(self.llm):
            rewritten = await self._rewrite_with_llm(question, history)
            if rewritten:
                return RewriteResult(question, rewritten, "llm", used_history=True)
            return RewriteResult(question, question, "llm_failed", used_history=False)

        # 启发式：仅在「问题本身不自足」时才并入上一轮，避免污染语义明确的查询
        if _needs_context(question):
            merged = f"{previous_question} {question}".strip()
            return RewriteResult(question, merged, "heuristic", used_history=True)
        return RewriteResult(question, question, "heuristic", used_history=False)

    # ------------------------------------------------------------------

    @staticmethod
    def _last_user_question(history: Sequence[dict[str, str]]) -> str:
        for message in reversed(list(history)):
            if str(message.get("role")) == "user" and str(message.get("content", "")).strip():
                return str(message["content"]).strip()
        return ""

    async def _rewrite_with_llm(
        self, question: str, history: Sequence[dict[str, str]]
    ) -> str:
        rendered = "\n".join(
            f"{m.get('role')}: {m.get('content')}" for m in list(history)[-6:]
        )
        prompt = self.prompt_template.format(history=rendered, question=question)
        try:
            result = await self.llm.generate([{"role": "user", "content": prompt}])
        except Exception as exc:
            log.warning("query_rewrite_failed", error=str(exc))
            return ""
        text = (result.text or "").strip().splitlines()
        return text[0].strip() if text else ""


def _needs_context(question: str) -> bool:
    """问题是否依赖上下文：含指代词，或过短（少于 6 个字符且非纯英文短句）。"""
    text = (question or "").strip()
    if not text:
        return False
    if any(marker in text for marker in _ANAPHORA):
        return True
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    return len(cjk) < 6


def _is_mock(provider: Any) -> bool:
    ref = getattr(provider, "ref", None)
    return bool(ref is not None and str(getattr(ref, "provider", "")).startswith("mock"))
