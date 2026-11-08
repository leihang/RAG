"""推荐问答三档（`ekbs/recommend.py`）：代表问句(3) > 热门(2) > 原逻辑(1)。"""

from __future__ import annotations

import uuid

from ekbs.recommend import (
    doc_recommend_questions,
    hot_questions,
    recommend_questions,
    upsert_doc_recommend,
)
from tests.conftest import user_id_of


def _doc_id(runtime, *, title: str) -> int:
    with runtime.db.sync_connect() as conn:
        row = conn.execute("SELECT doc_id FROM ekbs_document WHERE title = ?", (title,)).fetchone()
        assert row is not None, f"未找到文档 {title}"
        return int(row["doc_id"])


def _chunk_of(runtime, *, doc_id: int) -> tuple[int, str]:
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT chunk_id, shard_id FROM ekbs_chunk WHERE doc_id = ? LIMIT 1", (doc_id,)
        ).fetchone()
        assert row is not None, f"文档 {doc_id} 无分块"
        return int(row["chunk_id"]), str(row["shard_id"])


def _insert_qa(
    runtime, *, question: str, chunk_id: int, doc_id: int, user_id: int, count: int = 1
) -> None:
    """插入 ``count`` 条同问题的已答问答（refused=0 + 引用），供 hot_questions 聚合。"""
    with runtime.db.sync_connect() as conn:
        for _ in range(count):
            session_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO ekbs_qa_session(session_id, user_id, channel, perm_fingerprint, clearance_at) "
                "VALUES (?, ?, 'web', 'fp', 0)",
                (session_id, user_id),
            )
            # 与 AuditService.record_turn 一致：问题+引用都挂在 assistant 行
            conn.execute(
                "INSERT INTO ekbs_qa_message(session_id, turn, role, question, answer, refused) "
                "VALUES (?, 1, 'assistant', ?, 'x', 0)",
                (session_id, question),
            )
            message_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                "INSERT INTO ekbs_qa_citation(message_id, cite_index, chunk_id, doc_id, acl_result, page_check) "
                "VALUES (?, 1, ?, ?, 'pass', 'pass')",
                (message_id, chunk_id, doc_id),
            )
        conn.commit()


async def test_hot_questions_filters_by_frequency_and_permission(seeded) -> None:
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))

    hr_doc = _doc_id(runtime, title="年假管理制度")
    hr_chunk, hr_shard = _chunk_of(runtime, doc_id=hr_doc)
    assert hr_shard in alice.visible_shard_ids

    # 热门：同问题出现 2 次（近 90 天、refused=0、命中可见分片）→ 上榜
    _insert_qa(
        runtime, question="年假可以结转吗？", chunk_id=hr_chunk, doc_id=hr_doc,
        user_id=alice.user_id, count=2,
    )
    # 低频：只出现 1 次 → 被 min_count=2 过滤
    _insert_qa(
        runtime, question="年假最多几天？", chunk_id=hr_chunk, doc_id=hr_doc,
        user_id=alice.user_id, count=1,
    )
    hot = [h["question"] for h in hot_questions(runtime, alice, limit=10)]
    assert "年假可以结转吗？" in hot
    assert "年假最多几天？" not in hot

    # 权限：finance 分片的问题（出现 3 次）对 alice 不可见，不应上榜
    fin_doc = _doc_id(runtime, title="财务保密制度")
    fin_chunk, fin_shard = _chunk_of(runtime, doc_id=fin_doc)
    assert fin_shard not in alice.visible_shard_ids
    _insert_qa(
        runtime, question="财务专用章由谁保管？", chunk_id=fin_chunk, doc_id=fin_doc,
        user_id=alice.user_id, count=3,
    )
    hot2 = [h["question"] for h in hot_questions(runtime, alice, limit=10)]
    assert "财务专用章由谁保管？" not in hot2


async def test_doc_recommend_fetch_respects_visibility(seeded) -> None:
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))

    hr_doc = _doc_id(runtime, title="年假管理制度")
    fin_doc = _doc_id(runtime, title="财务保密制度")

    upsert_doc_recommend(runtime, doc_id=hr_doc, question="年假怎么算？", rerank_score=0.8)
    upsert_doc_recommend(runtime, doc_id=fin_doc, question="财务章谁管？", rerank_score=0.9)

    questions = [r["question"] for r in doc_recommend_questions(runtime, alice, limit=10)]
    assert "年假怎么算？" in questions
    assert "财务章谁管？" not in questions


async def test_recommend_questions_orders_doc_then_hot(seeded) -> None:
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))

    hr_doc = _doc_id(runtime, title="年假管理制度")
    hr_chunk, _ = _chunk_of(runtime, doc_id=hr_doc)

    _insert_qa(
        runtime, question="年假可以结转吗？", chunk_id=hr_chunk, doc_id=hr_doc,
        user_id=alice.user_id, count=2,
    )
    upsert_doc_recommend(runtime, doc_id=hr_doc, question="年假怎么算？", rerank_score=0.8)

    recs = recommend_questions(runtime, alice, limit=3)
    questions = [r["question"] for r in recs]
    assert "年假怎么算？" in questions, "代表问句(3)应上榜"
    assert "年假可以结转吗？" in questions, "热门(2)应上榜"
    # 代表问句(3) 必须排在热门(2)之前
    assert questions.index("年假怎么算？") < questions.index("年假可以结转吗？")


async def test_recommend_focus_doc_ids_prefer_cited_doc(seeded) -> None:
    """成功作答：focus 文档代表问优先于其他可见文档的代表问。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))

    handbook = _doc_id(runtime, title="员工手册（总则）")
    hr_doc = _doc_id(runtime, title="年假管理制度")
    # 手册更新更「新」时，无 focus 可能先推手册；带 focus=年假 必须先推年假
    upsert_doc_recommend(runtime, doc_id=handbook, question="考勤怎么打卡？", rerank_score=0.9)
    upsert_doc_recommend(runtime, doc_id=hr_doc, question="年假怎么算？", rerank_score=0.8)
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_document SET updated_at = datetime('now', '+1 day') WHERE doc_id = ?",
            (handbook,),
        )
        conn.commit()

    focused = recommend_questions(
        runtime, alice, limit=3, focus_doc_ids=[hr_doc]
    )
    questions = [r["question"] for r in focused]
    assert questions[0] == "年假怎么算？"
    assert "考勤怎么打卡？" in questions  # 凑满 limit 时仍可回填全局可见池


async def test_recommend_focus_drops_invisible_docs(seeded) -> None:
    """focus 里塞不可见文档 id 也不能越权出现。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))

    hr_doc = _doc_id(runtime, title="年假管理制度")
    fin_doc = _doc_id(runtime, title="财务保密制度")
    upsert_doc_recommend(runtime, doc_id=hr_doc, question="年假怎么算？", rerank_score=0.8)
    upsert_doc_recommend(runtime, doc_id=fin_doc, question="财务章谁管？", rerank_score=0.9)

    recs = recommend_questions(
        runtime, alice, limit=5, focus_doc_ids=[fin_doc, hr_doc]
    )
    questions = [r["question"] for r in recs]
    assert "年假怎么算？" in questions
    assert "财务章谁管？" not in questions


async def test_recommend_dedupes_current_query(seeded) -> None:
    """与当前问句相同的推荐要剔除。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))

    hr_doc = _doc_id(runtime, title="年假管理制度")
    upsert_doc_recommend(runtime, doc_id=hr_doc, question="年假怎么算？", rerank_score=0.8)

    recs = recommend_questions(
        runtime,
        alice,
        limit=3,
        query="年假怎么算？",
        focus_doc_ids=[hr_doc],
    )
    questions = [r["question"] for r in recs]
    assert "年假怎么算？" not in questions
