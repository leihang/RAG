"""推荐问答三档：文档代表问句(3) > 热门问答(2) > 原逻辑种子/标题(1)。

成本与命中保证：
* tier-3 文档代表问句：入库时 LLM 生成 1 条、完整检索（rerank ≥ 阈值且命中本文档）验证、
  人工确认后存 ``ekbs_doc_recommend``，推荐时查表零成本；
* tier-2 热门问答：从 ``ekbs_qa_message`` 聚合权限内、refused=0、近 90 天 ≥2 次的真实问题；
* tier-1 原逻辑：``ekbs.discovery.suggested_questions``（种子库 + 标题派生）兜底。
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from ekbs.discovery import list_visible_published_docs, suggested_questions
from ekbs.logging import get_logger
from ekbs.permission.context import PermContext

log = get_logger(component="recommend")

#: 热门问答的默认窗口与最小频次
HOT_DAYS = 90
HOT_MIN_COUNT = 2


def _normalize_question(text: str) -> str:
    """推荐去重用：去空白与常见标点，大小写不敏感。"""
    q = (text or "").strip().lower()
    q = re.sub(r"[\s\?？!！。．，,、；;：:\"'“”‘’]+", "", q)
    return q


def hot_questions(
    runtime: Any,
    perm: PermContext,
    *,
    days: int = HOT_DAYS,
    min_count: int = HOT_MIN_COUNT,
    limit: int = 10,
    focus_doc_ids: Sequence[int] | None = None,
) -> list[dict[str, Any]]:
    """权限内热门问题：近 ``days`` 天、refused=0、出现 ≥ ``min_count`` 次的用户问题。

    落库口径：``AuditService.record_turn`` 把问题写在 ``role='assistant'`` 行上，
    引用也挂在同一 ``message_id``（并不写单独的 user 行）。热点必须按 assistant 聚合，
    否则库里永远匹配不到、推荐只会落到种子/代表问。

    权限口径：该问题的历史引用（``ekbs_qa_citation`` → chunk → shard）必须落在用户可见分片内，
    避免把「你看不到的文档」的热门问题推给你。
    ``focus_doc_ids``：若给定，只保留引用命中这些文档的热门问（仍过可见分片）。
    """
    if not perm.visible_shard_ids:
        return []
    focus = [int(d) for d in (focus_doc_ids or ()) if int(d) > 0]
    shard_ph = ",".join("?" for _ in perm.visible_shard_ids)
    sql = (
        "SELECT m.question AS question, COUNT(*) AS cnt, MAX(m.created_at) AS last_at "
        "FROM ekbs_qa_message m "
        "JOIN ekbs_qa_citation qc ON qc.message_id = m.message_id "
        "JOIN ekbs_chunk c ON c.chunk_id = qc.chunk_id "
        f"WHERE m.role = 'assistant' AND m.refused = 0 AND c.shard_id IN ({shard_ph}) "
        "AND m.question IS NOT NULL AND TRIM(m.question) <> '' "
        "AND m.created_at >= datetime('now', ?) "
    )
    params: list[Any] = [*perm.visible_shard_ids, f"-{days} days"]
    if focus:
        doc_ph = ",".join("?" for _ in focus)
        sql += f"AND qc.doc_id IN ({doc_ph}) "
        params.extend(focus)
    sql += "GROUP BY m.question HAVING COUNT(*) >= ? ORDER BY cnt DESC, last_at DESC LIMIT ?"
    params.extend([min_count, limit])
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(sql, params).fetchall()
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
    runtime: Any,
    perm: PermContext,
    *,
    limit: int = 10,
    focus_doc_ids: Sequence[int] | None = None,
) -> list[dict[str, Any]]:
    """用户可见文档的已验证代表问句（tier-3）。

    ``focus_doc_ids``：若给定，只取可见文档与这些 id 的交集（越权 id 自动丢掉）。
    """
    docs = list_visible_published_docs(runtime, perm, limit=100)
    visible = [int(d["doc_id"]) for d in docs]
    if not visible:
        return []
    if focus_doc_ids is not None:
        focus = {int(d) for d in focus_doc_ids if int(d) > 0}
        doc_ids = [d for d in visible if d in focus]
    else:
        doc_ids = visible
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


def _doc_summary_for_recommend(runtime: Any, doc_id: int, title: str) -> str:
    """取文档前若干叶子正文作代表问句上下文（与 parse_tasks 摘要口径同级）。"""
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT GROUP_CONCAT(substr(content, 1, 200), '\n') AS s FROM ("
            "SELECT content FROM ekbs_chunk WHERE doc_id = ? "
            "AND parent_id IS NOT NULL ORDER BY seq LIMIT 8)",
            (doc_id,),
        ).fetchone()
    text = (str(row["s"]).strip() if row and row["s"] else "") or title
    return text[:1200]


def _perm_for_doc(runtime: Any, doc_id: int) -> PermContext | None:
    """用文档自身分片构造可见权限，避免入库自动验证时被操作者 ACL 挡住。"""
    with runtime.db.sync_connect() as conn:
        shards = [
            str(r[0])
            for r in conn.execute(
                "SELECT DISTINCT shard_id FROM ekbs_chunk WHERE doc_id = ? AND shard_id IS NOT NULL",
                (doc_id,),
            ).fetchall()
            if r[0]
        ]
        kb_row = conn.execute(
            "SELECT kb_id FROM ekbs_document WHERE doc_id = ?", (doc_id,)
        ).fetchone()
    if not shards or kb_row is None:
        return None
    return PermContext(
        user_id=0,
        dingtalk_userid="system:doc_recommend",
        clearance=4,
        dept_chain=(),
        visible_shard_ids=tuple(shards),
        visible_kb_ids=(int(kb_row["kb_id"]),),
        policy_version=0,
        acl_allow_shards=frozenset(shards),
    )


async def ensure_doc_recommend_after_publish(
    runtime: Any,
    doc_id: int,
    *,
    operator_id: int | None = None,
) -> dict[str, Any]:
    """发布后自动生成并验证代表问句（tier-3）；已 verified 则跳过。

    电子表格等免复核文档没有复核页入口，必须在入库成功后补这一步，
    否则推荐问答会退回「「标题」主要讲了什么？」弱兜底。
    """
    qa_cfg = getattr(runtime.settings, "qa", None) or {}
    if isinstance(qa_cfg, dict):
        enabled = bool(qa_cfg.get("auto_doc_recommend", True))
    else:
        enabled = bool(getattr(qa_cfg, "auto_doc_recommend", True))
    if not enabled:
        return {"doc_id": doc_id, "skipped": True, "reason": "disabled"}

    with runtime.db.sync_connect() as conn:
        doc = conn.execute(
            "SELECT doc_id, title, status FROM ekbs_document WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
        existing = conn.execute(
            "SELECT question, status, rerank_score FROM ekbs_doc_recommend WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
    if doc is None or str(doc["status"]) != "published":
        return {"doc_id": doc_id, "skipped": True, "reason": "not_published"}
    if existing is not None and str(existing["status"]) == "verified":
        return {
            "doc_id": doc_id,
            "skipped": True,
            "reason": "already_verified",
            "question": str(existing["question"]),
        }

    title = str(doc["title"] or f"doc-{doc_id}")
    question = (
        str(existing["question"]).strip()
        if existing is not None and str(existing["question"] or "").strip()
        else ""
    )
    if not question:
        question = await generate_doc_question(
            runtime, title=title, summary=_doc_summary_for_recommend(runtime, doc_id, title)
        ) or ""
    if not question:
        return {"doc_id": doc_id, "ok": False, "reason": "generate_failed"}

    upsert_doc_recommend(
        runtime,
        doc_id=doc_id,
        question=question,
        status="pending",
        operator_id=operator_id,
    )
    perm = _perm_for_doc(runtime, doc_id)
    if perm is None:
        return {
            "doc_id": doc_id,
            "ok": False,
            "reason": "no_shard",
            "question": question,
            "status": "pending",
        }
    score = await verify_doc_question(runtime, perm, question=question, doc_id=doc_id)
    status = "verified" if score is not None else "rejected"
    upsert_doc_recommend(
        runtime,
        doc_id=doc_id,
        question=question,
        rerank_score=score,
        status=status,
        operator_id=operator_id,
    )
    log.info(
        "doc_recommend_ensured",
        doc_id=doc_id,
        status=status,
        rerank_score=score,
        question_len=len(question),
    )
    return {
        "doc_id": doc_id,
        "ok": score is not None,
        "question": question,
        "status": status,
        "rerank_score": score,
    }


def _extend_recommendations(
    out: list[dict[str, Any]],
    seen: set[str],
    items: Sequence[dict[str, Any]],
    *,
    query_norm: str,
    limit: int,
) -> bool:
    """把 ``items`` 去重追加进 ``out``；已满返回 True。"""
    for item in items:
        q = str(item.get("question") or "").strip()
        if not q or q in seen:
            continue
        if query_norm and _normalize_question(q) == query_norm:
            continue
        seen.add(q)
        out.append(item)
        if len(out) >= limit:
            return True
    return False


def recommend_questions(
    runtime: Any,
    perm: PermContext,
    *,
    query: str = "",
    limit: int = 3,
    for_refusal: bool = False,
    focus_doc_ids: Sequence[int] | None = None,
) -> list[dict[str, Any]]:
    """三档合并：文档代表问句(3) > 热门问答(2) > 原逻辑(1)。

    ``focus_doc_ids``：本轮引用文档。成功作答时优先抽这些文档上的代表问/热门，
    再回填全局 ACL 池；与 ``query`` 归一化相同的问句剔除。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    query_norm = _normalize_question(query)
    focus = [int(d) for d in (focus_doc_ids or ()) if int(d) > 0]

    # 本轮引用优先（仍过可见文档交集，越权 id 不会进结果）
    if focus and _extend_recommendations(
        out,
        seen,
        doc_recommend_questions(runtime, perm, limit=limit, focus_doc_ids=focus),
        query_norm=query_norm,
        limit=limit,
    ):
        return out[:limit]
    if focus and _extend_recommendations(
        out,
        seen,
        hot_questions(runtime, perm, limit=limit, focus_doc_ids=focus),
        query_norm=query_norm,
        limit=limit,
    ):
        return out[:limit]

    if _extend_recommendations(
        out,
        seen,
        doc_recommend_questions(runtime, perm, limit=limit),
        query_norm=query_norm,
        limit=limit,
    ):
        return out[:limit]
    if _extend_recommendations(
        out,
        seen,
        hot_questions(runtime, perm, limit=limit),
        query_norm=query_norm,
        limit=limit,
    ):
        return out[:limit]
    _extend_recommendations(
        out,
        seen,
        suggested_questions(
            runtime, perm, limit=limit, query=query, for_refusal=for_refusal
        ),
        query_norm=query_norm,
        limit=limit,
    )
    return out[:limit]
