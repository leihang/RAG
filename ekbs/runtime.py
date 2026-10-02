"""运行时容器 —— 把配置、DB、模型、权限、索引、链路装配成一个对象。

四个 launchd 进程（api / dingtalk / worker / scheduler）各自构造一次 :class:`Runtime`，
因此这里也是**启动自检**的唯一落点（`00` §2.3 的「拒绝启动」类故障）：

1. Schema 自检（29 表 / 12 触发器 / WAL / integrity）；
2. 嵌入指纹一致性（分片登记的指纹 ≠ 当前模型指纹 → 拒绝启动）；
3. 合规闸状态（``corpus_egress_ack``）只记录，不阻断在线问答。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from ekbs.authz import AuthzService
from ekbs.cache.store import CacheStore
from ekbs.config import Secrets, Settings, load_settings
from ekbs.db import Database, SchemaReport
from ekbs.errors import EmbeddingFingerprintMismatch
from ekbs.index.faiss_store import ShardIndexManager
from ekbs.index.figure_faiss import FigureIndex
from ekbs.index.fts import FtsIndex
from ekbs.logging import configure_logging, get_logger
from ekbs.permission.acl import DocumentGate
from ekbs.permission.context import PermContext
from ekbs.permission.router import ShardRouter
from ekbs.provider.registry import Providers, build_providers
from ekbs.ratelimit import RateLimiter
from ekbs.retrieval.pipeline import RetrievalPipeline
from ekbs.security import ContentGuard
from ekbs.session import QueryRewriter, SessionStore

log = get_logger(component="runtime")


@dataclass
class Runtime:
    settings: Settings
    secrets: Secrets
    db: Database
    providers: Providers
    cache: CacheStore
    router: ShardRouter
    gate: DocumentGate
    shards: ShardIndexManager
    fts: FtsIndex
    pipeline: RetrievalPipeline
    rate_limiter: RateLimiter
    sessions: SessionStore
    content_guard: ContentGuard
    authz: AuthzService
    schema: SchemaReport
    #: figure 多模态旁路索引；未启用时为 None
    figure_index: FigureIndex | None = None

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------

    @classmethod
    def create(
        cls, settings: Settings | None = None, *, service: str = "api", init_schema: bool = False
    ) -> "Runtime":
        settings = settings or load_settings()
        configure_logging(settings, service)
        settings.ensure_dirs()

        secrets = Secrets()
        db = Database(settings)
        if init_schema:
            # 仅显式入口（ekbs.tools.*）会走到这里；服务进程一律严格自检
            schema, created = db.ensure_schema()
            if created:
                log.warning("schema_created_by_tool", path=str(db.path), tables=schema.tables)
        schema = db.require_ready()
        providers = build_providers(settings, secrets)
        cache = CacheStore(db, settings)
        router = ShardRouter(db, settings)
        gate = DocumentGate(db, settings)
        shards = ShardIndexManager(
            db,
            settings,
            dim=settings.models.embed.dim,
            fingerprint=providers.embed.ref.fingerprint,
        )
        fts = FtsIndex(db)

        figure_index: FigureIndex | None = None
        if providers.multimodal is not None:
            mm_dim = int(providers.multimodal.ref.dim or settings.models.multimodal_embed.dim)
            figure_path = settings.data_paths()["index"] / "figures.faiss"
            figure_index = FigureIndex(figure_path, dim=mm_dim)
            log.info(
                "figure_index_ready",
                path=str(figure_path),
                dim=mm_dim,
                vectors=figure_index.vector_count,
                model=providers.multimodal.ref.ident,
            )

        # 启动自检②：分片指纹一致性（C3）。全部不符 → 拒绝启动。
        active = shards.registry()
        if active:
            shards.load_active(strict=True)
            log.info("shards_loaded", count=len(active), dim=shards.dim)

        sessions = SessionStore(db, settings)
        rewriter = QueryRewriter(settings=settings, llm=providers.llm)
        authz = AuthzService(db)
        content_guard = ContentGuard(
            sensitive_words_path=settings.security.get("sensitive_words_path", "config/sensitive_words.txt"),
            enabled=bool(settings.security.get("content_security_enabled", True)),
        )
        pipeline = RetrievalPipeline(
            db=db, settings=settings, providers=providers, cache=cache, gate=gate,
            shard_manager=shards, fts=fts, session_store=sessions, rewriter=rewriter,
            content_guard=content_guard, figure_index=figure_index,
        )
        rate_limiter = RateLimiter(db, settings)
        log.info(
            "runtime_ready",
            service=service,
            env=settings.app.env,
            db=str(db.path),
            tables=schema.tables,
            triggers=schema.triggers,
            embed=providers.embed.ref.ident,
            embed_fingerprint=providers.embed.ref.fingerprint[:12],
            multimodal=(
                providers.multimodal.ref.ident if providers.multimodal else "disabled"
            ),
            rerank=providers.rerank.ref.ident if providers.rerank else "disabled",
            llm=providers.llm.ref.ident,
            corpus_egress_ack=settings.permission.corpus_egress_ack,
            l3_ingest_enabled=settings.permission.l3_ingest_enabled,
        )
        return cls(
            settings=settings, secrets=secrets, db=db, providers=providers, cache=cache,
            router=router, gate=gate, shards=shards, fts=fts, pipeline=pipeline,
            rate_limiter=rate_limiter, sessions=sessions, content_guard=content_guard,
            authz=authz, schema=schema, figure_index=figure_index,
        )

    # ------------------------------------------------------------------
    # 权限上下文（唯一的构建入口）
    # ------------------------------------------------------------------

    async def perm_context(self, user_id: int) -> PermContext:
        """按用户构建权限上下文；纯 SQLite，放线程池执行（`03` §4.1）。"""
        return await asyncio.to_thread(self.router.build_context, user_id)

    # ------------------------------------------------------------------
    # 自检
    # ------------------------------------------------------------------

    def fingerprint_match(self) -> bool:
        """所有 active 分片的登记指纹是否与当前嵌入模型一致。"""
        current = self.providers.embed.ref.fingerprint
        return all(row.embedding_fingerprint == current for row in self.shards.registry())

    def disk_used_pct(self) -> float:
        import shutil

        usage = shutil.disk_usage(str(self.settings.data_dir))
        return round(usage.used / usage.total * 100, 2)

    def data_dir_writable(self) -> bool:
        probe = Path(self.settings.data_paths()["tmp"]) / ".write_probe"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return True
        except OSError:
            return False

    async def aclose(self) -> None:
        await self.db.aclose()


__all__ = ["Runtime", "EmbeddingFingerprintMismatch"]
