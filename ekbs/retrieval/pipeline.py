"""检索与生成编排 —— `00` §2.1/§2.2 的链路落地。

链路顺序（不可调换）：

.. code-block:: text

    查询嵌入 → 逐分片 FAISS 召回 ┐
                                 ├→ RRF(k=60) → rerank(15→5, ≥0.30) → C5 闸门
    FTS5/BM25 召回（权限下推）  ┘        → 父块扩展 → 再过一次 C5 → 生成 → 引用后置校验

三处必须遵守的硬约束：

* **C1**：无权分片根本不参与计算 —— 召回只在 ``perm.visible_shard_ids`` 上做；
* **C5**：进 Prompt 前逐候选过文档级 ACL；被丢弃的内容**不得**进入上下文、答案缓存或日志正文；
  父块扩展后的父块**仍需过闸**（`00` C5 + §2.1）；
* **嵌入不可用即拒答**：查询期嵌入失败时，命中缓存则继续，否则**明确拒答**，
  **禁止**降级为「只走 FTS5 假装语义检索」（`00` §2.3）。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from ekbs.cache.store import CacheStore
from ekbs.config import Settings
from ekbs.db import Database
from ekbs.errors import EmbeddingUnavailable, ModelUnavailable, RerankUnavailable
from ekbs.index.faiss_store import ShardIndexManager
from ekbs.index.fts import FtsIndex
from ekbs.ingest.chunker import retrieval_text
from ekbs.logging import get_logger
from ekbs.permission.acl import DocumentGate
from ekbs.permission.context import PermContext
from ekbs.provider.base import LLMProvider, RerankDoc
from ekbs.provider.registry import Providers
from ekbs.retrieval.rrf import rrf_fuse, ranks_of

log = get_logger(component="retrieval")

#: 拒答原因（写进 ``ekbs_qa_message.refused`` 的旁证，供审计与评测分组）
REFUSE_NO_SHARD = "no_visible_shard"
REFUSE_EMBED_UNAVAILABLE = "embedding_unavailable"
REFUSE_NO_CANDIDATE = "no_candidate"
REFUSE_BELOW_THRESHOLD = "below_threshold"
REFUSE_ALL_DENIED = "all_denied_by_c5"
REFUSE_EMPTY_ANSWER = "empty_answer"
REFUSE_NO_BASIS = "no_basis_in_context"

#: 提示词约定的「资料不足」口径（`00` §2.1 要求「直接回复『未找到可靠依据』」）。
#: 模型可能带引号/前缀标点，故只在开头比对，不做全文包含判断 ——
#: 否则正文里正常引用这句话的答案会被误判成拒答。
NO_BASIS_MARKER = "未找到可靠依据"

_CITATION_RE = re.compile(r"\[(\d+)\]")


def _is_no_basis(text: str) -> bool:
    """判断生成结果是否为模型主动拒答（而非真的答了）。"""
    head = text.strip().lstrip("*#>-—・ \t\r\n「『\"'《")
    return head.startswith(NO_BASIS_MARKER)


@dataclass
class RetrievedChunk:
    """最终进入 Prompt 的块（引用卡片所需字段一并带上，见 `00` §2.2）。"""

    chunk_id: int
    doc_id: int
    shard_id: str
    content: str
    heading_path: str = ""
    page_from: int | None = None
    page_to: int | None = None
    chunk_type: str = "child"
    figure_id: str | None = None
    image_path: str | None = None
    doc_title: str = ""
    doc_version: str = "v1"
    parent_chunk_id: int | None = None
    dense_rank: int | None = None
    sparse_rank: int | None = None
    rrf_score: float = 0.0
    rerank_score: float | None = None

    @property
    def is_visual(self) -> bool:
        return self.chunk_type in {"figure", "table"}


@dataclass
class RetrievalResult:
    query: str
    chunks: list[RetrievedChunk] = field(default_factory=list)
    candidate_count: int = 0
    c5_dropped: int = 0
    top_score: float | None = None
    refused: bool = False
    refusal_reason: str = ""
    degraded: list[str] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)
    #: 调试台用的分阶段明细（仅在 collect_debug=True 时填充；正常问答不产生这份数据）
    stage_debug: dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return bool(self.chunks) and not self.refused


@dataclass
class Citation:
    """落库到 ``ekbs_qa_citation`` 的引用（只存 hash 与页码，不存片段全文）。"""

    cite_index: int
    chunk_id: int
    doc_id: int
    doc_title: str
    doc_version: str
    heading_path: str
    page_from: int | None
    page_to: int | None
    rerank_score: float | None
    acl_result: str
    page_check: str
    figure_id: str | None = None
    image_path: str | None = None


@dataclass
class AnswerResult:
    answer: str
    citations: list[Citation]
    retrieval: RetrievalResult
    model: str
    #: 查询改写结果（原始问题 → 实际检索用的查询）；审计要存两份（01 §5.4 的 rewritten_query）
    rewritten_query: str = ""
    history_used: bool = False
    #: 降级痕迹（如 rerank_skipped / generation_skipped_by_content_guard），供审计与告警
    degraded: list[str] = field(default_factory=list)
    refused: bool = False
    refusal_reason: str = ""
    hallucinated_citations: list[int] = field(default_factory=list)
    ttft_ms: int = 0
    total_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(frozen=True)
class RetrievalOverrides:
    """检索参数覆盖（检索调试台用）。

    **为什么不直接改 ``settings.retrieval``**：配置是进程级单例，``uvicorn --workers 1``
    下仍有并发协程 —— 按请求临时改全局配置会串到别的请求上。因此覆盖值沿调用链传递，
    默认 ``None`` 表示"用配置里的值"。
    """

    dense_top_k: int | None = None
    sparse_top_k: int | None = None
    rrf_k: int | None = None
    rerank_enabled: bool | None = None
    rerank_top_n: int | None = None
    rerank_keep: int | None = None
    rerank_min_score: float | None = None
    parent_expand: bool | None = None

    def effective(self, settings: Settings) -> dict[str, Any]:
        cfg = settings.retrieval
        return {
            "dense_top_k": self.dense_top_k or cfg.dense_top_k,
            "sparse_top_k": self.sparse_top_k or cfg.sparse_top_k,
            "rrf_k": self.rrf_k or cfg.rrf_k,
            "rerank_enabled": cfg.rerank.enabled if self.rerank_enabled is None else self.rerank_enabled,
            "rerank_top_n": self.rerank_top_n or cfg.rerank.top_n,
            "rerank_keep": self.rerank_keep or cfg.rerank.keep,
            "rerank_min_score": (
                cfg.rerank.min_score if self.rerank_min_score is None else self.rerank_min_score
            ),
            "parent_expand": cfg.parent_expand if self.parent_expand is None else self.parent_expand,
        }


SYSTEM_PROMPT = (
    "你是企业知识库问答助手。只能依据下面提供的资料回答，不得编造。\n"
    "引用资料时必须在句末标注编号，格式如 [1]、[2]。\n"
    "若资料不足以回答，直接回复「未找到可靠依据」。\n\n"
    "资料：\n{context}"
)

#: 提示词版本（进答案缓存键）：**改动 SYSTEM_PROMPT 必须同步 +1**，
#: 否则旧提示词生成的答案会继续从缓存里被服务。
PROMPT_REVISION = "v1"


class RetrievalPipeline:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        providers: Providers,
        cache: CacheStore,
        gate: DocumentGate,
        shard_manager: ShardIndexManager,
        fts: FtsIndex | None = None,
        session_store: Any | None = None,
        rewriter: Any | None = None,
        content_guard: Any | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.providers = providers
        self.cache = cache
        self.gate = gate
        self.shards = shard_manager
        self.fts = fts or FtsIndex(db)
        # 多轮会话与查询改写（00 §2.1）；为 None 时退化为单轮行为
        self.sessions = session_store
        self.rewriter = rewriter
        # 内容安全闸门（04 §6.4）：L4 输出侧检测未授权文档名
        self.content_guard = content_guard

    # ------------------------------------------------------------------
    # 查询嵌入（含缓存与「不可用即拒答」）
    # ------------------------------------------------------------------

    async def query_vector(self, query: str) -> list[float] | None:
        """返回查询向量；None 表示嵌入不可用（调用方必须拒答，不得降级）。"""
        key = CacheStore.embed_key(query)
        cached = await self.cache.get(key)
        if cached is not None:
            return list(cached)
        try:
            vector = await self.providers.embed.embed_one(query)
        except EmbeddingUnavailable:
            raise
        except Exception as exc:  # 云端超时/限流等一并以「嵌入不可用」处理
            log.error("embed_failed", error=str(exc))
            return None
        await self.cache.set(
            key, vector, ttl=self.settings.retrieval.cache.query_embedding_ttl
        )
        return vector

    # ------------------------------------------------------------------
    # 召回
    # ------------------------------------------------------------------

    def _dense_recall(
        self, vector: Sequence[float], perm: PermContext, top_k: int | None = None
    ) -> list[int]:
        """逐分片 FAISS 召回（C1：只查可见分片；顺序稳定以便复现）。"""
        import numpy as np

        query = np.asarray(vector, dtype="float32")
        per_shard: list[tuple[float, int]] = []
        for shard_id in perm.visible_shard_ids:  # 已是 sorted()
            hits = self.shards.search(shard_id, query, top_k or self.settings.retrieval.dense_top_k)
            if not hits:
                continue
            mapping = self._chunk_ids_by_faiss_row(shard_id, [row for row, _ in hits])
            for row, score in hits:
                chunk_id = mapping.get(row)
                if chunk_id is not None:
                    per_shard.append((score, chunk_id))
        per_shard.sort(key=lambda pair: (-pair[0], pair[1]))
        return [chunk_id for _, chunk_id in per_shard]

    def _chunk_ids_by_faiss_row(self, shard_id: str, rows: Sequence[int]) -> dict[int, int]:
        """``(shard_id, faiss_row) → chunk_id``；同时做**权限与状态下推**（与 FTS5 路径语义等价）。"""
        if not rows:
            return {}
        placeholders = ",".join("?" for _ in rows)
        sql = (
            "SELECT c.faiss_row AS faiss_row, c.chunk_id AS chunk_id "
            "FROM ekbs_chunk c JOIN ekbs_document d ON d.doc_id = c.doc_id "
            "WHERE c.shard_id = ? AND c.faiss_row IN (" + placeholders + ") "
            "AND d.status = 'published' "
            "AND (d.effective_date IS NULL OR d.effective_date <= date('now')) "
            "AND (d.expire_date IS NULL OR d.expire_date > date('now'))"
        )
        with self.db.sync_connect() as conn:
            result = conn.execute(sql, (shard_id, *[int(r) for r in rows])).fetchall()
        return {int(r["faiss_row"]): int(r["chunk_id"]) for r in result}

    def _sparse_recall(
        self, query: str, perm: PermContext, top_k: int | None = None
    ) -> list[int]:
        hits = self.fts.search(
            query,
            shard_ids=perm.visible_shard_ids,
            clearance=perm.clearance,
            dept_chain=perm.dept_chain,
            top_k=top_k or self.settings.retrieval.sparse_top_k,
        )
        return [hit.chunk_id for hit in hits]

    # ------------------------------------------------------------------
    # 块加载
    # ------------------------------------------------------------------

    def load_chunks(self, chunk_ids: Sequence[int]) -> dict[int, RetrievedChunk]:
        if not chunk_ids:
            return {}
        placeholders = ",".join("?" for _ in chunk_ids)
        sql = (
            "SELECT c.chunk_id, c.doc_id, c.parent_id, c.chunk_type, c.content, c.heading_path, "
            "       c.page_from, c.page_to, c.figure_id, c.image_path, c.shard_id, "
            "       d.title AS doc_title, d.doc_version "
            "FROM ekbs_chunk c JOIN ekbs_document d ON d.doc_id = c.doc_id "
            f"WHERE c.chunk_id IN ({placeholders})"
        )
        with self.db.sync_connect() as conn:
            rows = conn.execute(sql, tuple(int(c) for c in chunk_ids)).fetchall()
        result: dict[int, RetrievedChunk] = {}
        for row in rows:
            result[int(row["chunk_id"])] = RetrievedChunk(
                chunk_id=int(row["chunk_id"]),
                doc_id=int(row["doc_id"]),
                shard_id=str(row["shard_id"]),
                content=str(row["content"]),
                heading_path=str(row["heading_path"] or ""),
                page_from=row["page_from"],
                page_to=row["page_to"],
                chunk_type=str(row["chunk_type"]),
                figure_id=row["figure_id"],
                image_path=row["image_path"],
                doc_title=str(row["doc_title"]),
                doc_version=str(row["doc_version"]),
                parent_chunk_id=row["parent_id"],
            )
        return result

    # ------------------------------------------------------------------
    # 检索主流程
    # ------------------------------------------------------------------

    async def retrieve(
        self,
        query: str,
        perm: PermContext,
        *,
        overrides: RetrievalOverrides | None = None,
        collect_debug: bool = False,
    ) -> RetrievalResult:
        """检索主流程。

        ``overrides`` 供检索调试台按请求调参（不改全局配置）；``collect_debug=True``
        会额外收集分阶段明细（各分片召回、FTS 命中、RRF 排名、rerank 分数、C5 结果），
        正常问答路径不产生这份开销。
        """
        params = (overrides or RetrievalOverrides()).effective(self.settings)
        started = time.perf_counter()
        result = RetrievalResult(query=query)
        if collect_debug:
            result.stage_debug["params"] = dict(params)

        if not perm.visible_shard_ids:
            result.refused = True
            result.refusal_reason = REFUSE_NO_SHARD
            result.timings_ms["total"] = _ms(started)
            return result

        # ① 查询嵌入（C1 的分片集合 + 嵌入不可用即拒答）
        try:
            vector = await self.query_vector(query)
        except EmbeddingUnavailable:
            vector = None
        if vector is None:
            result.refused = True
            result.refusal_reason = REFUSE_EMBED_UNAVAILABLE
            result.timings_ms["total"] = _ms(started)
            log.warning("embedding_unavailable_refuse", query_len=len(query))
            return result

        embed_done = time.perf_counter()

        # ② 两路召回（FAISS 是同步 C 调用，放线程池；04 §1.5）
        dense_ids = await asyncio.to_thread(
            self._dense_recall, vector, perm, params["dense_top_k"]
        )
        sparse_ids = await asyncio.to_thread(
            self._sparse_recall, query, perm, params["sparse_top_k"]
        )
        recall_done = time.perf_counter()
        if collect_debug:
            import numpy as np  # 本模块按需导入 numpy（不在模块顶层引入）

            result.stage_debug["dense"] = {
                "requested_top_k": params["dense_top_k"],
                "per_shard": {
                    shard_id: [
                        {"faiss_row": row, "score": round(score, 6)}
                        for row, score in self.shards.search(
                            shard_id, np.asarray(vector, dtype="float32"), params["dense_top_k"]
                        )
                    ]
                    for shard_id in perm.visible_shard_ids
                },
                "merged_chunk_ids": dense_ids,
            }
            result.stage_debug["sparse"] = {
                "requested_top_k": params["sparse_top_k"],
                "hits": [
                    {"chunk_id": hit.chunk_id, "bm25": round(hit.score, 6), "shard_id": hit.shard_id}
                    for hit in self.fts.search(
                        query, shard_ids=perm.visible_shard_ids, clearance=perm.clearance,
                        dept_chain=perm.dept_chain, top_k=params["sparse_top_k"],
                    )
                ],
            }

        # ③ RRF
        fused = rrf_fuse([dense_ids, sparse_ids], k=params["rrf_k"])
        dense_ranks = ranks_of(dense_ids)
        sparse_ranks = ranks_of(sparse_ids)
        if not fused:
            result.refused = True
            result.refusal_reason = REFUSE_NO_CANDIDATE
            result.timings_ms.update(
                embed=_ms(started, embed_done),
                recall=_ms(embed_done, recall_done),
                total=_ms(started),
            )
            return result

        rerank_cfg = self.settings.retrieval.rerank
        rerank_enabled = bool(params["rerank_enabled"]) and self.providers.rerank is not None
        shortlist = [chunk_id for chunk_id, _ in fused[: params["rerank_top_n"]]]
        result.candidate_count = len(shortlist)
        ranked: list[RetrievedChunk] = []

        if rerank_enabled:
            docs = self.load_chunks(shortlist)
            payload = [
                RerankDoc(
                    chunk_id=chunk_id,
                    # 与嵌入同口径：带上小节标题，精排才看得见「第二条 住宿标准」这类限定词
                    text=retrieval_text(docs[chunk_id].heading_path, docs[chunk_id].content),
                    shard_id=docs[chunk_id].shard_id,
                    rrf_score=score,
                    dense_rank=dense_ranks.get(chunk_id),
                    sparse_rank=sparse_ranks.get(chunk_id),
                )
                for chunk_id, score in fused[: params["rerank_top_n"]]
                if chunk_id in docs
            ]
            try:
                reranked = await self.providers.rerank.rerank(
                    query, payload, params["rerank_top_n"]
                )
            except (RerankUnavailable, ModelUnavailable) as exc:
                # 00 §2.3：跳过精排，直接取 RRF Top-5（仍须过 C5）
                log.warning("rerank_unavailable_degrade", error=str(exc))
                result.degraded.append("rerank_skipped")
                reranked = []
            score_map = {item.chunk_id: item.score for item in reranked}
            if score_map:
                for chunk_id, rrf_score in fused[: params["rerank_top_n"]]:
                    chunk = docs.get(chunk_id)
                    if chunk is None:
                        continue
                    chunk.rrf_score = rrf_score
                    chunk.dense_rank = dense_ranks.get(chunk_id)
                    chunk.sparse_rank = sparse_ranks.get(chunk_id)
                    chunk.rerank_score = score_map.get(chunk_id)
                    ranked.append(chunk)
                ranked.sort(key=lambda c: (-(c.rerank_score or 0.0), c.chunk_id))
            else:
                result.degraded.append("rerank_fallback_rrf")
                for chunk_id, rrf_score in fused[: params["rerank_keep"]]:
                    chunk = docs.get(chunk_id)
                    if chunk is None:
                        continue
                    chunk.rrf_score = rrf_score
                    chunk.dense_rank = dense_ranks.get(chunk_id)
                    chunk.sparse_rank = sparse_ranks.get(chunk_id)
                    ranked.append(chunk)
        else:
            # rerank 关闭：直接取 RRF Top-keep
            docs = self.load_chunks(shortlist[: params["rerank_keep"]])
            for chunk_id, rrf_score in fused[: params["rerank_keep"]]:
                chunk = docs.get(chunk_id)
                if chunk is None:
                    continue
                chunk.rrf_score = rrf_score
                chunk.dense_rank = dense_ranks.get(chunk_id)
                chunk.sparse_rank = sparse_ranks.get(chunk_id)
                ranked.append(chunk)

        rerank_done = time.perf_counter()

        # ④ C5 闸门（在 rerank 之后、进 Prompt 之前；被丢弃者不得进入任何下游）
        kept, dropped = await asyncio.to_thread(
            self.gate.filter_chunks, perm, [(c.chunk_id, c.doc_id) for c in ranked]
        )
        dropped_ids = {chunk_id for chunk_id, _ in dropped}
        result.c5_dropped = len(dropped)
        survivors = [c for c in ranked if c.chunk_id not in dropped_ids]

        # 阈值判定：以**幸存候选**的最高分为准（否则会出现「最高分被 C5 拦掉却仍回答」）
        if survivors:
            result.top_score = max(
                (c.rerank_score for c in survivors if c.rerank_score is not None),
                default=None,
            )
        if not survivors:
            result.refused = True
            result.refusal_reason = REFUSE_ALL_DENIED if dropped else REFUSE_NO_CANDIDATE
            result.timings_ms.update(
                embed=_ms(started, embed_done),
                recall=_ms(embed_done, recall_done),
                rerank=_ms(recall_done, rerank_done),
                total=_ms(started),
            )
            return result

        if collect_debug:
            result.stage_debug["rrf"] = {
                "k": params["rrf_k"],
                "fused": [
                    {"chunk_id": chunk_id, "score": round(score, 8)} for chunk_id, score in fused
                ],
            }
            result.stage_debug["rerank"] = {
                "enabled": rerank_enabled,
                "requested_top_n": params["rerank_top_n"],
                "keep": params["rerank_keep"],
                "min_score": params["rerank_min_score"],
                "scores": [
                    {"chunk_id": c.chunk_id, "rerank_score": c.rerank_score} for c in ranked
                ],
            }

        if (
            rerank_enabled
            and "rerank_skipped" not in result.degraded
            and result.top_score is not None
            and result.top_score < params["rerank_min_score"]
        ):
            result.refused = True
            result.refusal_reason = REFUSE_BELOW_THRESHOLD
            result.timings_ms.update(
                embed=_ms(started, embed_done),
                recall=_ms(embed_done, recall_done),
                rerank=_ms(recall_done, rerank_done),
                total=_ms(started),
            )
            return result

        # ⑤ 父块扩展（扩展后的父块**仍需过 C5**，00 §2.1 / C5）
        selected = survivors[: params["rerank_keep"]]
        if collect_debug:
            result.stage_debug["c5"] = {
                "kept": [chunk_id for chunk_id, _ in kept],
                "dropped": [chunk_id for chunk_id, _ in dropped],
            }
        if params["parent_expand"]:
            selected = await asyncio.to_thread(self._expand_parents, selected, perm)

        expand_done = time.perf_counter()
        result.chunks = selected
        result.timings_ms.update(
            embed=_ms(started, embed_done),
            recall=_ms(embed_done, recall_done),
            rerank=_ms(recall_done, rerank_done),
            expand=_ms(rerank_done, expand_done),
            total=_ms(started, expand_done),
        )
        return result

    def _expand_parents(
        self, chunks: list[RetrievedChunk], perm: PermContext
    ) -> list[RetrievedChunk]:
        """子块命中 → 用父块全文进上下文；父块仍需过 C5。"""
        parent_ids = [
            c.parent_chunk_id
            for c in chunks
            if c.chunk_type == "child" and c.parent_chunk_id
        ]
        if not parent_ids:
            return chunks
        parents = self.load_chunks([int(p) for p in parent_ids])
        kept, dropped = self.gate.filter_chunks(
            perm, [(pid, parents[pid].doc_id) for pid in parents]
        )
        allowed = {chunk_id for chunk_id, _ in kept}
        if dropped:
            log.info("c5_denied_parent", count=len(dropped), user_id=perm.user_id)
        expanded: list[RetrievedChunk] = []
        for chunk in chunks:
            parent = parents.get(int(chunk.parent_chunk_id)) if chunk.parent_chunk_id else None
            if parent is not None and parent.chunk_id in allowed:
                parent.rerank_score = chunk.rerank_score
                parent.rrf_score = chunk.rrf_score
                parent.dense_rank = chunk.dense_rank
                parent.sparse_rank = chunk.sparse_rank
                expanded.append(parent)
            else:
                expanded.append(chunk)
        return expanded

    # ------------------------------------------------------------------
    # 生成与引用
    # ------------------------------------------------------------------

    @staticmethod
    def build_context(chunks: Sequence[RetrievedChunk]) -> str:
        """构造 Prompt 里的资料段：``[n] 标题（第 x 页）: 正文``。

        编号 → chunk_id 的映射由调用方随请求保存（`00` §2.2）。
        """
        lines: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            page = ""
            if chunk.page_from:
                page = f"（第 {chunk.page_from} 页）" if chunk.page_from == chunk.page_to else (
                    f"（第 {chunk.page_from}-{chunk.page_to} 页）"
                )
            heading = f"{chunk.heading_path} " if chunk.heading_path else ""
            lines.append(f"[{index}] {chunk.doc_title}{page} {heading}: {chunk.content}".strip())
        return "\n".join(lines)

    async def generate(
        self,
        question: str,
        retrieval: RetrievalResult,
        perm: PermContext,
        *,
        history: Sequence[dict[str, str]] = (),
    ) -> AnswerResult:
        started = time.perf_counter()
        chunks = retrieval.chunks
        if not chunks:
            return AnswerResult(
                answer="未找到可靠依据。",
                citations=[],
                retrieval=retrieval,
                model=self.providers.llm.ref.ident,
                refused=True,
                refusal_reason=retrieval.refusal_reason or REFUSE_EMPTY_ANSWER,
            )

        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT.format(context=self.build_context(chunks))}
        ]
        messages.extend(dict(m) for m in history)
        messages.append({"role": "user", "content": question})

        llm_started = time.perf_counter()
        try:
            llm_result = await self.providers.llm.generate(messages)
        except ModelUnavailable as exc:
            log.error("llm_failed", error=str(exc))
            return AnswerResult(
                answer="生成服务暂时不可用，请稍后重试。",
                citations=[],
                retrieval=retrieval,
                model=self.providers.llm.ref.ident,
                refused=True,
                refusal_reason="llm_unavailable",
            )
        llm_done = time.perf_counter()

        answer_text, hallucinated = self._drop_hallucinated(llm_result.text, len(chunks))
        # L4 输出侧检测：答案里不得出现当前用户看不到的文档名（04 §6.4）
        answer_text, leaked_titles = self._apply_output_guard(answer_text, perm)
        citations = self._post_check_citations(answer_text, chunks, perm)
        if hallucinated:
            self.gate.record_security_event(
                "hallucinated_citation",
                user_id=perm.user_id,
                severity="medium",
                detail={"indices": hallucinated, "model": llm_result.model},
            )

        total = _ms(started)
        # 模型自己判定「资料不足」时，必须回 refused=True 且清空引用：
        # 否则接口出现 refused=false + 5 条引用 + 答案「未找到可靠依据」的自相矛盾状态，
        # 前端照 refused 渲染，会把"没答"显示成"答了并给了出处"（实测踩到）。
        if not answer_text.strip() or _is_no_basis(answer_text):
            self.gate.record_security_event(
                "no_basis_in_context",
                user_id=perm.user_id,
                severity="low",
                detail={"model": llm_result.model, "candidate_count": len(chunks)},
            )
            return AnswerResult(
                answer=answer_text.strip() or f"{NO_BASIS_MARKER}。",
                citations=[],
                retrieval=retrieval,
                model=llm_result.model,
                refused=True,
                refusal_reason=REFUSE_NO_BASIS,
                hallucinated_citations=hallucinated,
                ttft_ms=int(_ms(llm_started, llm_done)),
                total_ms=total,
                prompt_tokens=llm_result.prompt_tokens,
                completion_tokens=llm_result.completion_tokens,
            )

        return AnswerResult(
            answer=answer_text,
            citations=citations,
            retrieval=retrieval,
            model=llm_result.model,
            refused=False,
            hallucinated_citations=hallucinated,
            degraded=[f"output_title_masked:{len(leaked_titles)}"] if leaked_titles else [],
            ttft_ms=int(_ms(llm_started, llm_done)),
            total_ms=total,
            prompt_tokens=llm_result.prompt_tokens,
            completion_tokens=llm_result.completion_tokens,
        )

    def _retrieval_only_answer(
        self, question: str, retrieval: RetrievalResult, perm: PermContext
    ) -> AnswerResult:
        """纯检索答复（不调用 LLM）：只列出检索到的资料与可溯源的引用。"""
        chunks = retrieval.chunks
        if not chunks:
            return AnswerResult(
                answer="未找到可靠依据。", citations=[], retrieval=retrieval,
                model="retrieval-only", refused=True,
                refusal_reason=retrieval.refusal_reason or REFUSE_EMPTY_ANSWER,
            )
        lines = ["（内容安全策略：本次仅返回检索到的资料，未启用生成）"]
        for index, chunk in enumerate(chunks, start=1):
            page = f"第 {chunk.page_from} 页 " if chunk.page_from else ""
            lines.append(f"[{index}] {chunk.doc_title} {page}{chunk.heading_path}".strip())
        answer = "\n".join(lines)
        citations = self._post_check_citations(answer, chunks, perm)
        return AnswerResult(
            answer=answer, citations=citations, retrieval=retrieval,
            model="retrieval-only", refused=False,
            degraded=["generation_skipped_by_content_guard"],
        )

    def _apply_output_guard(
        self, answer: str, perm: PermContext
    ) -> tuple[str, list[str]]:
        """L4：答案里出现当前用户**看不到**的文档名 → 替换为「内容受保护」。"""
        if self.content_guard is None or not answer:
            return answer, []
        titles = self.gate.unauthorized_titles(perm)
        decision = self.content_guard.check_output(answer, unauthorized_titles=titles)
        if decision.action != "allow":
            self.gate.record_security_event(
                "output_title_leak",
                user_id=perm.user_id,
                severity="high",
                detail={"titles": decision.hits[:5], "reason": decision.reason},
            )
            return decision.text, list(decision.hits)
        return answer, []

    @staticmethod
    def _drop_hallucinated(answer: str, chunk_count: int) -> tuple[str, list[int]]:
        """引用编号不存在于 ``citation_map`` → 幻觉引用：剔除标记并留痕（`00` §2.2）。"""
        bad: list[int] = []
        for match in _CITATION_RE.finditer(answer or ""):
            index = int(match.group(1))
            if index < 1 or index > chunk_count:
                bad.append(index)
        if not bad:
            return answer, []
        cleaned = answer
        for index in sorted(set(bad)):
            cleaned = cleaned.replace(f"[{index}]", "")
        return cleaned, sorted(set(bad))

    def _post_check_citations(
        self, answer: str, chunks: Sequence[RetrievedChunk], perm: PermContext
    ) -> list[Citation]:
        """引用后置校验（`00` §2.2、`docs/spec/10` §8）：编号存在 + ACL 二次确认 + 页码区间。"""
        if not answer.strip():
            return []
        cited_indices = {int(m.group(1)) for m in _CITATION_RE.finditer(answer)}
        if not cited_indices:  # 没标引用时，把全部入围块作为可溯源依据返回
            cited_indices = set(range(1, len(chunks) + 1))
        allowed, dropped = self.gate.filter_chunks(
            perm, [(c.chunk_id, c.doc_id) for c in chunks]
        )
        allowed_ids = {chunk_id for chunk_id, _ in allowed}
        revoked = {chunk_id for chunk_id, _ in dropped}

        citations: list[Citation] = []
        for index, chunk in enumerate(chunks, start=1):
            if index not in cited_indices:
                continue
            if chunk.chunk_id in revoked:
                # 长对话期间权限被回收：删引用标记 + 记审计（03 §5.5）
                self.gate.record_security_event(
                    "citation_acl_revoked",
                    user_id=perm.user_id,
                    severity="high",
                    detail={"chunk_id": chunk.chunk_id, "doc_id": chunk.doc_id},
                )
                continue
            page_check = "pass"
            if chunk.page_from is not None and chunk.page_to is not None:
                page_check = "pass" if int(chunk.page_from) <= int(chunk.page_to) else "out_of_range"
            citations.append(
                Citation(
                    cite_index=index,
                    chunk_id=chunk.chunk_id,
                    doc_id=chunk.doc_id,
                    doc_title=chunk.doc_title,
                    doc_version=chunk.doc_version,
                    heading_path=chunk.heading_path,
                    page_from=chunk.page_from,
                    page_to=chunk.page_to,
                    rerank_score=chunk.rerank_score,
                    acl_result="pass" if chunk.chunk_id in allowed_ids else "revoked",
                    page_check=page_check,
                    figure_id=chunk.figure_id,
                    image_path=chunk.image_path,
                )
            )
        return citations

    async def answer(
        self,
        question: str,
        perm: PermContext,
        *,
        history: Sequence[dict[str, str]] = (),
        conversation_id: str | None = None,
        use_cache: bool = True,
        allow_generation: bool = True,
    ) -> AnswerResult:
        """检索 + 生成的一条龙（供 API 与钉钉适配器调用）。

        ``allow_generation=False``：内容安全闸门判定为「疑似提示词注入」时的降级路径 ——
        **仍然做检索并返回可溯源引用，但不调用 LLM**（`04` §6.4 的 L3 层）。
        

        多轮时：先把会话历史取出来（``ekbs_session_ctx``，键含用户维度）→ **查询改写**
        → 用改写后的查询检索 → 带历史生成 → 把本轮写回上下文。
        每轮都重新加载 ``PermContext`` 由调用方负责（`03` §6.1 V5）。
        """
        # ① 会话历史：调用方显式给了就用给的，否则按 (conversation_id, 用户) 取
        messages = list(history)
        if not messages and self.sessions is not None and conversation_id:
            messages = await asyncio.to_thread(
                self.sessions.load, conversation_id, perm.dingtalk_userid
            )

        # ② 查询改写（00 §2.1 的第一步）；改写后的查询才是检索输入
        rewrite = await self._rewrite(question, messages)

        cached = None
        # 答案缓存键必须含**历史指纹**：同一问题在不同上下文下答案不同，
        # 否则多轮里会把上一轮的答案复用成这一轮的（C2 的同类问题）
        # 缓存键要含**检索配置版本**：只按 (权限指纹, 问题) 建键时，
        # 改了 min_score / 换精排模型 / 开关 rerank 之后，旧答案会继续被服务到 TTL 到期。
        # 实测踩到：阈值从 0.30 改到 0.60 后，同一个问题仍在 3ms 内返回旧语义的
        # `refused=false + 5 条引用 + 答案「未找到可靠依据」`。
        cache_key = CacheStore.answer_key(
            perm.fingerprint,
            f"{rewrite.rewritten}|{_history_digest(messages)}|gen={int(allow_generation)}"
            f"|rc={self.retrieval_cache_revision()}",
        )
        if use_cache:
            cached = await self.cache.get(cache_key)
        if cached:
            retrieval = RetrievalResult(
                query=rewrite.rewritten, chunks=[], refused=False,
                timings_ms={"embed": 0.0, "recall": 0.0, "rerank": 0.0, "expand": 0.0, "total": 0.0},
            )
            citations = [Citation(**item) for item in cached.get("citations", [])]
            result = AnswerResult(
                answer=str(cached.get("answer", "")),
                citations=citations,
                retrieval=retrieval,
                model=str(cached.get("model", "")),
                rewritten_query=rewrite.rewritten,
                refused=bool(cached.get("refused", False)),
                refusal_reason=str(cached.get("refusal_reason", "")),
                degraded=["answer_cache_hit"],
            )
            # 命中缓存也要记这一轮上下文：否则多轮追问会丢掉这一轮（实测踩到）
            await self._remember_turn(conversation_id, perm, question, result)
            return result

        retrieval = await self.retrieve(rewrite.rewritten, perm)
        if allow_generation:
            result = await self.generate(question, retrieval, perm, history=messages)
        else:
            result = self._retrieval_only_answer(question, retrieval, perm)
        result.rewritten_query = rewrite.rewritten
        result.history_used = rewrite.used_history

        # ③ 写回上下文（群聊按用户隔离，见 session.py 的说明）
        await self._remember_turn(conversation_id, perm, question, result)

        if use_cache and result.answer and not result.refused:
            await self.cache.set(
                cache_key,
                {
                    "answer": result.answer,
                    "model": result.model,
                    "refused": result.refused,
                    "refusal_reason": result.refusal_reason,
                    "citations": [c.__dict__ for c in result.citations],
                },
                ttl=self.settings.retrieval.cache.answer_ttl,
            )
        return result

    async def _rewrite(self, question: str, messages: Sequence[dict[str, str]]) -> Any:
        if self.rewriter is None:
            from ekbs.session import RewriteResult

            return RewriteResult(question, question, "off", used_history=False)
        return await self.rewriter.rewrite(question, messages)

    async def _remember_turn(
        self, conversation_id: str | None, perm: PermContext, question: str, result: AnswerResult
    ) -> None:
        """把本轮问答写回会话上下文（命中缓存时也要写，两条路径共用一份实现）。"""
        if self.sessions is None or not conversation_id or result.refused:
            return
        await asyncio.to_thread(
            self.sessions.append,
            conversation_id,
            perm.dingtalk_userid,
            [
                {"role": "user", "content": question},
                {"role": "assistant", "content": result.answer},
            ],
        )

    def retrieval_cache_revision(self) -> str:
        """检索配置版本（进答案缓存键）：影响答案的每一个开关都必须进来。

        漏掉任何一项，改配置后旧答案都会继续被服务到 TTL 到期 —— 这类"配置改了但
        行为没变"的排查成本极高，因此这里宁可多算几次哈希。
        """
        import hashlib

        cfg = self.settings.retrieval
        embed = getattr(self.providers.embed, "ref", None)
        rerank = getattr(self.providers.rerank, "ref", None) if self.providers.rerank else None
        llm = getattr(self.providers.llm, "ref", None)
        parts = [
            f"embed={getattr(embed, 'ident', '')}",
            f"rerank={getattr(rerank, 'ident', '')}:{int(cfg.rerank.enabled)}:{cfg.rerank.top_n}"
            f":{cfg.rerank.keep}:{cfg.rerank.min_score}",
            f"llm={getattr(llm, 'ident', '')}",
            f"k={cfg.dense_top_k}:{cfg.sparse_top_k}:{cfg.rrf_k}:{int(cfg.parent_expand)}",
            f"rw={getattr(cfg.query_rewrite, 'strategy', '')}",
            f"prompt={PROMPT_REVISION}",
        ]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _history_digest(messages: Sequence[dict[str, str]]) -> str:
    """历史摘要（进答案缓存键）：同一问题在不同上下文下必须落到不同的键。"""
    import hashlib

    if not messages:
        return "-"
    raw = "\n".join(f"{m.get('role')}:{m.get('content')}" for m in messages)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _ms(start: float, end: float | None = None) -> float:
    return round(((end if end is not None else time.perf_counter()) - start) * 1000, 2)
