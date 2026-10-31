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
from dataclasses import dataclass, field, replace
from typing import Any, Sequence

from ekbs.cache.store import CacheStore
from ekbs.config import Settings
from ekbs.db import Database
from ekbs.errors import EmbeddingUnavailable, ModelUnavailable, RerankUnavailable
from ekbs.index.faiss_store import ShardIndexManager
from ekbs.index.figure_faiss import FigureIndex
from ekbs.index.fts import FtsIndex
from ekbs.ingest.chunker import count_tokens, retrieval_text
from ekbs.logging import get_logger
from ekbs.permission.acl import DocumentGate
from ekbs.permission.context import PermContext
from ekbs.provider.base import RerankDoc
from ekbs.provider.registry import Providers
from ekbs.retrieval.answer_verify import AnswerVerifier, VerifyResult
from ekbs.retrieval.faithfulness import (
    DEGRADED_UNSUPPORTED,
    REFUSE_MISSING_CITATION,
    assess_faithfulness,
    clause_mismatches,
    mostly_unsupported,
)
from ekbs.retrieval.numeric import (
    MODE_AUTO,
    MODE_NUMERIC,
    MODE_PROSE,
    build_numeric_prompt,
    find_scope_alternatives,
    format_numeric_user_reply,
    format_scope_clarify_reply,
    parse_numeric_payload,
    question_pins_scope,
    resolve_answer_mode,
    verify_copied,
    verify_scope,
)
from ekbs.retrieval.intent import IntentSplitter, SubQuestion
from ekbs.retrieval.rrf import alpha_fuse, ranks_of, rrf_fuse, wrrf_fuse

log = get_logger(component="retrieval")

#: 拒答原因（写进 ``ekbs_qa_message.refused`` 的旁证，供审计与评测分组）
REFUSE_NO_SHARD = "no_visible_shard"
REFUSE_EMBED_UNAVAILABLE = "embedding_unavailable"
REFUSE_NO_CANDIDATE = "no_candidate"
REFUSE_BELOW_THRESHOLD = "below_threshold"
#: 预算裁剪时，"最后一块"低于这个 token 数就不再截断（再截就没信息了）
_BUDGET_MIN_CHUNK_TOKENS = 128

REFUSE_ALL_DENIED = "all_denied_by_c5"
REFUSE_EMPTY_ANSWER = "empty_answer"
REFUSE_NO_BASIS = "no_basis_in_context"
#: 生成后置"资料充分性"核验判定为不支持（见 ekbs/retrieval/answer_verify.py）
REFUSE_ANSWER_VERIFY = "answer_verify_unsupported"
REFUSE_ANSWER_VERIFY_UNAVAILABLE = "answer_verify_unavailable"
# REFUSE_MISSING_CITATION / REFUSE_UNSUPPORTED_CLAIM 自 faithfulness 导入后原样再导出

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
    #: D18 粗筛（rerank 之前的 doc 级 ACL）丢弃数；与 c5_dropped（rerank 后复检）分列
    c5_pre_dropped: int = 0
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
class _RecallLanes:
    """② 召回阶段的产物：dense/sparse/figure 三路 + 查询变体。"""

    variants: list[str]
    dense_scored: list[tuple[int, float]]
    sparse_scored: list[tuple[int, float]]
    dense_ids: list[int]
    sparse_ids: list[int]
    figure_ids: list[int]
    figure_scored: list[tuple[int, float]]
    use_figure_mm: bool
    degraded: list[str] = field(default_factory=list)


@dataclass
class _FusionOutcome:
    """③ 融合阶段的产物。"""

    fused: list[tuple[int, float]]
    dense_ranks: dict[int, int]
    sparse_ranks: dict[int, int]
    mode: str


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
    #: 生成侧忠实度报告（规则型 claim 支撑检查；调试台面板用）
    faithfulness: dict[str, Any] = field(default_factory=dict)
    #: **取数模式**的结构化结果（值/单位/页码/原文片段/是否通过抄写校验）。
    #: 业务问答（prose）下为 None —— 两种模式的答案形态不同，前端据此分别渲染。
    numeric: dict[str, Any] | None = None
    #: 解析后的实际作答模式（``prose`` | ``numeric``）；请求可为 ``auto``
    mode_used: str = MODE_PROSE
    #: 查询改写侧意图标签（factual / numeric / figure / procedure）
    intent: str = "factual"
    #: 供钉钉/前端直接回源的图片附件（相对 data_dir 的 image_path）
    figure_attachments: list[dict[str, Any]] = field(default_factory=list)
    ttft_ms: int = 0
    total_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: 上下文预算裁剪痕迹（可观测性：丢了几块 / 几轮、是否截断）
    dropped_chunks: int = 0
    dropped_history: int = 0
    context_truncated: bool = False
    #: **输出**被截断（模型 finish_reason=length，撞到 max_tokens 上限）。
    #: 与 context_truncated（输入侧裁剪）不是一回事：它意味着答案本身不完整。
    truncated: bool = False
    #: 多意图拆分（P0）：单问时为空；多子问时为 [{index,text,rewritten,source}]
    sub_questions: list[dict[str, Any]] = field(default_factory=list)
    #: 子问覆盖情况：{total, covered, uncovered}；单问时为空 dict
    coverage: dict[str, Any] = field(default_factory=dict)


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
    fusion: str | None = None
    hybrid_alpha: float | None = None
    multi_query: int | None = None
    dense_multi_query: int | None = None
    figure_multimodal: bool | None = None

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
            "fusion": (self.fusion or getattr(cfg, "fusion", "rrf") or "rrf").lower(),
            "hybrid_alpha": (
                cfg.hybrid_alpha if self.hybrid_alpha is None else self.hybrid_alpha
            ),
            "multi_query": (
                cfg.multi_query if self.multi_query is None else self.multi_query
            ),
            "dense_multi_query": (
                getattr(cfg, "dense_multi_query", 1)
                if self.dense_multi_query is None
                else self.dense_multi_query
            ),
            "figure_multimodal": (
                bool(getattr(cfg, "figure_multimodal", True))
                if self.figure_multimodal is None
                else bool(self.figure_multimodal)
            ),
        }


SYSTEM_PROMPT = (
    "你是企业知识库问答助手。只能依据下面提供的资料回答，不得编造。\n"
    "引用资料时必须在句末标注编号，格式如 [1]、[2]。\n"
    "资料里以【图片】开头的条目是可展示的原图：用户索要照片/图片/截图时，"
    "必须引用这些编号（界面会展示图片），不要因为无法在文字里嵌入图片就回复「未找到可靠依据」。\n"
    # 输出长度是硬约束（max_tokens=2048 ≈ 2000 字）：不加限制提示时，模型会试图复述
    # 整份合同/全文，结果被静默截断，用户看到半截还以为答完了。
    "回答要简洁，只答被问到的内容；如果用户要求返回整份文档/全文（例如整份合同），"
    "不要复述全文，只给出要点摘要，并明确说明「篇幅有限，完整内容请查看引用原文或使用文件下发」。\n"
    "若资料完全不足以回答，才直接回复「未找到可靠依据」。\n\n"
    "资料：\n{context}"
)

#: 输出被截断时追加给用户的提示（不把半截答案伪装成完整答案）
TRUNCATION_NOTICE = "（提示：回答已达长度上限被截断，未显示的部分请缩小问题范围或查看引用原文。）"

#: 提示词版本（进答案缓存键）：**改动 SYSTEM_PROMPT 必须同步 +1**，
#: 否则旧提示词生成的答案会继续从缓存里被服务。
PROMPT_REVISION = "v5"


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
        figure_index: FigureIndex | None = None,
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
        self.figure_index = figure_index
        # 生成后置核验（默认关闭；只对边界分调用，见 AnswerVerifyCfg）
        self.answer_verifier = AnswerVerifier(settings.answer_verify)
        # 多意图拆分（P0）：默认关闭；enabled 时多子问走独立召回 + 配额精排
        self.intent_splitter = IntentSplitter(settings)

    # ------------------------------------------------------------------
    # 查询嵌入（含缓存与「不可用即拒答」）
    # ------------------------------------------------------------------

    async def _multimodal_query_vector(self, query: str) -> list[float] | None:
        """多模态查询向量（**带缓存**）。

        为什么必须缓存：文本查询向量早就走了 `query_embedding_ttl` 缓存，而这条曾经
        每次都直连云端 —— 于是**同一个问题每问一次就多打一次多模态嵌入**，在连续提问下
        把低配额端点打成持续 429（实测 sweep：858 次调用 vs 882 次 429，一轮 A/B 从 7 分钟
        被拖到 50 分钟）。这不是"要不要图片旁路"的问题，是"别重复付费"的问题。

        键用 :meth:`CacheStore.mm_embed_key`：**与文本嵌入分开命名空间** ——
        两个向量维度不同（1152 vs 1024），混用会把错误的向量喂进错误的索引。
        """
        key = CacheStore.mm_embed_key(query)
        cached = await self.cache.get(key)
        if cached is not None:
            return list(cached)
        vector = await self.providers.multimodal.embed_text(query)
        if not vector:
            return None
        expected = getattr(self.figure_index, "dim", None)
        if expected and len(vector) != int(expected):
            # 维度不符时**不写缓存**：否则会把错的向量缓存 24 小时
            log.warning(
                "mm_embed_dim_mismatch", got=len(vector), expected=int(expected)
            )
            return None
        await self.cache.set(
            key, list(vector), ttl=self.settings.retrieval.cache.query_embedding_ttl
        )
        return list(vector)

    async def query_vector(self, query: str) -> list[float] | None:
        """返回查询向量；None 表示嵌入不可用（调用方必须拒答，不得降级）。"""
        # 缓存键带上模型指纹：开启/关闭不对称编码（text_type）后指纹会变，
        # 不带它就会命中"另一种编码"算出来的旧向量（语义混用，静默错搜）。
        key = CacheStore.embed_key(query, variant=self.providers.embed.ref.fingerprint)
        cached = await self.cache.get(key)
        if cached is not None:
            return list(cached)
        try:
            # 查询侧用 embed_query（不对称编码时为 text_type=query；对称时等价 embed_one）
            vector = await self.providers.embed.embed_query(query)
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
        return [chunk_id for chunk_id, _ in self._dense_recall_scored(vector, perm, top_k)]

    def _dense_recall_scored(
        self, vector: Sequence[float], perm: PermContext, top_k: int | None = None
    ) -> list[tuple[int, float]]:
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
        return [(chunk_id, score) for score, chunk_id in per_shard]

    def _chunk_ids_by_faiss_row(self, shard_id: str, rows: Sequence[int]) -> dict[int, int]:
        """FAISS 命中 → ``chunk_id``；同时做**权限与状态下推**（与 FTS5 路径语义等价）。

        索引换成 ``IndexIDMap2`` 后，FAISS 返回的标签**就是 ``chunk_id``**，
        所以这里直接按主键回表。名字保留 ``faiss_row`` 是为了不动调用方；
        语义已从"行号"变成"身份"（见 ``ekbs/index/faiss_store.py`` 的说明）。
        """
        if not rows:
            return {}
        placeholders = ",".join("?" for _ in rows)
        sql = (
            "SELECT c.chunk_id AS chunk_id "
            "FROM ekbs_chunk c JOIN ekbs_document d ON d.doc_id = c.doc_id "
            "WHERE c.shard_id = ? AND c.chunk_id IN (" + placeholders + ") "
            "AND d.status = 'published' "
            "AND (d.effective_date IS NULL OR d.effective_date <= date('now')) "
            "AND (d.expire_date IS NULL OR d.expire_date > date('now'))"
        )
        with self.db.sync_connect() as conn:
            result = conn.execute(sql, (shard_id, *[int(r) for r in rows])).fetchall()
        return {int(r["chunk_id"]): int(r["chunk_id"]) for r in result}

    def _sparse_recall(
        self, query: str, perm: PermContext, top_k: int | None = None
    ) -> list[int]:
        return [chunk_id for chunk_id, _ in self._sparse_recall_scored(query, perm, top_k)]

    def _sparse_recall_scored(
        self,
        query: str,
        perm: PermContext,
        top_k: int | None = None,
        *,
        variants: Sequence[str] | None = None,
    ) -> list[tuple[int, float]]:
        """BM25 召回；``variants`` 非空时 MultiQuery 多路取最佳（分数已翻转为越高越好）。"""
        queries = [q for q in (variants or [query]) if (q or "").strip()]
        if not queries:
            queries = [query]
        best: dict[int, float] = {}
        for q in queries:
            hits = self.fts.search(
                q,
                shard_ids=perm.visible_shard_ids,
                clearance=perm.clearance,
                dept_chain=perm.dept_chain,
                top_k=top_k or self.settings.retrieval.sparse_top_k,
                # 双路径等价（03 §5.3）：稀疏路放行语义须与闸门一致，
                # 否则跨部门授权用户的 BM25 一路静默为空
                acl_allow_shards=sorted(perm.acl_allow_shards),
                subjects=sorted(perm.subjects),
            )
            for hit in hits:
                # bm25 越小越相关 → 取负后越大越好（供 α 融合）
                score = -float(hit.score)
                prev = best.get(hit.chunk_id)
                if prev is None or score > prev:
                    best[hit.chunk_id] = score
        return sorted(best.items(), key=lambda pair: (-pair[1], pair[0]))

    def _query_variants(self, query: str, *, limit: int) -> list[str]:
        if limit <= 1:
            return [query]
        if self.rewriter is not None and hasattr(self.rewriter, "expand_variants"):
            # 传显式 limit：否则会再按**进程级配置**截断一次，把调试台的 override 吞掉
            return self.rewriter.expand_variants(query, limit=limit)[:limit]
        return [query]

    def _dense_query_variants(self, query: str, *, limit: int) -> list[str]:
        """**稠密侧**变体：语义改写（不是关键词碎片）。

        与 `_query_variants`（BM25 用）分开 —— 把 `_keyword_focus` 那种碎片喂给嵌入模型
        会把语义拉偏。没有 rewriter 或未实现时退化为单查询（保持旧行为）。
        """
        if limit <= 1:
            return [query]
        if self.rewriter is not None and hasattr(self.rewriter, "expand_dense_variants"):
            return self.rewriter.expand_dense_variants(query, limit=limit)[:limit] or [query]
        return [query]

    def _figure_recall_scored(
        self,
        mm_vector: Sequence[float],
        perm: PermContext,
        top_k: int | None = None,
    ) -> list[tuple[int, float]]:
        """多模态 figure 旁路召回；命中后按可见分片 + published 过滤（C1）。"""
        if self.figure_index is None or not mm_vector:
            return []
        k = top_k or self.settings.retrieval.dense_top_k
        hits = self.figure_index.search(mm_vector, k)
        if not hits:
            return []
        chunk_ids = [cid for cid, _ in hits]
        placeholders = ",".join("?" for _ in chunk_ids)
        sql = (
            "SELECT c.chunk_id, c.shard_id FROM ekbs_chunk c "
            "JOIN ekbs_document d ON d.doc_id = c.doc_id "
            f"WHERE c.chunk_id IN ({placeholders}) "
            "AND c.chunk_type = 'figure' "
            "AND d.status = 'published' "
            "AND (d.effective_date IS NULL OR d.effective_date <= date('now')) "
            "AND (d.expire_date IS NULL OR d.expire_date > date('now'))"
        )
        with self.db.sync_connect() as conn:
            rows = conn.execute(sql, tuple(chunk_ids)).fetchall()
        allowed = {
            int(r["chunk_id"])
            for r in rows
            if str(r["shard_id"]) in set(perm.visible_shard_ids)
        }
        return [(cid, score) for cid, score in hits if cid in allowed]

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

        # ② 两路召回（FAISS 是同步 C 调用，放线程池；04 §1.5）+ figure 第三路
        lanes = await self._recall_lanes(query, perm, params, vector)
        result.degraded.extend(lanes.degraded)
        recall_done = time.perf_counter()
        if collect_debug:
            self._record_recall_debug(result, params, perm, vector, lanes)

        # ③ 融合：默认 RRF（含 figure 第三路）；可切 α 加权（课程 18，仍只融 dense+sparse）
        fusion_outcome = self._fuse_lanes(lanes, params)
        fused = fusion_outcome.fused
        dense_ranks = fusion_outcome.dense_ranks
        sparse_ranks = fusion_outcome.sparse_ranks
        if collect_debug:
            result.stage_debug["fusion"] = {
                "mode": fusion_outcome.mode,
                "hybrid_alpha": params.get("hybrid_alpha"),
                "multi_query": lanes.variants,
                "figure_lane": bool(lanes.figure_ids),
            }
        if not fused:
            result.refused = True
            result.refusal_reason = REFUSE_NO_CANDIDATE
            result.timings_ms.update(
                embed=_ms(started, embed_done),
                recall=_ms(embed_done, recall_done),
                total=_ms(started),
            )
            return result

        # ④ 精排（含 D18 粗筛；rerank 不可用/关闭时降级为 RRF Top-keep）
        rerank_enabled, ranked, pre_dropped_ids = await self._rerank_candidates(
            query, perm, params, fused, dense_ranks, sparse_ranks, result, collect_debug
        )

        rerank_done = time.perf_counter()

        # ④ C5 复检（00 v4.0 C5 双闸之第二闸：rerank 之后、进 Prompt 之前；
        #    与 D18 粗筛冗余互补 —— 防「粗筛→复检」之间的权限变更窗口；被丢弃者不得进入任何下游）
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
            # 粗筛（D18）与复检（C5）任一拦截过候选，都算「全被权限拦掉」而非「无候选」
            result.refusal_reason = (
                REFUSE_ALL_DENIED if (dropped or pre_dropped_ids) else REFUSE_NO_CANDIDATE
            )
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
        # 短叶子父块（制度点分条款各成碎片时）把同章兄弟一并带入，补全召回上下文
        selected = await asyncio.to_thread(self._expand_short_siblings, selected, perm)
        # 正文命中后，把同页的 figure 一并带进上下文（PPT「把照片发出来」依赖这条）
        selected = await asyncio.to_thread(self._expand_page_figures, selected, perm)
        # docx 等常无页码：同页扩不到时，再按同文档补 figure（熙物联二维码踩过）
        selected = await asyncio.to_thread(self._expand_doc_figures, selected, perm)

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

    async def _recall_lanes(
        self,
        query: str,
        perm: PermContext,
        params: dict[str, Any],
        vector: list[float],
    ) -> _RecallLanes:
        """② 双路召回（dense 多变体 + sparse）+ figure 第三路。"""
        variants = self._query_variants(query, limit=int(params.get("multi_query") or 1))
        # 稠密多查询：每个语义变体各嵌一次、各召回一路，再**用 RRF 融合**。
        # 为什么不平均向量：变体越多平均向量越糊（把多个语义中心折中），实测更差。
        # 单变体（默认 dense_multi_query=1）时就是 `vector` 那一路，**与旧行为完全一致**。
        dense_variants = self._dense_query_variants(
            query, limit=int(params.get("dense_multi_query") or 1)
        )
        dense_vectors = [vector]
        for variant in dense_variants[1:]:
            extra = await self.query_vector(variant)
            if extra is None:
                log.warning("dense_variant_embed_failed", variant_len=len(variant))
                continue
            dense_vectors.append(extra)
        dense_lanes = []
        for dense_vector in dense_vectors:
            lane = await asyncio.to_thread(
                self._dense_recall_scored, dense_vector, perm, params["dense_top_k"]
            )
            dense_lanes.append(lane)
        if len(dense_lanes) > 1:
            dense_scored = rrf_fuse(
                [[chunk_id for chunk_id, _ in lane] for lane in dense_lanes],
                k=params["rrf_k"],
            )
        else:
            dense_scored = dense_lanes[0]
        sparse_scored = await asyncio.to_thread(
            self._sparse_recall_scored,
            query,
            perm,
            params["sparse_top_k"],
            variants=variants,
        )
        figure_ids, figure_scored, use_figure_mm, degraded = await self._recall_figures(
            query, perm, params
        )
        return _RecallLanes(
            variants=variants,
            dense_scored=dense_scored,
            sparse_scored=sparse_scored,
            dense_ids=[chunk_id for chunk_id, _ in dense_scored],
            sparse_ids=[chunk_id for chunk_id, _ in sparse_scored],
            figure_ids=figure_ids,
            figure_scored=figure_scored,
            use_figure_mm=use_figure_mm,
            degraded=degraded,
        )

    async def _recall_figures(
        self, query: str, perm: PermContext, params: dict[str, Any]
    ) -> tuple[list[int], list[tuple[int, float]], bool, list[str]]:
        """②ʹ figure 多模态第三路；失败降级不拖垮检索。"""
        use = (
            bool(params.get("figure_multimodal"))
            and self.figure_index is not None
            and self.providers.multimodal is not None
        )
        if not use:
            return [], [], False, []
        try:
            mm_vector = await self._multimodal_query_vector(query)
            scored = await asyncio.to_thread(
                self._figure_recall_scored, mm_vector, perm, params["dense_top_k"]
            )
            return [chunk_id for chunk_id, _ in scored], scored, True, []
        except Exception as exc:  # noqa: BLE001 - 多模态旁路失败不拖垮检索
            log.warning("figure_mm_recall_failed", error=str(exc)[:160])
            return [], [], True, ["figure_mm_unavailable"]

    def _record_recall_debug(
        self,
        result: RetrievalResult,
        params: dict[str, Any],
        perm: PermContext,
        vector: list[float],
        lanes: _RecallLanes,
    ) -> None:
        """把召回阶段的分片/BM25/figure 命中写进调试台（仅 collect_debug 时）。"""
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
            "merged_chunk_ids": lanes.dense_ids,
        }
        sparse_top = lanes.sparse_scored[: params["sparse_top_k"]]
        shard_by_chunk: dict[int, str] = {}
        if sparse_top:
            ids = [chunk_id for chunk_id, _score in sparse_top]
            ph = ",".join("?" for _ in ids)
            with self.db.sync_connect() as conn:
                shard_by_chunk = {
                    int(row["chunk_id"]): str(row["shard_id"])
                    for row in conn.execute(
                        f"SELECT chunk_id, shard_id FROM ekbs_chunk WHERE chunk_id IN ({ph})",
                        ids,
                    )
                }
        result.stage_debug["sparse"] = {
            "requested_top_k": params["sparse_top_k"],
            "variants": lanes.variants,
            "hits": [
                {
                    "chunk_id": chunk_id,
                    # 前端列名是 BM25 分数；同时保留 score 兼容旧调试台契约
                    "bm25": round(score, 6),
                    "score": round(score, 6),
                    "shard_id": shard_by_chunk.get(int(chunk_id)),
                }
                for chunk_id, score in sparse_top
            ],
        }
        result.stage_debug["figure_mm"] = {
            "enabled": lanes.use_figure_mm,
            "hits": [
                {"chunk_id": chunk_id, "score": round(score, 6)}
                for chunk_id, score in lanes.figure_scored[: params["dense_top_k"]]
            ],
        }

    def _fuse_lanes(self, lanes: _RecallLanes, params: dict[str, Any]) -> _FusionOutcome:
        """③ 融合：RRF / 加权 RRF（wrrf）/ α 加权（含 figure 第三路）。"""
        fusion = str(params.get("fusion") or "rrf").lower()
        alpha = float(
            params.get("hybrid_alpha")
            if params.get("hybrid_alpha") is not None
            else 0.5
        )
        alpha = max(0.0, min(1.0, alpha))
        if fusion == "alpha":
            fused = alpha_fuse(lanes.dense_scored, lanes.sparse_scored, alpha=alpha)
            if lanes.figure_ids:
                # α 路径再与 figure 做一次 RRF，避免图模态分数与文本量纲混算
                fused_ids = [cid for cid, _ in fused]
                fused = rrf_fuse([fused_ids, lanes.figure_ids], k=params["rrf_k"])
        elif fusion == "wrrf":
            # 加权 RRF：只用名次，权重只调稠密/稀疏贡献比（不做分数归一化）
            fused = wrrf_fuse(
                [lanes.dense_ids, lanes.sparse_ids],
                [alpha, 1.0 - alpha],
                k=params["rrf_k"],
            )
            if lanes.figure_ids:
                fused_ids = [cid for cid, _ in fused]
                fused = rrf_fuse([fused_ids, lanes.figure_ids], k=params["rrf_k"])
        else:
            fusion_lanes = [lanes.dense_ids, lanes.sparse_ids]
            if lanes.figure_ids:
                fusion_lanes.append(lanes.figure_ids)
            fused = rrf_fuse(fusion_lanes, k=params["rrf_k"])
        return _FusionOutcome(
            fused=fused,
            dense_ranks=ranks_of(lanes.dense_ids),
            sparse_ranks=ranks_of(lanes.sparse_ids),
            mode=fusion,
        )

    async def _rerank_candidates(
        self,
        query: str,
        perm: PermContext,
        params: dict[str, Any],
        fused: list[tuple[int, float]],
        dense_ranks: dict[int, int],
        sparse_ranks: dict[int, int],
        result: RetrievalResult,
        collect_debug: bool,
    ) -> tuple[bool, list[RetrievedChunk], set[int]]:
        """④ 精排：D18 粗筛 → 云端 rerank（不可用降级 RRF）→ ``(enabled, ranked, pre_dropped_ids)``。"""
        rerank_enabled = bool(params["rerank_enabled"]) and self.providers.rerank is not None
        shortlist = [chunk_id for chunk_id, _ in fused[: params["rerank_top_n"]]]
        result.candidate_count = len(shortlist)
        ranked: list[RetrievedChunk] = []
        #: D18 粗筛丢弃的候选（rerank 路径填充；关闭路径无出网行为，由 C5 复检兜底）
        pre_dropped_ids: set[int] = set()

        if not rerank_enabled:
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
            return rerank_enabled, ranked, pre_dropped_ids

        docs = self.load_chunks(shortlist)
        # D18 粗筛（00 v4.0 C5 双闸之第一闸）：云端 rerank **之前**先做 doc 级 ACL。
        # 无权 chunk 不进入 payload、不发出企业边界 —— 分片过滤只能保证「片级有权」，
        # 片内 deny / ACL 补丁整片加载的无权文档，必须在出网前拦下（rerank 后仍有 C5 复检）。
        pre_pairs = [(cid, docs[cid].doc_id) for cid in shortlist if cid in docs]
        _, pre_dropped = await asyncio.to_thread(self.gate.filter_chunks, perm, pre_pairs)
        pre_dropped_ids = {chunk_id for chunk_id, _ in pre_dropped}
        result.c5_pre_dropped = len(pre_dropped)
        if collect_debug:
            result.stage_debug["c5_pre"] = {
                "checked": len(pre_pairs),
                "dropped": len(pre_dropped),
                "dropped_chunk_ids": [cid for cid, _ in pre_dropped],
            }
        payload = [
            RerankDoc(
                chunk_id=chunk_id,
                # 与嵌入同口径：文档名 + 小节标题，精排才看得见「第二条 住宿标准」这类限定词
                text=retrieval_text(
                    docs[chunk_id].heading_path,
                    docs[chunk_id].content,
                    doc_title=docs[chunk_id].doc_title,
                ),
                shard_id=docs[chunk_id].shard_id,
                rrf_score=score,
                dense_rank=dense_ranks.get(chunk_id),
                sparse_rank=sparse_ranks.get(chunk_id),
            )
            for chunk_id, score in fused[: params["rerank_top_n"]]
            if chunk_id in docs and chunk_id not in pre_dropped_ids
        ]
        reranked: list = []
        if payload:
            try:
                reranked = await self.providers.rerank.rerank(
                    query, payload, params["rerank_top_n"]
                )
            except (RerankUnavailable, ModelUnavailable) as exc:
                # 00 §2.3：跳过精排，直接取 RRF Top-5（仍须过 C5）
                log.warning("rerank_unavailable_degrade", error=str(exc))
                result.degraded.append("rerank_skipped")
        else:
            # 候选被 D18 全部丢弃：不向外部发任何请求（顺带省一次 rerank 调用）
            log.info(
                "d18_prefilter_emptied_payload", user_id=perm.user_id, dropped=len(pre_dropped)
            )

        score_map = {item.chunk_id: item.score for item in reranked}
        if score_map:
            for chunk_id, rrf_score in fused[: params["rerank_top_n"]]:
                if chunk_id in pre_dropped_ids:
                    continue  # D18：被粗筛丢弃者不回流 ranked
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
            if payload:
                result.degraded.append("rerank_fallback_rrf")
            for chunk_id, rrf_score in fused[: params["rerank_keep"]]:
                if chunk_id in pre_dropped_ids:
                    continue  # D18：降级路径同样不放行
                chunk = docs.get(chunk_id)
                if chunk is None:
                    continue
                chunk.rrf_score = rrf_score
                chunk.dense_rank = dense_ranks.get(chunk_id)
                chunk.sparse_rank = sparse_ranks.get(chunk_id)
                ranked.append(chunk)
        return rerank_enabled, ranked, pre_dropped_ids

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

    def _expand_short_siblings(
        self,
        chunks: list[RetrievedChunk],
        perm: PermContext,
        *,
        short_chars: int = 160,
        window: int = 4,
        max_total: int = 16,
    ) -> list[RetrievedChunk]:
        """短叶子父块命中时，拉取同文档、同一级标题下的相邻兄弟进上下文。

        交接班类 Word 常被切成「每条 4.x 一个 20～90 字父块」且无 child；
        仅靠 keep=5 只能带上几条碎片。兄弟扩展在**不重切库**的前提下补全同章条款。
        扩展块仍过 C5。
        """
        if not chunks:
            return chunks
        present = {c.chunk_id for c in chunks}
        anchors = [
            c
            for c in chunks
            if c.chunk_type == "parent" and len((c.content or "").strip()) < short_chars
        ]
        if not anchors:
            return chunks

        extra_ids: list[int] = []
        seen_extra: set[int] = set()
        with self.db.sync_connect() as conn:
            for anchor in anchors:
                root = (anchor.heading_path or "").split(" > ", 1)[0].strip()
                if not root:
                    continue
                rows = conn.execute(
                    "SELECT chunk_id, heading_path, seq FROM ekbs_chunk "
                    "WHERE doc_id = ? AND chunk_type = 'parent' "
                    "AND (heading_path = ? OR heading_path LIKE ?) "
                    "ORDER BY seq",
                    (int(anchor.doc_id), root, f"{root} > %"),
                ).fetchall()
                if not rows:
                    continue
                ids = [int(r["chunk_id"]) for r in rows]
                try:
                    idx = ids.index(int(anchor.chunk_id))
                except ValueError:
                    idx = 0
                lo = max(0, idx - window)
                hi = min(len(ids), idx + window + 1)
                for cid in ids[lo:hi]:
                    if cid in present or cid in seen_extra:
                        continue
                    seen_extra.add(cid)
                    extra_ids.append(cid)
                    if len(extra_ids) >= max_total:
                        break
                if len(extra_ids) >= max_total:
                    break

        if not extra_ids:
            return chunks

        loaded = self.load_chunks(extra_ids)
        kept, dropped = self.gate.filter_chunks(
            perm, [(cid, loaded[cid].doc_id) for cid in loaded]
        )
        allowed = {chunk_id for chunk_id, _ in kept}
        if dropped:
            log.info("c5_denied_sibling", count=len(dropped), user_id=perm.user_id)

        # 按锚点分数赋予扩展块略低的分，保持原命中优先
        score_by_doc = {
            c.doc_id: (c.rerank_score, c.rrf_score) for c in anchors
        }
        out = list(chunks)
        for cid in extra_ids:
            if cid not in allowed or cid not in loaded:
                continue
            sib = loaded[cid]
            base = score_by_doc.get(sib.doc_id, (0.0, 0.0))
            sib.rerank_score = float(base[0]) * 0.85 if base[0] else 0.0
            sib.rrf_score = float(base[1]) * 0.85 if base[1] else 0.0
            out.append(sib)
            present.add(cid)
        return out

    def _page_figure_ids(
        self,
        anchors: list[RetrievedChunk],
        present: set[int],
        *,
        max_per_anchor: int,
        max_total: int,
    ) -> list[int]:
        """按锚点页重叠查同文档 figure 块 id（去重 + 上限）。"""
        figure_ids: list[int] = []
        seen_fig: set[int] = set()
        with self.db.sync_connect() as conn:
            for anchor in anchors:
                page_lo = int(anchor.page_from or 0)
                page_hi = int(anchor.page_to or page_lo)
                rows = conn.execute(
                    "SELECT chunk_id FROM ekbs_chunk "
                    "WHERE doc_id = ? AND chunk_type = 'figure' "
                    "AND page_from IS NOT NULL "
                    "AND page_from <= ? AND COALESCE(page_to, page_from) >= ? "
                    "ORDER BY seq LIMIT ?",
                    (int(anchor.doc_id), page_hi, page_lo, int(max_per_anchor)),
                ).fetchall()
                for row in rows:
                    cid = int(row["chunk_id"])
                    if cid in present or cid in seen_fig:
                        continue
                    seen_fig.add(cid)
                    figure_ids.append(cid)
                    if len(figure_ids) >= max_total:
                        break
                if len(figure_ids) >= max_total:
                    break
        return figure_ids

    @staticmethod
    def _group_figures_by_page(
        figure_ids: list[int],
        allowed: set[int],
        figures: dict[int, RetrievedChunk],
    ) -> dict[tuple[int, int, int], list[RetrievedChunk]]:
        by_doc_page: dict[tuple[int, int, int], list[RetrievedChunk]] = {}
        for cid in figure_ids:
            if cid not in allowed:
                continue
            fig = figures[cid]
            lo = int(fig.page_from or 0)
            hi = int(fig.page_to or lo)
            by_doc_page.setdefault((int(fig.doc_id), lo, hi), []).append(fig)
        return by_doc_page

    @staticmethod
    def _figures_for_chunk(
        chunk: RetrievedChunk,
        by_doc_page: dict[tuple[int, int, int], list[RetrievedChunk]],
    ) -> list[RetrievedChunk]:
        lo = int(chunk.page_from or 0)
        hi = int(chunk.page_to or lo)
        matches: list[RetrievedChunk] = []
        for (doc_id, flo, fhi), figs in by_doc_page.items():
            if doc_id != int(chunk.doc_id):
                continue
            if fhi < lo or flo > hi:
                continue
            matches.extend(figs)
        return matches

    def _attach_figures_by_page(
        self,
        expanded: list[RetrievedChunk],
        by_doc_page: dict[tuple[int, int, int], list[RetrievedChunk]],
        present: set[int],
    ) -> tuple[list[RetrievedChunk], set[int]]:
        """把插图挂在对应正文锚点后面，方便模型就近引用。"""
        out: list[RetrievedChunk] = []
        inserted: set[int] = set()
        for chunk in expanded:
            out.append(chunk)
            if chunk.chunk_type == "figure" or chunk.page_from is None:
                continue
            for fig in self._figures_for_chunk(chunk, by_doc_page):
                if fig.chunk_id in inserted or fig.chunk_id in present:
                    continue
                fig.rerank_score = chunk.rerank_score
                fig.rrf_score = chunk.rrf_score
                out.append(fig)
                inserted.add(fig.chunk_id)
        return out, inserted

    def _expand_page_figures(
        self,
        chunks: list[RetrievedChunk],
        perm: PermContext,
        *,
        max_per_anchor: int = 8,
        max_total: int = 12,
    ) -> list[RetrievedChunk]:
        """正文/表格命中后，把**同一文档、页码重叠**的 figure 块并入上下文。

        PPT「计量表更换」正文与电表照片常分属 parent / figure；只靠向量时照片
        往往排不进 top-k，用户一问「把照片发出来」就会因上下文无图而拒答。
        """
        if not chunks:
            return chunks
        present = {c.chunk_id for c in chunks}
        anchors = [
            c for c in chunks
            if c.chunk_type != "figure" and c.page_from is not None
        ]
        if not anchors:
            return chunks

        figure_ids = self._page_figure_ids(
            anchors, present, max_per_anchor=max_per_anchor, max_total=max_total
        )
        if not figure_ids:
            return chunks

        figures = self.load_chunks(figure_ids)
        kept, dropped = self.gate.filter_chunks(
            perm, [(cid, figures[cid].doc_id) for cid in figures]
        )
        allowed = {chunk_id for chunk_id, _ in kept}
        if dropped:
            log.info("c5_denied_figure", count=len(dropped), user_id=perm.user_id)

        by_doc_page = self._group_figures_by_page(figure_ids, allowed, figures)
        out, inserted = self._attach_figures_by_page(list(chunks), by_doc_page, present)
        # 未挂上的（页码对不上）仍追加在末尾，避免丢掉
        for cid in figure_ids:
            if cid in allowed and cid not in inserted and cid not in present:
                out.append(figures[cid])
                inserted.add(cid)
        if inserted:
            log.info(
                "page_figures_expanded",
                anchors=len(anchors),
                figures=len(inserted),
                user_id=perm.user_id,
            )
        return out

    def _expand_doc_figures(
        self,
        chunks: list[RetrievedChunk],
        perm: PermContext,
        *,
        max_per_doc: int = 4,
        max_total: int = 8,
    ) -> list[RetrievedChunk]:
        """正文命中但无页码（docx 常见）时，把**同文档** figure 并入上下文。

        ``_expand_page_figures`` 要求 page_from 非空；熙物联教程正文与图都无页码时
        会静默跳过，模型只能看到「请扫二维码」文字却看不到图，进而拒答或乱引其它文档的图。
        """
        if not chunks:
            return chunks
        present = {c.chunk_id for c in chunks}
        already_has_fig = {c.doc_id for c in chunks if c.chunk_type == "figure" or c.figure_id}
        doc_ids = sorted(
            {
                int(c.doc_id)
                for c in chunks
                if c.chunk_type != "figure" and int(c.doc_id or 0) not in already_has_fig
            }
        )
        if not doc_ids:
            return chunks

        figure_ids: list[int] = []
        with self.db.sync_connect() as conn:
            for doc_id in doc_ids:
                rows = conn.execute(
                    "SELECT chunk_id FROM ekbs_chunk "
                    "WHERE doc_id = ? AND chunk_type = 'figure' "
                    "ORDER BY seq LIMIT ?",
                    (doc_id, int(max_per_doc)),
                ).fetchall()
                for row in rows:
                    cid = int(row["chunk_id"])
                    if cid in present:
                        continue
                    figure_ids.append(cid)
                    if len(figure_ids) >= max_total:
                        break
                if len(figure_ids) >= max_total:
                    break
        if not figure_ids:
            return chunks

        figures = self.load_chunks(figure_ids)
        kept, dropped = self.gate.filter_chunks(
            perm, [(cid, figures[cid].doc_id) for cid in figures]
        )
        allowed = {chunk_id for chunk_id, _ in kept}
        if dropped:
            log.info("c5_denied_doc_figure", count=len(dropped), user_id=perm.user_id)

        score_by_doc: dict[int, tuple[float, float]] = {}
        for c in chunks:
            if c.chunk_type == "figure":
                continue
            prev = score_by_doc.get(int(c.doc_id), (0.0, 0.0))
            score_by_doc[int(c.doc_id)] = (
                max(prev[0], float(c.rerank_score or 0.0)),
                max(prev[1], float(c.rrf_score or 0.0)),
            )

        out = list(chunks)
        inserted = 0
        for cid in figure_ids:
            if cid not in allowed or cid in present:
                continue
            fig = figures[cid]
            base = score_by_doc.get(int(fig.doc_id), (0.0, 0.0))
            fig.rerank_score = float(base[0]) * 0.9 if base[0] else 0.0
            fig.rrf_score = float(base[1]) * 0.9 if base[1] else 0.0
            out.append(fig)
            present.add(cid)
            inserted += 1
        if inserted:
            log.info(
                "doc_figures_expanded",
                docs=len(doc_ids),
                figures=inserted,
                user_id=perm.user_id,
            )
        return out

    # ------------------------------------------------------------------
    # 生成与引用
    # ------------------------------------------------------------------

    @staticmethod
    def build_context(chunks: Sequence[RetrievedChunk]) -> str:
        """构造 Prompt 里的资料段：``[n] 标题（第 x 页）: 正文``。

        表格块优先还原为 HTML（课程 19：LLM 对 HTML 表结构理解更好）；失败回退原文。
        编号 → chunk_id 的映射由调用方随请求保存（`00` §2.2）。
        """
        from ekbs.ingest.table_serialize import information_block_to_html

        lines: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            page = ""
            if chunk.page_from:
                page = f"（第 {chunk.page_from} 页）" if chunk.page_from == chunk.page_to else (
                    f"（第 {chunk.page_from}-{chunk.page_to} 页）"
                )
            from ekbs.discovery import display_doc_title

            heading = f"{chunk.heading_path} " if chunk.heading_path else ""
            kind = ""
            body = chunk.content
            if chunk.chunk_type == "figure":
                kind = "【图片】"
            elif chunk.chunk_type == "table":
                kind = "【表格】"
                html = information_block_to_html(chunk.content)
                if html:
                    body = html
            title = display_doc_title(chunk.doc_title)
            lines.append(
                f"[{index}] {kind}{title}{page} {heading}: {body}".strip()
            )
        return "\n".join(lines)

    def _context_budget(self) -> int:
        """prompt 的 token 上限（0 = 不限制）。"""
        return int(getattr(self.settings.models.llm, "max_input_tokens", 0) or 0)

    def _fit_prompt(
        self,
        chunks: Sequence[RetrievedChunk],
        history: Sequence[dict[str, str]],
        question: str,
        *,
        system_tokens: int,
    ) -> tuple[list[RetrievedChunk], list[dict[str, str]], dict[str, Any]]:
        """把「资料 + 记忆 + 问题」压进上下文预算，返回裁剪后的三者与统计。

        为什么要它：资料块、多轮记忆、问题**是一起塞给模型的**，而模型窗口有限
        （qwen-max 最大输入 30,720）。实测最坏情况（5 个表格块 × 2048 + 10 轮 ×
        （答 2048 + 问 2000））≈ 4 万 token ⇒ 不设上限会直接超窗，且原先**没有任何兜底**。

        **牺牲顺序**（先丢谁）：① 最旧的轮次 → ② 排名最低的块 → ③ 截断最后剩下的块。
        理由：资料是答案的**唯一依据**（忠实度校验盯着它），记忆只是辅助；而排名最低的块
        本来就在 `keep` 边缘。**永远保留**：最近 1 轮 + 至少 1 个块（哪怕被截断）。
        """
        budget = self._context_budget()
        stats: dict[str, Any] = {
            "budget": budget, "dropped_chunks": 0, "dropped_history": 0,
            "truncated": False, "prompt_tokens": 0,
        }
        kept_chunks = list(chunks)
        kept_history = list(history)
        if not kept_chunks:
            return kept_chunks, kept_history, stats

        # 固定开销：系统提示（不含资料，调用方已渲染好）+ 问题 + 一点格式余量
        # 注意**不要在模板上做 .format()** —— 取数提示词里有 JSON 花括号，会被当成占位符
        base = int(system_tokens) + count_tokens(question) + 64

        def total() -> int:
            return (
                base
                + count_tokens(self.build_context(kept_chunks))
                + sum(count_tokens(str(m.get("content") or "")) for m in kept_history)
            )

        stats["prompt_tokens"] = total()
        if budget <= 0:
            return kept_chunks, kept_history, stats

        # ① 丢最旧的轮次（按"轮"成对丢，避免把 user/assistant 拆散）
        while kept_history and total() > budget:
            step = 2 if len(kept_history) >= 2 else 1
            kept_history = kept_history[step:]
            stats["dropped_history"] += step
        # ② 丢排名最低的块（保留至少 1 个）
        while len(kept_chunks) > 1 and total() > budget:
            kept_chunks.pop()
            stats["dropped_chunks"] += 1
        # ③ 只剩 1 个块仍超预算 → 截断正文（保开头，明确标注，别让模型以为内容就这些）
        while kept_chunks and total() > budget:
            body = kept_chunks[-1].content
            if count_tokens(body) <= _BUDGET_MIN_CHUNK_TOKENS:
                break
            shorter = body[: max(64, int(len(body) * 0.75))]
            if len(shorter) >= len(body):
                break
            kept_chunks[-1] = replace(
                kept_chunks[-1], content=shorter + "\n…（内容超上下文预算，已截断）"
            )
            stats["truncated"] = True
        stats["prompt_tokens"] = total()
        if stats["dropped_chunks"] or stats["dropped_history"] or stats["truncated"]:
            log.warning("prompt_budget_trimmed", **stats)
        return kept_chunks, kept_history, stats

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

        # **上下文预算**：资料 + 记忆 + 问题一起裁（保证至少 1 块 + 最近 1 轮）
        chunks, history, budget = self._fit_prompt(
            chunks, history, question,
            system_tokens=count_tokens(SYSTEM_PROMPT.format(context="")),
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
        finish_reason = str(getattr(llm_result, "finish_reason", "") or "")

        answer_text, hallucinated = self._drop_hallucinated(llm_result.text, len(chunks))
        # L4 输出侧检测：答案里不得出现当前用户看不到的文档名（04 §6.4）
        answer_text, leaked_titles = self._apply_output_guard(answer_text, perm)
        if hallucinated:
            self.gate.record_security_event(
                "hallucinated_citation",
                user_id=perm.user_id,
                severity="medium",
                detail={"indices": hallucinated, "model": llm_result.model},
            )

        total = _ms(started)
        timing_kw = dict(
            ttft_ms=int(_ms(llm_started, llm_done)),
            total_ms=total,
            prompt_tokens=llm_result.prompt_tokens,
            completion_tokens=llm_result.completion_tokens,
        )
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
                **timing_kw,
            )

        # P0 忠实度：无 [n] 不成溯源；数字/量词须落在入选分块原文中
        #
        # 分级处理（实测教训：`2026年7月能耗分析` 这类**表格汇总**问题，模型会做单位换算与
        # 格式化，旧的"一条对不上就整答拒答 + 清空引用"会把好答案打成拒答 —— 25 条 claim
        # 误杀 9 条，用户看到的是"答不出来"，而检索其实完全正常）：
        #   * `missing_citation`：答案根本没标 [n] → **仍然硬拒**（不允许假溯源）；
        #   * `unsupported_claim` 且占比过高（`mostly_unsupported`）→ **仍然硬拒**（大概整篇是编的）；
        #   * `unsupported_claim` 占比不高 → **降级**：答案与引用都保留，标 degraded、
        #     写审计（`faithfulness_degraded`），由界面提示"部分数字未能逐字核对"。
        report = assess_faithfulness(answer_text, chunks)
        degraded: list[str] = (
            [f"output_title_masked:{len(leaked_titles)}"] if leaked_titles else []
        )
        if not report.ok:
            hard_refuse = report.reason == REFUSE_MISSING_CITATION or mostly_unsupported(report)
            self.gate.record_security_event(
                "faithfulness_refused" if hard_refuse else "faithfulness_degraded",
                user_id=perm.user_id,
                severity="medium" if hard_refuse else "low",
                detail={
                    "reason": report.reason,
                    "unsupported": report.unsupported[:10],
                    "claims": len(report.claims),
                    "model": llm_result.model,
                },
            )
            if hard_refuse:
                return AnswerResult(
                    answer=answer_text,
                    citations=[],
                    retrieval=retrieval,
                    model=llm_result.model,
                    refused=True,
                    refusal_reason=report.reason,
                    hallucinated_citations=hallucinated,
                    faithfulness=report.as_dict(),
                    degraded=degraded,
                    **timing_kw,
                )
            degraded.append(f"{DEGRADED_UNSUPPORTED}:{len(report.unsupported)}")

        # 答案里的「第X条/章」必须出现在被引用块的标题路径里，写错条号就整答拒掉。
        mismatched = clause_mismatches(answer_text, chunks)
        if mismatched:
            self.gate.record_security_event(
                "clause_mismatch",
                user_id=perm.user_id,
                severity="medium",
                detail={"clauses": mismatched[:8], "model": llm_result.model},
            )
            return AnswerResult(
                answer=f"{NO_BASIS_MARKER}。",
                citations=[],
                retrieval=retrieval,
                model=llm_result.model,
                refused=True,
                refusal_reason="clause_mismatch",
                hallucinated_citations=hallucinated,
                faithfulness=report.as_dict(),
                degraded=degraded,
                **timing_kw,
            )

        verdict = await self._verify_answer(question, answer_text, chunks, retrieval)
        if verdict is not None and verdict.supported is not True:
            cfg = self.settings.answer_verify
            # supported=None 表示判官不可用；fail_open 时放行，否则按"不可用"拒答
            if verdict.supported is False or not cfg.fail_open:
                reason = REFUSE_ANSWER_VERIFY if verdict.supported is False else REFUSE_ANSWER_VERIFY_UNAVAILABLE
                self.gate.record_security_event(
                    "answer_verify_refused",
                    user_id=perm.user_id,
                    severity="medium" if verdict.supported is False else "low",
                    detail={
                        "reason": reason,
                        "verifier_reason": verdict.reason,
                        "error": verdict.error,
                        "top_score": retrieval.top_score,
                        "model": verdict.model,
                    },
                )
                timing_kw["total_ms"] = int(_ms(started))
                return AnswerResult(
                    answer=f"{NO_BASIS_MARKER}。",
                    citations=[],
                    retrieval=retrieval,
                    model=llm_result.model,
                    refused=True,
                    refusal_reason=reason,
                    hallucinated_citations=hallucinated,
                    faithfulness=report.as_dict(),
                    degraded=degraded,
                    **timing_kw,
                )

        citations = self._post_check_citations(answer_text, chunks, perm)
        # 输出截断：撞到 max_tokens 上限时答案本身不完整，必须明确告诉用户，
        # 不能把半截答案伪装成完整答案（评测与用户都会被骗）。
        truncated = finish_reason == "length"
        if truncated:
            answer_text = f"{answer_text}\n\n{TRUNCATION_NOTICE}"
            degraded.append("output_truncated")
            log.warning(
                "answer_truncated",
                model=llm_result.model,
                completion_tokens=llm_result.completion_tokens,
            )
        return AnswerResult(
            answer=answer_text,
            citations=citations,
            retrieval=retrieval,
            model=llm_result.model,
            refused=False,
            hallucinated_citations=hallucinated,
            faithfulness=report.as_dict(),
            degraded=degraded,
            dropped_chunks=int(budget["dropped_chunks"]),
            dropped_history=int(budget["dropped_history"]),
            context_truncated=bool(budget["truncated"]),
            truncated=truncated,
            **timing_kw,
        )

    async def _verify_answer(
        self,
        question: str,
        answer_text: str,
        chunks: Sequence[Any],
        retrieval: RetrievalResult,
    ) -> VerifyResult | None:
        """按配置对"边界分"答案做资料充分性核验；返回 None 表示不需要核验。"""
        cfg = self.settings.answer_verify
        if not cfg.enabled:
            return None
        score = retrieval.top_score
        if score is None or not (cfg.min_score <= float(score) <= cfg.max_score):
            return None
        try:
            return await self.answer_verifier.verify(
                question, answer_text, [str(getattr(c, "content", "")) for c in chunks]
            )
        except Exception as exc:  # noqa: BLE001 - 门禁失败不能拖垮问答
            log.warning("answer_verify_failed", error=str(exc))
            return VerifyResult(supported=None, error=f"{type(exc).__name__}: {exc}")

    async def _answer_numeric(
        self, question: str, retrieval: RetrievalResult, perm: PermContext
    ) -> AnswerResult:
        """**取数模式**：类型锁死 + 抄写校验（口径同课程 19，见 :mod:`ekbs.retrieval.numeric`）。

        与业务问答（:meth:`generate`）的三处**刻意差异**：

        1. 用**独立提示词** `NUMERIC_SYSTEM_PROMPT` —— 全局 `SYSTEM_PROMPT` 一个字不动，
           否则"宁可 N/A 也不解释"的铁律会污染业务问答；
        2. **不跑自由文本的忠实度校验** `assess_faithfulness`：那是"事后拿规则对账"，
           对结构化取数值不适用；取而代之的是**更强的抄写校验**（值必须逐字来自它引用的
           原文片段）。两者互斥但不等价 —— 后者是结构验证，强度更高；
        3. 拒答语义与业务问答**完全一致**：`refused=True` + `citations=[]`
           （课程防线 7 与我们既有的"拒答必清引用"是同一条，不新增状态）。
        """
        chunks = retrieval.chunks
        model_ident = self.providers.llm.ref.ident
        if not chunks:
            return AnswerResult(
                answer="未找到可靠依据。",
                citations=[],
                retrieval=retrieval,
                model=model_ident,
                refused=True,
                refusal_reason=retrieval.refusal_reason or REFUSE_EMPTY_ANSWER,
            )

        chunks, _numeric_history, _numeric_budget = self._fit_prompt(
            chunks, (), question,
            system_tokens=count_tokens(build_numeric_prompt("")),
        )
        if _numeric_budget["dropped_chunks"] or _numeric_budget["truncated"]:
            log.warning("prompt_budget_trimmed_numeric", **_numeric_budget)
        messages = [
            {
                "role": "system",
                "content": build_numeric_prompt(self.build_context(chunks)),
            },
            {"role": "user", "content": question},
        ]
        started = time.perf_counter()
        # JSON 模式：让服务端约束输出形态（课程防线 2/5 的做法）。取数模式**只在**这里用它，
        # 业务问答不带该字段 —— 请求体逐字不变。
        response_format = {"type": "json_object"}
        try:
            llm_result = await self.providers.llm.generate(
                messages, response_format=response_format
            )
        except ModelUnavailable as exc:
            log.error("numeric_llm_failed", error=str(exc))
            return AnswerResult(
                answer="生成服务暂时不可用，请稍后重试。",
                citations=[],
                retrieval=retrieval,
                model=model_ident,
                refused=True,
                refusal_reason="llm_unavailable",
            )
        numeric = parse_numeric_payload(llm_result.text)
        if numeric.parse_error:
            # 一次修复重试（课程防线 5 的"二次重解析"）：**只修格式，不重新取数**。
            # 只在"格式失败"时触发 —— 模型按规则返回 N/A 时不重试（重试无意义且多烧一次调用）。
            log.warning("numeric_parse_retry", model=llm_result.model, detail=numeric.reason)
            retry_messages = [
                *messages,
                {
                    "role": "user",
                    "content": "上次的输出不是合法 JSON。只输出一个 JSON 对象，不要任何其它文字或代码块。",
                },
            ]
            try:
                retry_result = await self.providers.llm.generate(
                    retry_messages, response_format=response_format
                )
            except ModelUnavailable as exc:
                log.warning("numeric_parse_retry_failed", error=str(exc)[:120])
            else:
                retried = parse_numeric_payload(retry_result.text)
                if not retried.parse_error:
                    llm_result = retry_result
                    numeric = retried
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        def _refuse(
            reason: str, detail: dict[str, Any], *, answer_text: str = "未找到可靠依据。"
        ) -> AnswerResult:
            """取数失败一律走这里：**答案与引用同时清空**，不留"有答案没出处"的中间态。"""
            self.gate.record_security_event(
                "numeric_refused",
                user_id=perm.user_id,
                severity="low",
                detail={"reason": reason, "model": llm_result.model, **detail},
            )
            return AnswerResult(
                answer=answer_text,
                citations=[],
                retrieval=retrieval,
                model=llm_result.model,
                refused=True,
                refusal_reason=reason,
                numeric=numeric.as_dict(),
                total_ms=elapsed_ms,
                prompt_tokens=llm_result.prompt_tokens,
                completion_tokens=llm_result.completion_tokens,
            )

        if numeric.is_na:
            # 格式失败（重试也没修好）与"资料没有该指标"共用拒答，但 reason 里如实区分
            return _refuse("numeric_na", {"why": numeric.reason[:120], "parse_error": numeric.parse_error})

        verified, cite_index = verify_copied(numeric, chunks)
        if not verified:
            # 值不在它引用的片段里 → 要么是编的、要么是算出来的（课程两条铁律都不许）
            return _refuse(
                "numeric_unverified",
                {"value": numeric.value, "quote": numeric.quote[:60]},
            )

        # 口径（年份/期间/表名）必须也能从引用的分块里逐字核到 —— 编出来的口径
        # 等于"答非所问还显得权威"，比错的数字更难被发现（README §四 第 79 条）
        if not verify_scope(numeric, chunks, cite_index):
            return _refuse(
                "numeric_invalid_scope",
                {"scope": numeric.scope[:40], "quote": numeric.quote[:60]},
            )

        numeric.verified = True
        numeric.cite_index = cite_index
        # P1：同标签多口径且问题未钉死年份/表名 → 拒答并反问（消掉「同一问两个真数」）
        alternatives = find_scope_alternatives(
            chunks, quote=numeric.quote, value=numeric.value
        )
        if alternatives and not question_pins_scope(question, numeric.scope):
            numeric.alternatives = alternatives
            clarify = format_scope_clarify_reply(numeric, alternatives)
            return _refuse(
                "numeric_scope_ambiguous",
                {"alternatives": len(alternatives)},
                answer_text=clarify,
            )

        answer_text, _leaked = self._apply_output_guard(
            format_numeric_user_reply(numeric, cite_index), perm
        )
        # 复用既有引用路径：ACL 二次确认 + 页码区间校验，与业务问答同一套（不另写一份）
        citations = self._post_check_citations(answer_text, chunks, perm)
        return AnswerResult(
            answer=answer_text,
            citations=citations,
            retrieval=retrieval,
            model=llm_result.model,
            numeric=numeric.as_dict(),
            # 取数模式的"忠实度"由抄写校验承担：显式写清口径，
            # 免得调试台按业务问答的面板显示成"未通过"（那是另一套判据）
            faithfulness={
                "ok": True,
                "reason": "numeric_copy_verified",
                "cited_indices": [cite_index] if cite_index else [],
                "claims": [],
                "unsupported": [],
            },
            total_ms=elapsed_ms,
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
        """引用后置校验（`00` §2.2、`docs/spec/10` §8）：编号存在 + ACL 二次确认 + 页码区间。

        **P0**：答案里没有 ``[n]`` 时返回空列表 —— 禁止「没标出处却挂上全部候选」的假溯源。
        """
        if not answer.strip():
            return []
        cited_indices = {int(m.group(1)) for m in _CITATION_RE.finditer(answer)}
        if not cited_indices:
            return []
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

    @staticmethod
    def _drop_repeated_question_history(
        messages: list[dict[str, Any]], question: str
    ) -> list[dict[str, Any]]:
        """同一问题重复问时，丢掉历史里该问题的旧问答，避免旧回答把模型带偏。"""
        def norm(text: Any) -> str:
            return re.sub(r"\s+|[，。；：,.;:!?！？]", "", str(text or ""))

        target = norm(question)
        if not target:
            return messages
        for i in range(len(messages) - 1, -1, -1):
            item = messages[i]
            if str(item.get("role")) == "user" and norm(item.get("content")) == target:
                return messages[:i]
        return messages

    async def answer(
        self,
        question: str,
        perm: PermContext,
        *,
        history: Sequence[dict[str, str]] = (),
        conversation_id: str | None = None,
        use_cache: bool = True,
        allow_generation: bool = True,
        overrides: RetrievalOverrides | None = None,
        collect_debug: bool = False,
        mode: str = MODE_AUTO,
        fresh_on_repeat: bool = False,
    ) -> AnswerResult:
        """检索 + 生成的一条龙（供 API 与钉钉适配器调用）。

        ``allow_generation=False``：内容安全闸门判定为「疑似提示词注入」时的降级路径 ——
        **仍然做检索并返回可溯源引用，但不调用 LLM**（`04` §6.4 的 L3 层）。


        多轮时：先把会话历史取出来（``ekbs_session_ctx``，键含用户维度）→ **查询改写**
        → 用改写后的查询检索 → 生成（历史只用于改写，不进生成 prompt）→ 把本轮写回上下文。
        每轮都重新加载 ``PermContext`` 由调用方负责（`03` §6.1 V5）。

        ``mode``：``auto``（默认，按意图 + ``qa.auto_numeric`` 路由）|
        ``prose`` / ``numeric``（显式覆盖，管理台用）。

        ``overrides`` / ``collect_debug``：检索调试台用；正式问答保持默认即可。
        """
        # ① 会话历史：调用方显式给了就用给的，否则按 (conversation_id, 用户) 取
        messages = list(history)
        if not messages and self.sessions is not None and conversation_id:
            messages = await asyncio.to_thread(
                self.sessions.load, conversation_id, perm.dingtalk_userid
            )
        # 重复问题：钉钉等长会话入口可要求不让该问题的旧回答参与本轮生成
        # （修复后重问/换模式场景）；默认保留历史，以兼容“同一会话追问”的缓存契约。
        if fresh_on_repeat:
            messages = self._drop_repeated_question_history(messages, question)

        # ② 查询改写（00 §2.1 的第一步）；改写后的查询才是检索输入
        rewrite = await self._rewrite(question, messages)
        intent = str(getattr(rewrite, "intent", "factual") or "factual")
        auto_numeric = bool((getattr(self.settings, "qa", None) or {}).get("auto_numeric", True))
        mode_used = resolve_answer_mode(mode, intent, auto_numeric=auto_numeric)
        # ②.5 多意图拆分（P0）：一次问多个问题时拆成子问。
        # 单问（默认）→ sub_questions 只有 1 个，下面走与原实现完全一致的链路。
        sub_questions: list[SubQuestion] = self.intent_splitter.split(question)
        multi_intent = len(sub_questions) > 1
        numeric_overridden = False
        if multi_intent and mode_used == MODE_NUMERIC:
            # 多子问合并回答属 prose 语义；取数模式是单问题语义。覆盖必须留痕，便于排障。
            numeric_overridden = True
            mode_used = MODE_PROSE
        sub_key = (
            "|subq=" + "|".join(sq.text for sq in sub_questions) if multi_intent else ""
        )
        # 会话历史**只**服务于查询改写（帮向量/嵌入模型判定意图），绝不进生成 prompt：
        # 生成只吃「改写后查询召回的 chunks + 用户原问题」，钉钉与 UI 用同一份 prompt。
        cached = None
        # 答案缓存键以**改写后的查询**分区：历史只影响改写，追问/指代的上下文已经被
        # 并进 `rewrite.rewritten`，所以不同上下文自然落到不同键（C2 的同类问题）。
        # 键还要含**检索配置版本**：只按 (权限指纹, 问题) 建键时，改了 min_score /
        # 换精排模型 / 开关 rerank 之后，旧答案会继续被服务到 TTL 到期。
        # 实测踩到：阈值从 0.30 改到 0.60 后，同一个问题仍在 3ms 内返回旧语义的
        # `refused=false + 5 条引用 + 答案「未找到可靠依据」`。
        cache_key = CacheStore.answer_key(
            perm.fingerprint,
            f"{rewrite.rewritten}{sub_key}|gen={int(allow_generation)}"
            # 模式必须进键：取数模式与业务问答的答案形态完全不同，
            # 用解析后的 mode_used（auto→prose/numeric），避免同意图重复缓存两套键。
            f"|mode={mode_used}|rc={self.retrieval_cache_revision()}|idx={self.index_cache_revision()}",
        )
        if use_cache and not collect_debug:
            cached = await self.cache.get(cache_key)
        if cached:
            result = self._answer_from_cache(cached, rewrite, mode_used, intent)
            # 命中缓存也要记这一轮上下文：否则多轮追问会丢掉这一轮（实测踩到）
            await self._remember_turn(conversation_id, perm, question, result)
            return result

        retrieval, intent_hits, per_sub = await self._retrieve_for_answer(
            rewrite=rewrite,
            sub_questions=sub_questions,
            multi_intent=multi_intent,
            messages=messages,
            perm=perm,
            overrides=overrides,
            collect_debug=collect_debug,
        )
        if allow_generation and mode_used == MODE_NUMERIC:
            result = await self._answer_numeric(question, retrieval, perm)
        elif allow_generation and multi_intent and per_sub:
            # 多问题：逐子问生成再拼接（共享 2048 输出上限会让后面的问题被略过）
            result = await self._answer_multi_intent(sub_questions, per_sub, retrieval, perm)
        elif allow_generation:
            result = await self.generate(question, retrieval, perm)
        else:
            result = self._retrieval_only_answer(question, retrieval, perm)
        self._annotate_result(
            result,
            rewrite,
            sub_questions,
            multi_intent,
            intent_hits,
            retrieval,
            mode_used=mode_used,
            intent=intent,
            numeric_overridden=numeric_overridden,
        )
        self._apply_figure_adjustments(result, retrieval, intent=intent, question=question)

        # ③ 写回上下文（群聊按用户隔离，见 session.py 的说明）
        await self._remember_turn(conversation_id, perm, question, result)

        if use_cache and not collect_debug and result.answer and not result.refused:
            await self.cache.set(
                cache_key,
                self._answer_cache_payload(result, mode_used, intent),
                ttl=self.settings.retrieval.cache.answer_ttl,
            )
        return result

    # ------------------------------------------------------------------
    # answer() 的后处理（缓存命中 / 结果标注 / 找图修正 / 缓存写回）
    # ------------------------------------------------------------------

    def _answer_from_cache(
        self, cached: dict[str, Any], rewrite: Any, mode_used: str, intent: str
    ) -> AnswerResult:
        """用答案缓存重建 ``AnswerResult``（缓存命中路径）。"""
        retrieval = RetrievalResult(
            query=rewrite.rewritten, chunks=[], refused=False,
            timings_ms={"embed": 0.0, "recall": 0.0, "rerank": 0.0, "expand": 0.0, "total": 0.0},
        )
        citations = [Citation(**item) for item in cached.get("citations", [])]
        return AnswerResult(
            answer=str(cached.get("answer", "")),
            citations=citations,
            retrieval=retrieval,
            model=str(cached.get("model", "")),
            rewritten_query=str(cached.get("rewritten_query") or rewrite.rewritten),
            history_used=rewrite.used_history,
            refused=bool(cached.get("refused", False)),
            refusal_reason=str(cached.get("refusal_reason", "")),
            numeric=cached.get("numeric"),
            mode_used=str(cached.get("mode_used") or mode_used),
            intent=str(cached.get("intent") or intent),
            figure_attachments=list(cached.get("figure_attachments") or []),
            sub_questions=list(cached.get("sub_questions") or []),
            coverage=dict(cached.get("coverage") or {}),
            degraded=["answer_cache_hit"],
        )

    def _annotate_result(
        self,
        result: AnswerResult,
        rewrite: Any,
        sub_questions: Sequence[SubQuestion],
        multi_intent: bool,
        intent_hits: dict[int, set[int]],
        retrieval: RetrievalResult,
        *,
        mode_used: str,
        intent: str,
        numeric_overridden: bool,
    ) -> None:
        """把改写/意图/子问覆盖等元信息写回结果。"""
        result.rewritten_query = (
            " ; ".join(sq.rewritten for sq in sub_questions) if multi_intent else rewrite.rewritten
        )
        result.history_used = rewrite.used_history
        result.mode_used = mode_used
        result.intent = intent
        result.sub_questions = (
            [
                {"index": sq.index, "text": sq.text, "rewritten": sq.rewritten, "source": sq.source}
                for sq in sub_questions
            ]
            if multi_intent
            else []
        )
        result.coverage = (
            self._coverage(sub_questions, retrieval, intent_hits) if multi_intent else {}
        )
        if numeric_overridden:
            result.degraded.append("multi_intent_numeric_to_prose")

    def _apply_figure_adjustments(
        self, result: AnswerResult, retrieval: RetrievalResult, *, intent: str, question: str
    ) -> None:
        """找图意图的后处理：补/清洗引用，并把「资料区无像素」的口头拒答纠正为已找到原图。"""
        result.figure_attachments = self._collect_figure_attachments(
            retrieval.chunks,
            result.citations,
            intent=intent,
            query=result.rewritten_query or question,
        )
        # 找图意图：引用里的空白垃圾图（空调合同扫描页等）不得留在答案卡片上
        if intent == "figure" and result.figure_attachments:
            keep_ids = {int(a["chunk_id"]) for a in result.figure_attachments}
            cleaned: list[Citation] = []
            for cite in result.citations:
                is_fig = bool(cite.figure_id or cite.image_path)
                if is_fig and int(cite.chunk_id) not in keep_ids:
                    continue
                cleaned.append(cite)
            if cleaned:
                for i, cite in enumerate(cleaned, 1):
                    cite.cite_index = i
                result.citations = cleaned
        # 找图意图且已筛出相关原图时：模型常因「资料区没有像素」口头拒答；
        # 实际发图走 figure_attachments，不应再当成无依据拒答。
        if (
            intent == "figure"
            and result.figure_attachments
            and result.refused
            and result.refusal_reason in {REFUSE_NO_BASIS, REFUSE_EMPTY_ANSWER, ""}
        ):
            titles = sorted(
                {
                    str(a.get("doc_title") or "").strip()
                    for a in result.figure_attachments
                    if str(a.get("doc_title") or "").strip()
                }
            )
            doc_hint = f"（来自{'、'.join(titles[:2])}）" if titles else ""
            result.answer = f"已找到相关图片{doc_hint}，见下方原图。"
            result.refused = False
            result.refusal_reason = ""
            if not result.citations:
                # 用附件对应块补最小引用，便于调试台/钉钉对齐
                by_id = {c.chunk_id: c for c in retrieval.chunks}
                rebuilt: list[Citation] = []
                for i, att in enumerate(result.figure_attachments, 1):
                    src = by_id.get(int(att.get("chunk_id") or 0))
                    if src is None:
                        continue
                    rebuilt.append(
                        Citation(
                            cite_index=i,
                            chunk_id=int(src.chunk_id),
                            doc_id=int(src.doc_id),
                            doc_title=str(src.doc_title or ""),
                            doc_version=str(getattr(src, "doc_version", None) or "v1"),
                            heading_path=str(src.heading_path or ""),
                            page_from=src.page_from,
                            page_to=src.page_to,
                            rerank_score=src.rerank_score,
                            acl_result="pass",
                            page_check="pass",
                            figure_id=src.figure_id,
                            image_path=src.image_path,
                        )
                    )
                result.citations = rebuilt

    @staticmethod
    def _answer_cache_payload(result: AnswerResult, mode_used: str, intent: str) -> dict[str, Any]:
        """答案缓存落库载荷（字段变更需同步 ``_answer_from_cache``）。"""
        return {
            "answer": result.answer,
            "model": result.model,
            "refused": result.refused,
            "refusal_reason": result.refusal_reason,
            "citations": [c.__dict__ for c in result.citations],
            "numeric": result.numeric,
            "mode_used": mode_used,
            "intent": intent,
            "figure_attachments": result.figure_attachments,
            "rewritten_query": result.rewritten_query,
            "sub_questions": result.sub_questions,
            "coverage": result.coverage,
        }

    @staticmethod
    def _figure_is_junk(content: str, heading_path: str = "") -> bool:
        """多模态旁路常把扫描件空白页当「图」召回；这类不得回源。"""
        blob = f"{content or ''}\n{heading_path or ''}"
        return any(
            mark in blob
            for mark in ("无可读文字", "纯白背景", "纯白色的背景", "没有任何可识别")
        )

    @staticmethod
    def _figure_query_score(
        *,
        query: str,
        doc_title: str,
        content: str,
        text_hit_docs: set[int],
        doc_id: int,
    ) -> int:
        """找图回源排序：正文命中文档 > 题注/标题与问句重合 > 垃圾图垫底。"""
        q = (query or "").strip().lower()
        title = (doc_title or "").strip().lower()
        body = (content or "").strip().lower()
        score = 0
        if doc_id in text_hit_docs:
            score += 100
        for n in (4, 3, 2):
            for i in range(0, max(0, len(q) - n + 1)):
                gram = q[i : i + n]
                if gram in title:
                    score += 5 * n
                if gram in body:
                    score += 3 * n
        if RetrievalPipeline._figure_is_junk(content):
            score -= 200
        return score

    @staticmethod
    def _collect_figure_attachments(
        chunks: Sequence[RetrievedChunk],
        citations: Sequence[Citation],
        *,
        intent: str,
        query: str = "",
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """汇总要回源的图片：找图意图下按问句相关性排序，并丢掉空白垃圾图。"""
        by_id = {c.chunk_id: c for c in chunks}
        # 正文/表格命中的文档：二维码问句通常先命中「首次登录」段，再扩同文档图
        text_hit_docs = {
            int(c.doc_id)
            for c in chunks
            if c.chunk_type not in {"figure"} and not c.figure_id
        }
        candidates: list[tuple[int, dict[str, Any], int]] = []
        seen: set[int] = set()

        def _push(chunk_id: int, figure_id: str, image_path: str, doc_title: str, doc_id: int, content: str) -> None:
            if not image_path or chunk_id in seen:
                return
            seen.add(chunk_id)
            score = RetrievalPipeline._figure_query_score(
                query=query,
                doc_title=doc_title,
                content=content,
                text_hit_docs=text_hit_docs,
                doc_id=int(doc_id or 0),
            )
            candidates.append(
                (
                    score,
                    {
                        "chunk_id": chunk_id,
                        "figure_id": figure_id or "",
                        "image_path": image_path,
                        "doc_title": doc_title or "",
                    },
                    int(doc_id or 0),
                )
            )

        for cite in citations:
            path = str(getattr(cite, "image_path", None) or "").strip()
            fid = str(getattr(cite, "figure_id", None) or "").strip()
            if not path and not fid:
                continue
            src = by_id.get(int(cite.chunk_id))
            content = str(getattr(src, "content", None) or getattr(cite, "heading_path", None) or "")
            _push(
                int(cite.chunk_id),
                fid or (src.figure_id if src else ""),
                path or (str(src.image_path or "") if src else ""),
                str(getattr(cite, "doc_title", None) or (src.doc_title if src else "")),
                int(getattr(cite, "doc_id", None) or (src.doc_id if src else 0) or 0),
                content,
            )

        if (intent or "").strip().lower() == "figure":
            for chunk in chunks:
                if chunk.chunk_type != "figure" and not chunk.figure_id:
                    continue
                _push(
                    int(chunk.chunk_id),
                    str(chunk.figure_id or ""),
                    str(chunk.image_path or ""),
                    str(chunk.doc_title or ""),
                    int(chunk.doc_id or 0),
                    str(chunk.content or chunk.heading_path or ""),
                )

        if not candidates:
            return []

        candidates.sort(key=lambda item: (-item[0], item[1]["chunk_id"]))
        # 有正相关图时丢掉垃圾图；全是垃圾则不回源（宁可不发，也不发错文档）
        positive = [c for c in candidates if c[0] > 0]
        pool = positive if positive else []
        if not pool:
            return []

        # 问句能对上某文档标题时，只发这些文档的图（「熙物联」不夹带空调合同）
        q = (query or "").strip().lower()
        title_matched_docs = {
            doc_id
            for score, att, doc_id in pool
            if doc_id
            and any(
                q[i : i + n] in (att.get("doc_title") or "").lower()
                for n in (4, 3, 2)
                for i in range(0, max(0, len(q) - n + 1))
            )
        }
        if title_matched_docs:
            pool = [c for c in pool if c[2] in title_matched_docs] or pool
        elif text_hit_docs:
            anchored = [c for c in pool if c[2] in text_hit_docs]
            if anchored:
                pool = anchored

        return [att for _, att, _ in pool[:limit]]

    async def _rewrite(self, question: str, messages: Sequence[dict[str, str]]) -> Any:
        if self.rewriter is None:
            from ekbs.session import QueryRewriter, RewriteResult

            # 改写关闭时仍要做意图识别，否则 mode=auto 永远落不到 numeric
            result = RewriteResult(question, question, "off", used_history=False)
            result.intent = QueryRewriter.detect_intent(question)
            return result
        return await self.rewriter.rewrite(question, messages)

    # ------------------------------------------------------------------
    # 多意图检索（P0+P1+P2）
    # ------------------------------------------------------------------

    async def _retrieve_multi_intent(
        self,
        sub_questions: Sequence[SubQuestion],
        messages: Sequence[dict[str, str]],
        perm: PermContext,
        *,
        overrides: RetrievalOverrides | None = None,
        collect_debug: bool = False,
    ) -> tuple[RetrievalResult, dict[int, set[int]], list[tuple[int, RetrievalResult]]]:
        """多子问：每子问独立改写 + 独立召回，再合并去重 + 配额选择（P1+P2）。

        ``collect_debug`` 只保留给未来调试扩展；子问级召回默认不收集分阶段明细，
        避免 N 倍调试开销。返回 ``(合并结果, chunk_id → 命中它的子问集合, 各子问结果)``
        —— 各子问结果供"逐子问生成"使用（见 :meth:`_answer_multi_intent`）。
        """
        per_sub: list[tuple[int, RetrievalResult]] = []
        for sq in sub_questions:
            sub_rewrite = await self._rewrite(sq.text, messages)
            sq.rewritten = str(getattr(sub_rewrite, "rewritten", sq.text) or sq.text)
            result = await self.retrieve(
                sq.rewritten, perm, overrides=overrides, collect_debug=False
            )
            per_sub.append((sq.index, result))
        merged, hit_by = self._merge_retrievals(per_sub)
        return merged, hit_by, per_sub

    async def _retrieve_for_answer(
        self,
        *,
        rewrite: Any,
        sub_questions: Sequence[SubQuestion],
        multi_intent: bool,
        messages: Sequence[dict[str, str]],
        perm: PermContext,
        overrides: RetrievalOverrides | None,
        collect_debug: bool,
    ) -> tuple[RetrievalResult, dict[int, set[int]], list[tuple[int, RetrievalResult]]]:
        """单问走原链路（与改动前逐字节一致）；多子问走独立召回 + 配额合并。"""
        if not multi_intent:
            return (
                await self.retrieve(
                    rewrite.rewritten, perm, overrides=overrides, collect_debug=collect_debug
                ),
                {},
                [],
            )
        return await self._retrieve_multi_intent(
            sub_questions, messages, perm, overrides=overrides, collect_debug=collect_debug
        )

    async def _answer_multi_intent(
        self,
        sub_questions: Sequence[SubQuestion],
        per_sub: Sequence[tuple[int, RetrievalResult]],
        merged: RetrievalResult,
        perm: PermContext,
    ) -> AnswerResult:
        """多子问：**逐子问生成**再拼接。

        为什么不是"一次生成答所有子问"：输出上限 ``models.llm.max_tokens``（2048）是所有
        子问**共享**的 —— 问题一多，后面的子问会被模型略过或压缩，用户看到的是只答了
        前一半。逐子问生成让每个子问独占一次输出预算，证据也更聚焦。

        引用重编号：子问内部 ``[n]`` 对应它自己的块列表；拼成一份答案前必须映射成
        全局编号，否则每个子问都从 ``[1]`` 开始、引用列表对不上。
        """
        by_index = {index: result for index, result in per_sub}
        sections: list[str] = []
        global_index: dict[int, int] = {}
        citations: list[Citation] = []
        degraded: set[str] = set()
        refused_all = True
        truncated = False
        model = ""
        prompt_tokens = completion_tokens = total_ms = 0

        for order, sq in enumerate(sub_questions, start=1):
            result = by_index.get(sq.index)
            if result is None or not result.chunks:
                sections.append(f"{order}. {sq.text}\n未找到可靠依据。")
                continue
            part = await self.generate(sq.text, result, perm)
            model = model or part.model
            prompt_tokens += part.prompt_tokens
            completion_tokens += part.completion_tokens
            total_ms += part.total_ms
            truncated = truncated or part.truncated
            degraded.update(part.degraded)
            if not part.refused:
                refused_all = False

            local_chunks = {i + 1: chunk.chunk_id for i, chunk in enumerate(result.chunks)}

            def _renumber(match: re.Match[str], _map: dict[int, int] = local_chunks) -> str:
                chunk_id = _map.get(int(match.group(1)))
                if chunk_id is None:
                    return match.group(0)
                if chunk_id not in global_index:
                    global_index[chunk_id] = len(global_index) + 1
                return f"[{global_index[chunk_id]}]"

            text = _CITATION_RE.sub(_renumber, part.answer)
            sections.append(f"{order}. {sq.text}\n{text}")
            for cite in part.citations:
                mapped = global_index.get(cite.chunk_id)
                if mapped is not None:
                    citations.append(replace(cite, cite_index=mapped))

        citations.sort(key=lambda cite: cite.cite_index)
        answer = "\n\n".join(sections).strip() or "未找到可靠依据。"
        return AnswerResult(
            answer=answer,
            citations=citations,
            retrieval=merged,
            model=model,
            refused=refused_all,
            refusal_reason=(
                "" if not refused_all else (merged.refusal_reason or REFUSE_NO_BASIS)
            ),
            degraded=sorted(degraded),
            truncated=truncated,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_ms=total_ms,
        )

    def _merge_retrievals(
        self, per_sub: Sequence[tuple[int, RetrievalResult]]
    ) -> tuple[RetrievalResult, dict[int, set[int]]]:
        """合并各子问结果：去重取最高分 + 每子问保底配额 + 全局补足 + 上限截断。"""
        cfg = self.settings.retrieval
        per_min = max(1, int(getattr(cfg.rerank, "per_question_min", 1) or 1))
        cap = max(per_min, int(getattr(cfg.rerank, "keep_cap", 15) or 15))

        pool: dict[int, RetrievedChunk] = {}
        hit_by: dict[int, set[int]] = {}
        for sub_index, result in per_sub:
            for chunk in result.chunks:
                existing = pool.get(chunk.chunk_id)
                if existing is None or (chunk.rerank_score or 0.0) > (existing.rerank_score or 0.0):
                    pool[chunk.chunk_id] = chunk
                hit_by.setdefault(chunk.chunk_id, set()).add(sub_index)

        # 每子问保底：各自 rerank 最高的 per_min 块
        selected_ids: list[int] = []
        seen: set[int] = set()
        for _sub_index, result in per_sub:
            ranked = sorted(result.chunks, key=lambda c: (c.rerank_score or 0.0), reverse=True)
            for chunk in ranked[:per_min]:
                if chunk.chunk_id not in seen:
                    seen.add(chunk.chunk_id)
                    selected_ids.append(chunk.chunk_id)

        # 剩余名额按全局 rerank 分补足
        remaining = sorted(pool.values(), key=lambda c: (c.rerank_score or 0.0), reverse=True)
        for chunk in remaining:
            if len(selected_ids) >= cap:
                break
            if chunk.chunk_id not in seen:
                seen.add(chunk.chunk_id)
                selected_ids.append(chunk.chunk_id)

        selected = [pool[cid] for cid in selected_ids[:cap]]
        top_score = max(
            (c.rerank_score for c in selected if c.rerank_score is not None), default=None
        )
        merged = RetrievalResult(
            query=" ; ".join(r.query for _, r in per_sub),
            chunks=selected,
            candidate_count=sum(r.candidate_count for _, r in per_sub),
            c5_dropped=sum(r.c5_dropped for _, r in per_sub),
            c5_pre_dropped=sum(r.c5_pre_dropped for _, r in per_sub),
            top_score=top_score,
            refused=not selected,
            refusal_reason=(
                "" if selected
                else (per_sub[0][1].refusal_reason if per_sub else REFUSE_NO_CANDIDATE)
            ),
            degraded=sorted({d for _, r in per_sub for d in r.degraded}),
            timings_ms={"total": sum(float(r.timings_ms.get("total", 0.0)) for _, r in per_sub)},
        )
        return merged, hit_by

    @staticmethod
    def _coverage(
        sub_questions: Sequence[SubQuestion],
        retrieval: RetrievalResult,
        intent_hits: dict[int, set[int]],
    ) -> dict[str, Any]:
        """统计每个子问是否被最终选中的块覆盖（P5 的前置可观测数据）。"""
        selected_ids = {c.chunk_id for c in retrieval.chunks}
        covered: list[str] = []
        uncovered: list[str] = []
        for sq in sub_questions:
            hit = any(sq.index in intent_hits.get(cid, set()) for cid in selected_ids)
            (covered if hit else uncovered).append(sq.text)
        return {
            "total": len(sub_questions),
            "covered": len(covered),
            "uncovered": uncovered,
        }

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

    def index_cache_revision(self) -> str:
        """数据/索引版本（进答案缓存键）：重入库/重建后自动失效，避免旧答案被缓存。"""
        import hashlib

        parts: list[str] = []
        try:
            with self.db.sync_connect() as conn:
                for row in conn.execute(
                    "SELECT shard_id, vector_count, updated_at FROM ekbs_shard ORDER BY shard_id"
                ):
                    parts.append(f"{row['shard_id']}:{row['vector_count']}:{row['updated_at']}")
                doc_row = conn.execute(
                    "SELECT MAX(updated_at) AS t FROM ekbs_document"
                ).fetchone()
                if doc_row and doc_row["t"]:
                    parts.append(f"doc:{doc_row['t']}")
        except Exception as exc:  # noqa: BLE001 - 版本计算失败只影响缓存，不应拖垮问答
            log.warning("index_cache_revision_failed", error=str(exc)[:120])
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]

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
            f":{cfg.rerank.keep}:{cfg.rerank.min_score}"
            f":{getattr(cfg.rerank, 'per_question_min', 1)}:{getattr(cfg.rerank, 'keep_cap', 15)}",
            f"llm={getattr(llm, 'ident', '')}",
            f"k={cfg.dense_top_k}:{cfg.sparse_top_k}:{cfg.rrf_k}:{int(cfg.parent_expand)}",
            f":mq={cfg.multi_query}:{getattr(cfg, 'dense_multi_query', 1)}",
            f"fusion={cfg.fusion}:{getattr(cfg, 'hybrid_alpha', 0.5)}",
            f"rw={getattr(cfg.query_rewrite, 'strategy', '')}",
            f"split={int(getattr(cfg.intent_split, 'enabled', False))}"
            f":{getattr(cfg.intent_split, 'strategy', '')}"
            f":{getattr(cfg.intent_split, 'max_questions', 0)}",
            f"expand={getattr(getattr(getattr(self.rewriter, 'expand_llm', None), 'ref', None), 'ident', '')}",
            f"prompt={PROMPT_REVISION}",
        ]
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _ms(start: float, end: float | None = None) -> float:
    return round(((end if end is not None else time.perf_counter()) - start) * 1000, 2)
