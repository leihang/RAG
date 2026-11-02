"""推荐问答三档：文档代表问句(3) > 热门问答(2) > 原逻辑种子/标题(1)。

成本与命中保证：
* tier-3 文档代表问句：入库时 LLM 生成 1 条、完整检索（rerank ≥ 阈值且命中本文档）验证、
  人工确认后存 ``ekbs_doc_recommend``，推荐时查表零成本；
* tier-2 热门问答：从 ``ekbs_qa_message`` 聚合权限内、refused=0、近 90 天 ≥2 次的真实问题；
* tier-1 原逻辑：``ekbs.discovery.suggested_questions``（种子库 + 标题派生）兜底。
"""

from __future__ import annotations

from typing import Any

from ekbs.discovery import list_visible_published_docs, suggested_questions
from ekbs.permission.context import PermContext

#: 热门问答的默认窗口与最小频次
HOT_DAYS = 90
HOT_MIN_COUNT = 2


def hot_questions(
    runtime: Any,
    perm: PermContext,
    *,
    days: int = HOT_DAYS,
    min_count: int = HOT_MIN_COUNT,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """权限内热门问题：近 ``days`` 天、refused=0、出现 ≥ ``min_count`` 次的用户问题。

    权限口径：该问题的历史引用（``ekbs_qa_citation`` → chunk → shard）必须落在用户可见分片内，
    避免把「你看不到的文档」的热门问题推给你。
    """
    if not perm.visible_shard_ids:
        return []
    placeholders = ",".join("?" for _ in perm.visible_shard_ids)
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT m.question AS question, COUNT(*) AS cnt, MAX(m.created_at) AS last_at "
            "FROM ekbs_qa_message m "
            "JOIN ekbs_qa_citation qc ON qc.message_id = m.message_id "
            "JOIN ekbs_chunk c ON c.chunk_id = qc.chunk_id "
            f"WHERE m.role = 'user' AND m.refused = 0 AND c.shard_id IN ({placeholders}) "
            "AND m.question IS NOT NULL AND TRIM(m.question) <> '' "
            "AND m.created_at >= datetime('now', ?) "
            "GROUP BY m.question HAVING COUNT(*) >= ? "
            "ORDER BY cnt DESC, last_at DESC LIMIT ?",
            (*perm.visible_shard_ids, f"-{days} days", min_count, limit),
        ).fetchall()
    return [
        {"question": str(r["question"]).strip(), "source": "hot", "count": int(r["cnt"])}
        for r in rows
    ]


def upsert_doc_recommend(
    runtime: Any,
    *,
    doc_id: int,
    question: str,
    rerank_score: float | None = None,
    status: str = "verified",
    operator_id: int | None = None,
) -> None:
    """写入/覆盖一条文档代表问句（tier-3）。"""
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_doc_recommend(doc_id, question, rerank_score, status, created_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(doc_id) DO UPDATE SET question = excluded.question, "
            "rerank_score = excluded.rerank_score, status = excluded.status, updated_at = datetime('now')",
            (doc_id, question, rerank_score, status, operator_id),
        )
        conn.commit()


def doc_recommend_questions(
    runtime: Any, perm: PermContext, *, limit: int = 10
) -> list[dict[str, Any]]:
    """用户可见文档的已验证代表问句（tier-3）。"""
    docs = list_visible_published_docs(runtime, perm, limit=100)
    doc_ids = [int(d["doc_id"]) for d in docs]
    if not doc_ids:
        return []
    placeholders = ",".join("?" for _ in doc_ids)
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT r.doc_id, r.question, r.rerank_score, d.title AS doc_title "
            "FROM ekbs_doc_recommend r JOIN ekbs_document d ON d.doc_id = r.doc_id "
            f"WHERE r.status = 'verified' AND r.doc_id IN ({placeholders}) "
            "ORDER BY d.updated_at DESC LIMIT ?",
            (*doc_ids, limit),
        ).fetchall()
    return [
        {
            "question": str(r["question"]).strip(),
            "source": "doc",
            "doc_id": int(r["doc_id"]),
            "doc_title": str(r["doc_title"] or ""),
            "rerank_score": r["rerank_score"],
        }
        for r in rows
    ]


async def generate_doc_question(runtime: Any, *, title: str, summary: str) -> str | None:
    """LLM 按标题+摘要生成一条口语化代表问句（仅生成，不验证）。"""
    messages = [
        {
            "role": "system",
            "content": (
                "你是知识库导览助手。给一篇文档生成一条最可能被用户问到、且能靠这篇文档回答的代表问题。"
                "只输出问题本身，不要解释、不要加引号。"
            ),
        },
        {"role": "user", "content": f"文档标题：{title}\n文档摘要：{summary}"},
    ]
    try:
        result = await runtime.providers.llm.generate(messages)
    except Exception:
        return None
    text = (result.text or "").strip()
    return text or None


async def verify_doc_question(
    runtime: Any, perm: PermContext, *, question: str, doc_id: int
) -> float | None:
    """完整检索验证：rerank 分数 ≥ 阈值且命中该文档才返回分数，否则 None。"""
    result = await runtime.pipeline.retrieve(question, perm)
    threshold = runtime.settings.retrieval.rerank.min_score
    for chunk in result.chunks:
        if (
            chunk.doc_id == doc_id
            and chunk.rerank_score is not None
            and chunk.rerank_score >= threshold
        ):
            return float(chunk.rerank_score)
    return None


def recommend_questions(
    runtime: Any,
    perm: PermContext,
    *,
    query: str = "",
    limit: int = 3,
    for_refusal: bool = False,
) -> list[dict[str, Any]]:
    """三档合并：文档代表问句(3) > 热门问答(2) > 原逻辑(1)。"""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(item: dict[str, Any]) -> None:
        q = str(item.get("question") or "").strip()
        if q and q not in seen:
            seen.add(q)
            out.append(item)

    for item in doc_recommend_questions(runtime, perm, limit=limit):
        add(item)
        if len(out) >= limit:
            return out[:limit]
    for item in hot_questions(runtime, perm, limit=limit):
        add(item)
        if len(out) >= limit:
            return out[:limit]
    for item in suggested_questions(
        runtime, perm, limit=limit, query=query, for_refusal=for_refusal
    ):
        add(item)
        if len(out) >= limit:
            return out[:limit]
    return out[:limit]
