"""入库编排 —— 清洗 → 解析 → 分块 → 向量化 → 写索引 → 门禁发布（`docs/spec/20` §1）。

状态机（`ekbs_document.status`）：``pending → parsing → indexing → published``，
失败进 ``failed``，下线进 ``offline``。

**两道硬闸门在入库路径上必须生效**：

* 合规闸（`00` §7）：``permission.corpus_egress_ack=false`` 时禁止**真实语料**做云端嵌入；
* C5 闸（`00` C5）：``permission.l3_ingest_enabled=false`` 时禁止 L3 入库。

本模块的解析入口是 :mod:`ekbs.ingest.parser`（MinerU / Office / 纯文本），
因此「没有 MinerU 也能跑通入库 → 检索 → 问答」这条链，只是解析质量不同。
"""

from __future__ import annotations

import asyncio

import hashlib
from dataclasses import dataclass
from typing import Sequence

from ekbs.config import Settings
from ekbs.db import Database
from ekbs.errors import ConfigError
from ekbs.index.faiss_store import ShardIndexManager, ensure_shard_for_placement
from ekbs.index.figure_faiss import FigureIndex
from ekbs.index.fts import FtsIndex
from ekbs.index.shard import parse_shard_id, resolve_shard_for_document
from ekbs.ingest.code_chunker import chunk_code, display_source_path, is_code_source
from ekbs.ingest.retrievable import RETRIEVABLE_PREDICATE, retrievable_chunk_ids
from ekbs.ingest.clean import clean_markdown
from ekbs.ingest.chunker import (
    CHILD_MAX_TOKENS,
    CHILD_MIN_TOKENS,
    ChunkNode,
    ChunkingResult,
    FigureInput,
    TableInput,
    chunk_markdown,
    count_tokens,
    retrieval_text,
)
from ekbs.logging import get_logger
from ekbs.permission.router import bump_policy_version
from ekbs.provider.registry import Providers

log = get_logger(component="ingest")


@dataclass
class IngestResult:
    doc_id: int
    shard_id: str
    parent_count: int
    child_count: int
    vector_count: int
    status: str
    checksum: str
    figure_vector_count: int = 0
    #: 图片向量化**失败**的张数（重试后仍失败）—— 这些图片块会「存了但搜不到」，
    #: 必须能被上游看见（旧实现只记一条 warning，文档照样 published）
    figure_vectors_failed: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "doc_id": self.doc_id,
            "shard_id": self.shard_id,
            "parent_count": self.parent_count,
            "child_count": self.child_count,
            "vector_count": self.vector_count,
            "figure_vector_count": self.figure_vector_count,
            "figure_vectors_failed": self.figure_vectors_failed,
            "status": self.status,
            "checksum": self.checksum,
        }


class IngestPipeline:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        providers: Providers,
        shards: ShardIndexManager,
        fts: FtsIndex | None = None,
        figure_index: FigureIndex | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.providers = providers
        self.shards = shards
        self.fts = fts or FtsIndex(db)
        self.figure_index = figure_index

    # ------------------------------------------------------------------

    async def ingest_markdown(
        self,
        *,
        title: str,
        markdown: str,
        kb_id: int,
        level: int = 2,
        visible_dept_ids: Sequence[str] = (),
        source_path: str = "",
        doc_version: str = "v1",
        page_map: Sequence[tuple[int, int, int]] | None = None,
        real_corpus: bool = False,
        checksum: str | None = None,
        existing_doc_id: int | None = None,
        figures: Sequence[FigureInput] = (),
        tables: Sequence[TableInput] = (),
    ) -> IngestResult:
        """把一份 Markdown 入库（清洗与解析由调用方完成，见 :mod:`ekbs.ingest.parser`）。

        ``existing_doc_id``：复用已有文档行（``ekbs-worker`` 的「重新解析」路径，`01` §16）。
        会先删掉该文档的旧分块、旧 FTS 行**与旧向量**（见 :meth:`_replace_existing`）：
        分片自 ``IndexIDMap2`` 改造后支持 ``remove``，重解析不再留孤儿向量，
        因此**不需要**再跑 ``ekbs.tools.rebuild_shard`` 回收。
        """
        # 闸门①：合规（真实语料 + 云端嵌入 = 全量出网）
        self.settings.assert_compliance_gate(real_corpus=real_corpus)
        # 闸门②：L3/L4
        self.settings.assert_l3_ingest_allowed(level)
        if level == 4 and not self.settings.permission.l4_enabled:
            raise ConfigError("L4 不入库：permission.l4_enabled=false（00 D11）")

        placement = resolve_shard_for_document(
            level=level, visible_dept_ids=list(visible_dept_ids), settings=self.settings
        )
        shard_id = placement.primary_shard_id
        dept_code, shard_level = parse_shard_id(shard_id)
        ensure_shard_for_placement(
            self.shards,
            shard_id,
            level=shard_level,
            dept_code=dept_code,
            fingerprint=self.providers.embed.ref.fingerprint,
        )

        checksum = checksum or hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        # 清洗（`06` §1.7）——**显式一步**，放在 checksum 之后、分块之前：
        #   * checksum 仍基于解析原始产物 → 「秒传/去重」的语义不变；
        #   * 进分块、进 FTS、进向量的都是清洗后的文本（此前"清洗"没有独立实现，
        #     解析器吐出的 `$1 0 0 \%$` 这类 LaTeX 原样入库 → 关键词那一路搜不到）。
        cleaned = clean_markdown(markdown)
        if cleaned.stats:
            log.info("markdown_cleaned", title=title, **cleaned.stats)
        markdown = cleaned.text
        if existing_doc_id is not None:
            self._replace_existing(existing_doc_id, shard_id=shard_id)
            doc_id = int(existing_doc_id)
            self._update_document(
                doc_id, kb_id=kb_id, title=title, source_path=source_path, level=level,
                visible_dept_ids=list(visible_dept_ids), checksum=checksum,
                doc_version=doc_version, status="indexing",
            )
        else:
            doc_id = self._insert_document(
                kb_id=kb_id, title=title, source_path=source_path, level=level,
                visible_dept_ids=list(visible_dept_ids), checksum=checksum,
                doc_version=doc_version, status="indexing",
            )

        try:
            figure_vector_count = 0
            figure_vectors_failed = 0
            # 代码文件走**代码感知分块**（按函数/类切，path+符号进 heading_path）：
            # 定长/Markdown 分块会把函数从中间切断，代码检索效果很差。
            if is_code_source(source_path):
                # heading_path 里**不能**放机器绝对路径：路径本来就在磁盘上（source_path 列），
                # 放进每个块会让检索语义随机器/目录变化，还把服务器布局泄进引用。
                display_path = display_source_path(self.settings, source_path)
                chunking = ChunkingResult(
                    nodes=chunk_code(
                        markdown,
                        path=display_path,
                        max_tokens=CHILD_MAX_TOKENS,
                        min_tokens=CHILD_MIN_TOKENS,
                    )
                )
                log.info(
                    "chunked_as_code",
                    source_path=source_path, display_path=display_path,
                    nodes=len(chunking.nodes),
                )
            else:
                chunking = chunk_markdown(
                    markdown,
                    page_map=page_map,
                    figures=figures,
                    tables=tables,
                    doc_title=title,
                )
            self._insert_chunks(doc_id, shard_id, chunking.nodes)
            child_ids = self._child_chunk_ids(doc_id)
            vectors = await self._embed_children(doc_id, child_ids)
            if vectors is not None and len(vectors):
                self.shards.append(shard_id, child_ids, vectors)
            figure_vector_count, figure_vectors_failed = (
                await self._embed_figures_multimodal(doc_id)
            )
            self._index_fts(doc_id, shard_id, chunking.nodes)
            self._grant_extra_depts(doc_id, placement.extra_grants, level)
            self._publish(
                doc_id,
                page_count=_page_count(page_map),
                status="published",
            )
        except Exception as exc:  # 失败必须落状态，不能静默停在中途态
            log.error("ingest_failed", doc_id=doc_id, error=str(exc))
            self._publish(doc_id, page_count=None, status="failed")
            raise

        result = IngestResult(
            doc_id=doc_id,
            shard_id=shard_id,
            parent_count=len(chunking.parents),
            child_count=len(chunking.children),
            vector_count=len(child_ids),
            figure_vector_count=figure_vector_count,
            figure_vectors_failed=figure_vectors_failed,
            status="published",
            checksum=checksum,
        )
        log.info("ingest_done", **result.as_dict())
        return result

    # ------------------------------------------------------------------
    # 落库细节
    # ------------------------------------------------------------------

    def _update_document(
        self,
        doc_id: int,
        *,
        kb_id: int,
        title: str,
        source_path: str,
        level: int,
        visible_dept_ids: list[str],
        checksum: str,
        doc_version: str,
        status: str,
    ) -> None:
        import json

        with self.db.sync_connect() as conn:
            conn.execute(
                "UPDATE ekbs_document SET kb_id = ?, title = ?, source_path = ?, level = ?, "
                "visible_dept_ids = ?, status = ?, checksum = ?, doc_version = ?, "
                "updated_at = datetime('now') WHERE doc_id = ?",
                (
                    kb_id, title, source_path or title, level,
                    json.dumps(visible_dept_ids, ensure_ascii=False), status, checksum,
                    doc_version, doc_id,
                ),
            )
            conn.commit()

    def _replace_existing(self, doc_id: int, *, shard_id: str) -> None:
        """重新解析前清掉旧分块、旧 FTS 行与旧向量（三处必须同口径，见 :meth:`ingest_markdown`）。"""
        with self.db.sync_connect() as conn:
            chunk_ids = [
                int(r["chunk_id"])
                for r in conn.execute(
                    "SELECT chunk_id FROM ekbs_chunk WHERE doc_id = ?", (doc_id,)
                ).fetchall()
            ]
        if not chunk_ids:
            return
        self.fts.delete_chunk_ids(chunk_ids)
        # 重新解析前把旧向量**真删**（IDMap2 支持 remove_ids），
        # 否则每重跑一次解析就在分片里留一批孤儿向量
        with self.db.sync_connect() as conn:
            by_shard: dict[str, list[int]] = {}
            for row in conn.execute(
                "SELECT chunk_id, shard_id FROM ekbs_chunk WHERE doc_id = ?", (doc_id,)
            ).fetchall():
                by_shard.setdefault(str(row["shard_id"]), []).append(int(row["chunk_id"]))
        for shard_id, ids in by_shard.items():
            self.shards.remove(shard_id, ids)
        if self.figure_index is not None:
            self.figure_index.remove(chunk_ids)
        with self.db.sync_connect() as conn:
            conn.execute("DELETE FROM ekbs_chunk WHERE doc_id = ?", (doc_id,))
            conn.commit()
        # 这是**正常路径**，不是告警：上面已连同分块与 FTS 一起把旧向量真删了。
        # 早期这里用 warning + "请跑 rebuild_shard" 是因为 IndexFlatIP 删不掉向量；
        # 换 IDMap2 之后那条提示已经不成立，留着会让运维白跑一次全量重建。
        log.info(
            "reingest_vectors_removed",
            doc_id=doc_id,
            shard_id=shard_id,
            removed_vectors=len(chunk_ids),
        )

    def _insert_document(
        self,
        *,
        kb_id: int,
        title: str,
        source_path: str,
        level: int,
        visible_dept_ids: list[str],
        checksum: str,
        doc_version: str,
        status: str,
    ) -> int:
        import json

        with self.db.sync_connect() as conn:
            conn.execute(
                "INSERT INTO ekbs_document(kb_id, title, source_path, level, visible_dept_ids, "
                "status, checksum, doc_version, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
                (
                    kb_id, title, source_path or title, level,
                    json.dumps(visible_dept_ids, ensure_ascii=False), status, checksum, doc_version,
                ),
            )
            doc_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
            conn.commit()
        return doc_id

    def _insert_chunks(self, doc_id: int, shard_id: str, nodes: Sequence[ChunkNode]) -> None:
        """按 ``seq`` 顺序插入，把子块的 ``parent_index`` 解析成真实 ``parent_id``。"""
        with self.db.sync_connect() as conn:
            id_by_index: dict[int, int] = {}
            for index, node in enumerate(nodes):
                parent_id = (
                    id_by_index.get(node.parent_index)
                    if node.parent_index is not None
                    else None
                )
                conn.execute(
                    "INSERT INTO ekbs_chunk(doc_id, parent_id, chunk_type, seq, content, "
                    "token_count, heading_path, page_from, page_to, figure_id, image_path, "
                    "shard_id, embedding_fingerprint) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        doc_id, parent_id, node.chunk_type, node.seq, node.content,
                        node.token_count, node.heading_path, node.page_from, node.page_to,
                        node.figure_id, node.image_path, shard_id,
                        self.providers.embed.ref.fingerprint,
                    ),
                )
                id_by_index[index] = int(
                    conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                )
            conn.commit()

    def _child_chunk_ids(self, doc_id: int) -> list[int]:
        """**可向量化/可检索**的块 id —— 口径见 :mod:`ekbs.ingest.retrievable`。

        一条「叶子块才是检索单元」同时兑现三件事：图片块进得了向量（图上的文字搜得到）、
        代码的符号块进得了向量（代码库不再是"存了但搜不到"）、Markdown 的父容器不重复入向量。
        """
        with self.db.sync_connect() as conn:
            return retrievable_chunk_ids(conn, doc_id=doc_id)

    def _index_fts(self, doc_id: int, shard_id: str, nodes: Sequence[ChunkNode]) -> None:
        with self.db.sync_connect() as conn:
            rows = conn.execute(
                "SELECT c.chunk_id, c.content, c.heading_path, c.chunk_type FROM ekbs_chunk c "
                f"WHERE c.doc_id = ? AND {RETRIEVABLE_PREDICATE} ORDER BY c.seq",
                (doc_id,),
            ).fetchall()
        written = self.fts.upsert_retrievable_with_doc2query(
            [
                (
                    int(r["chunk_id"]),
                    shard_id,
                    str(r["content"]),
                    r["heading_path"],
                    str(r["chunk_type"] or "child"),
                )
                for r in rows
            ],
            enabled=bool(getattr(self.settings.retrieval, "doc2query_enabled", True)),
            max_questions=int(getattr(self.settings.retrieval, "doc2query_max", 4) or 4),
        )
        log.info("fts_indexed", doc_id=doc_id, rows=written)

    async def _embed_children(self, doc_id: int, chunk_ids: Sequence[int]):
        """分批嵌入（``models.embed.batch_size``）—— **子块 + 图片块**，
        入库期失败按 `00` §2.3 排队重试。"""
        import numpy as np

        if not chunk_ids:
            return None
        with self.db.sync_connect() as conn:
            rows = conn.execute(
                "SELECT c.chunk_id, c.content, c.heading_path FROM ekbs_chunk c "
                f"WHERE c.doc_id = ? AND {RETRIEVABLE_PREDICATE} ORDER BY c.seq",
                (doc_id,),
            ).fetchall()
        # 嵌入的是「标题路径 + 正文」（口径唯一，见 chunker.retrieval_text）
        texts = [retrieval_text(r["heading_path"], str(r["content"])) for r in rows]
        batch_size = max(1, int(self.settings.models.embed.batch_size))
        vectors: list[list[float]] = []
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            vectors.extend(await self.providers.embed.embed(batch))
        array = np.asarray(vectors, dtype="float32")
        expected_dim = int(self.providers.embed.ref.dim or 0)
        if expected_dim and array.shape[1] != expected_dim:
            raise ConfigError(
                f"嵌入维度 {array.shape[1]} ≠ 配置 dim {expected_dim}（C3：模型与配置不一致）"
            )
        return array

    async def _embed_figures_multimodal(self, doc_id: int) -> tuple[int, int]:
        """figure 块再写一路多模态图像向量（与文本 FAISS 分索引，维度通常 1152）。"""
        import numpy as np

        if self.figure_index is None or self.providers.multimodal is None:
            return 0, 0
        with self.db.sync_connect() as conn:
            rows = conn.execute(
                "SELECT chunk_id, image_path FROM ekbs_chunk "
                "WHERE doc_id = ? AND chunk_type = 'figure' "
                "AND image_path IS NOT NULL AND TRIM(image_path) != '' "
                "ORDER BY seq",
                (doc_id,),
            ).fetchall()
        if not rows:
            return 0, 0
        data_dir = self.settings.data_dir
        vectors: list[list[float]] = []
        chunk_ids: list[int] = []
        failed: list[int] = []
        mm = self.providers.multimodal
        for row in rows:
            rel = str(row["image_path"])
            path = (data_dir / rel).resolve()
            if not path.is_file():
                log.warning(
                    "figure_mm_missing_file",
                    doc_id=doc_id,
                    chunk_id=int(row["chunk_id"]),
                    image_path=rel,
                )
                continue
            vector: list[float] | None = None
            last_error = ""
            # 单张图的向量化失败**必须重试**：多模态端点会偶发连接错误/限流（实测
            # `HTTPSConnectionPool(host='dashscope.aliyuncs.com')`），旧实现只记一条 warning
            # 就跳过 —— 结果是图片进了库却**没有向量**，「存了但搜不到」，而文档照样
            # published，除人工比对文件之外没有任何信号（实测 2/201 张图如此）。
            for attempt in range(self.settings.models.multimodal_embed.max_retries):
                try:
                    vector = await mm.embed_image(path)
                    break
                except Exception as exc:  # noqa: BLE001 —— 网络/限流/解码一律重试
                    last_error = str(exc)[:160]
                    if attempt + 1 < self.settings.models.multimodal_embed.max_retries:
                        await asyncio.sleep(self.settings.models.multimodal_embed.retry_backoff_s * (attempt + 1))
            if vector is None:
                failed.append(int(row["chunk_id"]))
                log.warning(
                    "figure_mm_embed_failed",
                    doc_id=doc_id,
                    chunk_id=int(row["chunk_id"]),
                    attempts=self.settings.models.multimodal_embed.max_retries,
                    error=last_error,
                )
                continue
            vectors.append(vector)
            chunk_ids.append(int(row["chunk_id"]))
        if failed:
            # 汇总一条：**可观测**比"默默少几个向量"重要（`IngestResult.figure_vectors_failed`）
            log.warning(
                "figure_mm_partial",
                doc_id=doc_id,
                failed=len(failed),
                chunk_ids=failed[:20],
                hint="这些图片已成为「存了但搜不到」的孤儿块，需重嵌入",
            )
        if not vectors:
            return 0, len(failed)
        written = self.figure_index.upsert(
            np.asarray(vectors, dtype="float32"), chunk_ids
        )
        log.info("figure_mm_indexed", doc_id=doc_id, figures=written, failed=len(failed))
        return written, len(failed)

    def _grant_extra_depts(
        self, doc_id: int, extra_grants: Sequence[tuple[str, str]], level: int
    ) -> None:
        """多部门文档：向量只存一份，其余部门靠 **shard 级 ACL allow** 授权（`06` §2.2）。"""
        if not extra_grants:
            return
        with self.db.sync_connect() as conn:
            for dept_ref, shard_id in extra_grants:
                conn.execute(
                    "INSERT INTO ekbs_acl_entry(object_type, object_id, subject_type, "
                    "subject_id, permission, effect, created_at) "
                    "VALUES ('shard', ?, 'dept', ?, 'read', 'allow', datetime('now')) "
                    "ON CONFLICT(object_type, object_id, subject_type, subject_id, permission) "
                    "DO NOTHING",
                    (shard_id, str(dept_ref)),
                )
            # ACL 与 policy_version 自增**必须同事务**（06 §2.6），否则旧缓存会继续被复用
            bump_policy_version(conn, reason=f"ingest_extra_depts:doc={doc_id}")
            conn.commit()

    def _publish(self, doc_id: int, *, page_count: int | None, status: str) -> None:
        with self.db.sync_connect() as conn:
            conn.execute(
                "UPDATE ekbs_document SET status = ?, page_count = COALESCE(?, page_count), "
                "updated_at = datetime('now'), parse_path = COALESCE(parse_path, ?) "
                "WHERE doc_id = ?",
                (status, page_count, None, doc_id),
            )
            conn.commit()


def _page_count(page_map: Sequence[tuple[int, int, int]] | None) -> int | None:
    if not page_map:
        return None
    return max(page for _, page, _ in page_map) if page_map else None


def text_token_count(text: str) -> int:
    """便于工具与测试复用的 token 计数别名。"""
    return count_tokens(text)
