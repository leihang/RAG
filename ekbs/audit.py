"""审计写入 —— `01` §5.4 的三张问答审计表 + `00` §3.5 的留存要求。

三张表都是 **append-only**（`01` §5.6 的触发器硬拒 UPDATE/DELETE），因此这里只做 INSERT；
``ekbs_qa_session`` 的 ``ended_at`` / ``message_count`` 是唯一被白名单允许的更新。

引用表**只存 hash 与页码，不存片段全文** —— 审计库被越权访问时不应泄露内容（`01` §5.4 注释）。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from ekbs.db import Database
from ekbs.logging import get_logger
from ekbs.permission.context import PermContext
from ekbs.retrieval.pipeline import AnswerResult, Citation

log = get_logger(component="audit")


@dataclass(frozen=True)
class TurnRecord:
    session_id: str
    user_id: int
    dt_userid: str
    channel: str
    conversation_id: str | None
    perm_fingerprint: str
    clearance: int
    turn: int


class AuditService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------

    def open_session(self, perm: PermContext, *, channel: str, conversation_id: str | None) -> str:
        """会话不存在则建；返回 ``session_id``。"""
        session_id = str(uuid.uuid4())
        with self.db.sync_connect() as conn:
            conn.execute(
                "INSERT INTO ekbs_qa_session(session_id, user_id, dt_userid, channel, "
                "conversation_id, perm_fingerprint, clearance_at, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                (
                    session_id, perm.user_id, perm.dingtalk_userid, channel, conversation_id,
                    perm.fingerprint, perm.clearance,
                ),
            )
            conn.commit()
        return session_id

    def next_turn(self, session_id: str) -> int:
        with self.db.sync_connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(turn), 0) AS t FROM ekbs_qa_message WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row["t"]) + 1

    def find_session(
        self, *, user_id: int, conversation_id: str, ttl_seconds: int = 0
    ) -> str | None:
        """按 ``(user_id, conversation_id)`` 找回**未过期**的会话。

        ⚠️ 这里踩过坑：``conversation_id`` 是钉钉侧的会话标识，**不是**
        ``ekbs_qa_session.session_id``（那是应用生成的 UUID）。早先的实现拿
        ``conversation_id`` 去查 ``session_id``，永远查不到，于是每轮问答都新建一个会话，
        ``turn`` 恒为 1 —— 「按会话完整回溯」直接失效（00 §4 要求审计完备率 100%）。
        """
        if not conversation_id:
            return None
        sql = (
            "SELECT session_id FROM ekbs_qa_session WHERE user_id = ? AND conversation_id = ? "
        )
        params: list[object] = [user_id, conversation_id]
        if ttl_seconds:
            sql += "AND (strftime('%s','now') - strftime('%s', started_at)) <= ? "
            params.append(int(ttl_seconds))
        sql += "ORDER BY started_at DESC LIMIT 1"
        with self.db.sync_connect() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
        return str(row["session_id"]) if row else None

    def resolve_session(
        self, perm: PermContext, *, channel: str, conversation_id: str | None, ttl_seconds: int = 0
    ) -> tuple[str, int]:
        """返回 ``(session_id, turn)``：同一会话复用，``turn`` 在会话内递增。"""
        session_id = self.find_session(
            user_id=perm.user_id, conversation_id=conversation_id or "", ttl_seconds=ttl_seconds
        )
        if session_id is None:
            session_id = self.open_session(perm, channel=channel, conversation_id=conversation_id)
            return session_id, 1
        return session_id, self.next_turn(session_id)

    # ------------------------------------------------------------------

    def record_turn(
        self,
        record: TurnRecord,
        *,
        question: str,
        answer: AnswerResult,
        trace_id: str = "",
        rewritten_query: str = "",
    ) -> int:
        """写入 user / assistant 两条消息 + 引用行，并更新会话计数。返回 ``message_id``。"""
        retrieval = answer.retrieval
        timings = retrieval.timings_ms
        with self.db.sync_connect() as conn:
            conn.execute(
                "INSERT INTO ekbs_qa_message(session_id, turn, role, question, rewritten_query, "
                "answer, model, prompt_tokens, completion_tokens, ttft_ms, total_ms, retrieve_ms, "
                "rerank_ms, generate_ms, candidate_count, rerank_top_score, refused, trace_id, "
                "context_chunk_ids) "
                "VALUES (?, ?, 'assistant', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.session_id, record.turn, question,
                    rewritten_query or answer.rewritten_query, answer.answer, answer.model,
                    answer.prompt_tokens, answer.completion_tokens, answer.ttft_ms,
                    answer.total_ms, int(timings.get("recall", 0) or 0),
                    int(timings.get("rerank", 0) or 0), answer.ttft_ms,
                    retrieval.candidate_count, retrieval.top_score,
                    1 if answer.refused else 0, trace_id,
                    # 「模型实际看到的上下文」：引用只说明用了哪几块，这里记的是喂了哪几块
                    # （doc 205 的 35281 进了 prompt 但没被引用，只查引用会误判"没发出去"）。
                    json.dumps(answer.context_chunk_ids, ensure_ascii=False),
                ),
            )
            message_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])

            conn.executemany(
                "INSERT INTO ekbs_qa_citation(message_id, cite_index, chunk_id, "
                "source_chunk_id, doc_id, doc_version, heading_path, page_from, page_to, "
                "rerank_score, acl_result, page_check, snippet_hash, figure_id, image_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        message_id, c.cite_index, c.chunk_id, c.source_chunk_id, c.doc_id,
                        c.doc_version, c.heading_path, c.page_from, c.page_to, c.rerank_score,
                        c.acl_result, c.page_check, _snippet_hash(c), c.figure_id, c.image_path,
                    )
                    for c in answer.citations
                ],
            )
            # 唯一允许的会话更新（ended_at / message_count，见 01 §5.6 的 UPDATE 守卫）
            conn.execute(
                "UPDATE ekbs_qa_session SET message_count = message_count + 2, "
                "ended_at = datetime('now') WHERE session_id = ?",
                (record.session_id,),
            )
            conn.commit()
        return message_id

    def record_access(
        self, *, user_id: int, doc_id: int, action: str, channel: str, result: str
    ) -> None:
        with self.db.sync_connect() as conn:
            conn.execute(
                "INSERT INTO ekbs_doc_access_log(user_id, doc_id, action, channel, result) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, doc_id, action, channel, result),
            )
            conn.commit()


def _snippet_hash(citation: Citation) -> str:
    """片段指纹（不存全文）：用于事后核对「当时引用的到底是哪一段」。

    指纹必须同时覆盖**上下文块**与**精排来源**：同一父块可由不同精排叶子扩展而来
    （doc 205 的 35274 ← 35277 / 35276），只 hash 父块 chunk_id 时，事后无法核对
    「这次回源到底指哪个被精排选中的叶子」，审计与回源就会各说各话。
    """
    raw = (
        f"{citation.doc_id}|{citation.chunk_id}|{citation.source_chunk_id}|"
        f"{citation.page_from}|{citation.page_to}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


__all__ = ["AuditService", "TurnRecord"]
