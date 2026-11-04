"""审计引用行要落「上下文块 + 精排来源」两个身份（task-3）。

背景：父块扩展后 ``chunk_id`` 是喂给 LLM 的上下文块（35274），而精排真正选中
的是叶子（35277）。审计表只存 chunk_id 时，事后无法核对回源到底指哪一块。
task-5 追加：消息行还要落「模型实际看到的上下文块列表」（context_chunk_ids），
否则只看引用会把「已发送但未引用」误判成「没发送」。
"""

from __future__ import annotations

import json

from ekbs.audit import AuditService, TurnRecord, _snippet_hash
from ekbs.retrieval.pipeline import AnswerResult, Citation, RetrievalResult


def _citation(
    *, chunk_id: int = 35274, source_chunk_id: int | None = 35277,
) -> Citation:
    return Citation(
        cite_index=1,
        chunk_id=chunk_id,
        doc_id=205,
        doc_title="XJWY-GC-12 能耗管理作业指导书",
        doc_version="v1",
        heading_path="5. 工作程序和要求 > 5.1 能耗管理员职责",
        page_from=3,
        page_to=3,
        rerank_score=0.899,
        acl_result="pass",
        page_check="pass",
        source_chunk_id=source_chunk_id,
    )


def _open_session(db) -> str:
    session_id = "s-audit-citation"
    with db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_qa_session(session_id, user_id, channel, perm_fingerprint, "
            "clearance_at) VALUES (?, 1, 'web', 'fp', 2)",
            (session_id,),
        )
        conn.commit()
    return session_id


def _answer(
    citations: list[Citation], context_chunk_ids: list[int] | None = None
) -> AnswerResult:
    return AnswerResult(
        answer="能耗管理员负责…… [1]",
        citations=citations,
        retrieval=RetrievalResult(query="能耗管理员在能耗管理中的主要作用"),
        model="audit-test",
        context_chunk_ids=list(context_chunk_ids or []),
    )


def test_record_turn_persists_source_chunk_id(db) -> None:
    """引用行同时落 chunk_id（上下文块）与 source_chunk_id（精排来源块）。"""
    session_id = _open_session(db)
    message_id = AuditService(db).record_turn(
        TurnRecord(
            session_id=session_id,
            user_id=1,
            dt_userid="",
            channel="web",
            conversation_id=None,
            perm_fingerprint="fp",
            clearance=2,
            turn=1,
        ),
        question="能耗管理员在能耗管理中的主要作用",
        answer=_answer([_citation()]),
    )

    with db.sync_connect() as conn:
        row = conn.execute(
            "SELECT chunk_id, source_chunk_id, snippet_hash FROM ekbs_qa_citation "
            "WHERE message_id = ?",
            (message_id,),
        ).fetchone()
    assert row is not None
    assert int(row["chunk_id"]) == 35274, "chunk_id 是实际喂给 LLM 的上下文块"
    assert int(row["source_chunk_id"]) == 35277, "source_chunk_id 是精排选中的叶子"
    assert row["snippet_hash"]


def test_record_turn_persists_null_source_for_unexpanded_citation(db) -> None:
    session_id = _open_session(db)
    message_id = AuditService(db).record_turn(
        TurnRecord(
            session_id=session_id,
            user_id=1,
            dt_userid="",
            channel="web",
            conversation_id=None,
            perm_fingerprint="fp",
            clearance=2,
            turn=1,
        ),
        question="q",
        answer=_answer([_citation(chunk_id=35276, source_chunk_id=None)]),
    )
    with db.sync_connect() as conn:
        row = conn.execute(
            "SELECT chunk_id, source_chunk_id FROM ekbs_qa_citation WHERE message_id = ?",
            (message_id,),
        ).fetchone()
    assert row is not None
    assert int(row["chunk_id"]) == 35276
    assert row["source_chunk_id"] is None, "未扩展的块自己就是精排候选"


def test_snippet_hash_covers_context_and_ranked_source() -> None:
    """同一父块可由不同精排叶子扩展而来，指纹必须能区分两者。"""
    from_leaf_a = _snippet_hash(_citation(source_chunk_id=35277))
    from_leaf_b = _snippet_hash(_citation(source_chunk_id=35276))
    plain = _snippet_hash(_citation(source_chunk_id=None))
    assert len({from_leaf_a, from_leaf_b, plain}) == 3


def _ordered_context_ids(message_id: int, db) -> list[int]:
    with db.sync_connect() as conn:
        row = conn.execute(
            "SELECT context_chunk_ids FROM ekbs_qa_message WHERE message_id = ?",
            (message_id,),
        ).fetchone()
    assert row is not None
    return json.loads(row["context_chunk_ids"])


def test_record_turn_persists_context_chunk_ids(db) -> None:
    """消息行落「模型实际看到的上下文」：引用只是它的子集。"""
    session_id = _open_session(db)
    context_ids = [35280, 35274, 35281]  # doc 205：第 3 块进了 prompt 但没被引用
    message_id = AuditService(db).record_turn(
        TurnRecord(
            session_id=session_id,
            user_id=1,
            dt_userid="",
            channel="web",
            conversation_id=None,
            perm_fingerprint="fp",
            clearance=2,
            turn=1,
        ),
        question="能耗管理员在能耗管理中的主要作用",
        answer=_answer([_citation()], context_chunk_ids=context_ids),
    )

    assert _ordered_context_ids(message_id, db) == context_ids
    # 引用只有 1 行 → 恰好说明「引用不是上下文全集」
    with db.sync_connect() as conn:
        cited = conn.execute(
            "SELECT COUNT(*) AS c FROM ekbs_qa_citation WHERE message_id = ?", (message_id,)
        ).fetchone()["c"]
    assert int(cited) == 1 == len(_answer([_citation()]).citations)


def test_record_turn_without_context_ids_writes_empty_json_array(db) -> None:
    """无上下文信息时写 ``[]``：前端解析统一走 JSON，不用区分 NULL / 缺失。"""
    session_id = _open_session(db)
    message_id = AuditService(db).record_turn(
        TurnRecord(
            session_id=session_id,
            user_id=1,
            dt_userid="",
            channel="web",
            conversation_id=None,
            perm_fingerprint="fp",
            clearance=2,
            turn=1,
        ),
        question="q",
        answer=_answer([_citation()]),
    )
    assert _ordered_context_ids(message_id, db) == []
