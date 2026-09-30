"""权限变更服务 —— `03` §6.4 的「四件事」必须一起发生：**数据更新 + 分片迁移 + 缓存失效 + 审计**。

为什么必须有这个模块（它不是锦上添花，是补一个真实漏洞）：

文档密级/可见部门变化时，若只改 ``ekbs_document`` 而不动 ``ekbs_chunk.shard_id``，
向量仍留在**旧密级分片**里。把一份内部文档调成「秘密」后，只有 L2 权限的用户
**照样能召回它** —— 因为分片路由（C1）认为那是 L2 分片。反之降密也需要迁移，
否则高密级用户以外的授权范围对不上。

四件事的落地方式：

1. **数据更新**：``ekbs_document.level`` / ``visible_dept_ids``；
2. **分片迁移**：重算 ``shard_id``、重嵌入并追加到目标分片、重写 FTS5 行、回写 ``faiss_row``；
3. **缓存失效**：``policy_version`` 在**同一事务**内自增 ⇒ 指纹变化 ⇒
   旧指纹下的检索/答案缓存自然不再命中（这是 C2 的设计红利）；此外再做一次
   ``ekbs:cache:retrieve:*`` / ``ekbs:cache:answer:*`` 的前缀清理，回收空间；
4. **审计**：写 ``ekbs_perm_change_log``，带 ``policy_version_before/after``。

事务边界说明（诚实交代）：FAISS 是文件索引，不在 SQLite 事务里。因此顺序是
**先追加向量到目标分片并落盘 → 再提交数据库事务**。若数据库事务失败，
目标分片里会留下「没有 chunk 指向」的孤儿向量 —— 它们不会被召回
（``_chunk_ids_by_faiss_row`` 按当前 ``shard_id`` 过滤），只会白占容量，
由 ``ekbs.tools.rebuild_shard`` 或每日对账清理。反过来做（先提交后写索引）
才会造成「能召回到不存在的向量」的不一致。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Sequence

from ekbs.cache.store import PREFIX_ANSWER, PREFIX_RETRIEVE, CacheStore
from ekbs.config import Settings
from ekbs.db import Database
from ekbs.errors import ConfigError, SchemaError
from ekbs.index.faiss_store import ShardIndexManager, ensure_shard_for_placement
from ekbs.index.fts import FtsIndex
from ekbs.index.shard import parse_shard_id, resolve_shard_for_document
from ekbs.ingest.chunker import retrieval_text
from ekbs.logging import get_logger
from ekbs.permission.router import bump_policy_version
from ekbs.provider.registry import Providers

log = get_logger(component="perm_change")


@dataclass
class ChangeResult:
    object_type: str
    object_id: str
    change_type: str
    field: str
    old_value: str
    new_value: str
    policy_version: int
    from_shard: str = ""
    to_shard: str = ""
    migrated_chunks: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "object_type": self.object_type,
            "object_id": self.object_id,
            "change_type": self.change_type,
            "field": self.field,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "policy_version": self.policy_version,
            "from_shard": self.from_shard,
            "to_shard": self.to_shard,
            "migrated_chunks": self.migrated_chunks,
        }


class PermissionChangeService:
    """所有权限/密级/部门/ACL 变更的**唯一入口**（禁止绕过，`03` §6.4）。"""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        providers: Providers,
        shards: ShardIndexManager,
        fts: FtsIndex | None = None,
        cache: CacheStore | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.providers = providers
        self.shards = shards
        self.fts = fts or FtsIndex(db)
        self.cache = cache

    # ------------------------------------------------------------------
    # 文档密级 / 可见部门变更
    # ------------------------------------------------------------------

    async def change_document_level(
        self,
        doc_id: int,
        new_level: int,
        *,
        operator_id: int | None = None,
        reason: str = "",
        ip: str | None = None,
    ) -> ChangeResult:
        doc = self._load_document(doc_id)
        new_level = int(new_level)
        if new_level == int(doc["level"]):
            raise ConfigError(f"文档 {doc_id} 密级未变化（仍为 L{new_level}）")
        # 与入库路径同口径的闸门：把文档调到 L3 等价于让 L3 语料进分片
        self.settings.assert_l3_ingest_allowed(new_level)
        if new_level == 4 and not self.settings.permission.l4_enabled:
            raise ConfigError("不允许调到 L4：permission.l4_enabled=false（00 D11）")

        placement = resolve_shard_for_document(
            level=new_level,
            visible_dept_ids=list(json.loads(doc["visible_dept_ids"] or "[]")),
            settings=self.settings,
        )
        return await self._apply_document_change(
            doc,
            new_level=new_level,
            new_depts=None,
            target_shard=placement.primary_shard_id,
            change_type="level",
            field="level",
            old_value=f"L{int(doc['level'])}",
            new_value=f"L{new_level}",
            operator_id=operator_id,
            reason=reason,
            ip=ip,
        )

    async def change_document_depts(
        self,
        doc_id: int,
        new_dept_ids: Sequence[str],
        *,
        operator_id: int | None = None,
        reason: str = "",
        ip: str | None = None,
    ) -> ChangeResult:
        doc = self._load_document(doc_id)
        new_depts = [str(d) for d in new_dept_ids]
        old_depts = list(json.loads(doc["visible_dept_ids"] or "[]"))
        if sorted(new_depts) == sorted(old_depts):
            raise ConfigError(f"文档 {doc_id} 可见部门未变化")
        placement = resolve_shard_for_document(
            level=int(doc["level"]), visible_dept_ids=new_depts, settings=self.settings
        )
        return await self._apply_document_change(
            doc,
            new_level=int(doc["level"]),
            new_depts=new_depts,
            target_shard=placement.primary_shard_id,
            change_type="dept",
            field="visible_dept_ids",
            old_value=json.dumps(old_depts, ensure_ascii=False),
            new_value=json.dumps(new_depts, ensure_ascii=False),
            operator_id=operator_id,
            reason=reason,
            ip=ip,
        )

    async def _apply_document_change(
        self,
        doc: Any,
        *,
        new_level: int,
        new_depts: list[str] | None,
        target_shard: str,
        change_type: str,
        field: str,
        old_value: str,
        new_value: str,
        operator_id: int | None,
        reason: str,
        ip: str | None,
    ) -> ChangeResult:
        doc_id = int(doc["doc_id"])
        current_shard = self._current_shard(doc_id)
        migrated = 0
        if current_shard != target_shard:
            migrated = await self._migrate_document(doc_id, target_shard)

        with self.db.sync_connect() as conn:
            if new_depts is not None:
                conn.execute(
                    "UPDATE ekbs_document SET visible_dept_ids = ?, updated_at = datetime('now') "
                    "WHERE doc_id = ?",
                    (json.dumps(new_depts, ensure_ascii=False), doc_id),
                )
            conn.execute(
                "UPDATE ekbs_document SET level = ?, updated_at = datetime('now') WHERE doc_id = ?",
                (new_level, doc_id),
            )
            # ③ 缓存失效：与数据更新**同一事务**自增（06 §2.6）
            version = bump_policy_version(conn, reason=f"{change_type}:doc={doc_id}")
            # ④ 审计
            conn.execute(
                "INSERT INTO ekbs_perm_change_log(operator_id, object_type, object_id, "
                "change_type, field, old_value, new_value, reason, policy_version_before, "
                "policy_version_after, ip) "
                "VALUES (?, 'document', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    operator_id, str(doc_id), change_type, field, old_value, new_value, reason,
                    version - 1, version, ip,
                ),
            )
            conn.commit()

        await self._invalidate_retrieval_caches()
        result = ChangeResult(
            object_type="document", object_id=str(doc_id), change_type=change_type, field=field,
            old_value=old_value, new_value=new_value, policy_version=version,
            from_shard=current_shard, to_shard=target_shard, migrated_chunks=migrated,
        )
        log.info("perm_change_applied", **result.as_dict())
        return result

    # ------------------------------------------------------------------
    # 用户密级
    # ------------------------------------------------------------------

    async def change_user_clearance(
        self,
        user_id: int,
        new_clearance: int,
        *,
        operator_id: int | None = None,
        reason: str = "",
        ip: str | None = None,
    ) -> ChangeResult:
        new_clearance = int(new_clearance)
        if not 1 <= new_clearance <= 4:
            raise ConfigError(f"密级越界：{new_clearance}（必须是 1..4）")
        with self.db.sync_connect() as conn:
            row = conn.execute(
                "SELECT clearance FROM ekbs_user WHERE user_id = ? AND deleted_at IS NULL",
                (user_id,),
            ).fetchone()
            if row is None:
                raise ConfigError(f"用户不存在：{user_id}")
            old = int(row["clearance"])
            if old == new_clearance:
                raise ConfigError(f"用户 {user_id} 密级未变化（仍为 L{new_clearance}）")
            conn.execute(
                "UPDATE ekbs_user SET clearance = ?, perm_version = perm_version + 1, "
                "synced_at = datetime('now') WHERE user_id = ?",
                (new_clearance, user_id),
            )
            version = bump_policy_version(conn, reason=f"clearance:user={user_id}")
            conn.execute(
                "INSERT INTO ekbs_perm_change_log(operator_id, object_type, object_id, "
                "change_type, field, old_value, new_value, reason, policy_version_before, "
                "policy_version_after, ip) "
                "VALUES (?, 'user', ?, 'clearance', 'clearance', ?, ?, ?, ?, ?, ?)",
                (
                    operator_id, str(user_id), f"L{old}", f"L{new_clearance}", reason,
                    version - 1, version, ip,
                ),
            )
            conn.commit()
        # 该用户的权限上下文缓存必须立刻失效（04 §4.6：不能等 300s TTL）
        if self.cache is not None:
            self.cache.invalidate(f"ekbs:perm:user:{user_id}")
        result = ChangeResult(
            object_type="user", object_id=str(user_id), change_type="clearance",
            field="clearance", old_value=f"L{old}", new_value=f"L{new_clearance}",
            policy_version=version,
        )
        log.info("perm_change_applied", **result.as_dict())
        return result

    # ------------------------------------------------------------------
    # ACL 授权 / 回收
    # ------------------------------------------------------------------

    async def grant_acl(
        self,
        *,
        object_type: str,
        object_id: str,
        subject_type: str,
        subject_id: str,
        permission: str = "read",
        effect: str = "allow",
        operator_id: int | None = None,
        expire_at: str | None = None,
        reason: str = "",
        ip: str | None = None,
    ) -> ChangeResult:
        return await self._write_acl(
            action="grant", object_type=object_type, object_id=object_id,
            subject_type=subject_type, subject_id=subject_id, permission=permission,
            effect=effect, operator_id=operator_id, expire_at=expire_at, reason=reason, ip=ip,
        )

    async def revoke_acl(
        self,
        *,
        object_type: str,
        object_id: str,
        subject_type: str,
        subject_id: str,
        permission: str = "read",
        operator_id: int | None = None,
        reason: str = "",
        ip: str | None = None,
    ) -> ChangeResult:
        return await self._write_acl(
            action="revoke", object_type=object_type, object_id=object_id,
            subject_type=subject_type, subject_id=subject_id, permission=permission,
            effect="allow", operator_id=operator_id, expire_at=None, reason=reason, ip=ip,
        )

    async def _write_acl(
        self,
        *,
        action: str,
        object_type: str,
        object_id: str,
        subject_type: str,
        subject_id: str,
        permission: str,
        effect: str,
        operator_id: int | None,
        expire_at: str | None,
        reason: str,
        ip: str | None,
    ) -> ChangeResult:
        if effect not in {"allow", "deny"}:
            raise ConfigError(f"effect 非法：{effect}（只能是 allow / deny）")
        if object_type not in {"kb", "folder", "document", "shard"}:
            raise ConfigError(f"object_type 非法：{object_type}")
        with self.db.sync_connect() as conn:
            if action == "grant":
                conn.execute(
                    "INSERT INTO ekbs_acl_entry(object_type, object_id, subject_type, "
                    "subject_id, permission, effect, granted_by, expire_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
                    "ON CONFLICT(object_type, object_id, subject_type, subject_id, permission) "
                    "DO UPDATE SET effect = excluded.effect, expire_at = excluded.expire_at",
                    (
                        object_type, str(object_id), subject_type, str(subject_id), permission,
                        effect, operator_id, expire_at,
                    ),
                )
                change_type = "acl_grant"
            else:
                cursor = conn.execute(
                    "DELETE FROM ekbs_acl_entry WHERE object_type = ? AND object_id = ? "
                    "AND subject_type = ? AND subject_id = ? AND permission = ?",
                    (object_type, str(object_id), subject_type, str(subject_id), permission),
                )
                if not cursor.rowcount:
                    raise ConfigError("要回收的 ACL 条目不存在")
                change_type = "acl_revoke"
            # ACL 与 policy_version 自增**必须同事务**（06 §2.6）：否则出现
            # 「ACL 变了、指纹没变、旧缓存继续复用」的窗口
            version = bump_policy_version(
                conn, reason=f"{change_type}:{object_type}={object_id}:{subject_type}={subject_id}"
            )
            conn.execute(
                "INSERT INTO ekbs_perm_change_log(operator_id, object_type, object_id, "
                "change_type, field, old_value, new_value, reason, policy_version_before, "
                "policy_version_after, ip) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    operator_id, object_type, str(object_id), change_type,
                    f"{subject_type}:{subject_id}", "", f"{effect}:{permission}", reason,
                    version - 1, version, ip,
                ),
            )
            conn.commit()
        result = ChangeResult(
            object_type=object_type, object_id=str(object_id), change_type=change_type,
            field=f"{subject_type}:{subject_id}", old_value="", new_value=f"{effect}:{permission}",
            policy_version=version,
        )
        log.info("perm_change_applied", **result.as_dict())
        return result

    # ------------------------------------------------------------------
    # 分片迁移
    # ------------------------------------------------------------------

    async def _migrate_document(self, doc_id: int, target_shard: str) -> int:
        """把文档的全部 chunk 迁到目标分片：重嵌入 → 追加 → 重写 FTS → 回写 shard_id。"""
        with self.db.sync_connect() as conn:
            rows = conn.execute(
                "SELECT chunk_id, chunk_type, content, heading_path FROM ekbs_chunk "
                "WHERE doc_id = ? ORDER BY seq",
                (doc_id,),
            ).fetchall()
        if not rows:
            raise SchemaError(f"文档 {doc_id} 没有任何分块，无法迁移（是否尚未入库？）")

        dept_code, level = parse_shard_id(target_shard)
        ensure_shard_for_placement(
            self.shards, target_shard, level=level, dept_code=dept_code,
            fingerprint=self.providers.embed.ref.fingerprint,
        )

        children = [
            (int(r["chunk_id"]), retrieval_text(r["heading_path"], str(r["content"])))
            for r in rows
            if r["chunk_type"] == "child"
        ]
        new_rows: dict[int, int] = {}
        if children:
            import numpy as np

            vectors: list[list[float]] = []
            batch_size = max(1, int(self.settings.models.embed.batch_size))
            texts = [text for _, text in children]
            for start in range(0, len(texts), batch_size):
                vectors.extend(await self.providers.embed.embed(texts[start : start + batch_size]))
            assigned = self.shards.append(
                target_shard, [chunk_id for chunk_id, _ in children], np.asarray(vectors, dtype="float32")
            )
            new_rows = dict(zip([chunk_id for chunk_id, _ in children], assigned))

        with self.db.sync_connect() as conn:
            current = conn.execute(
                "SELECT DISTINCT shard_id FROM ekbs_chunk WHERE doc_id = ?", (doc_id,)
            ).fetchall()
            old_shards = [str(r["shard_id"]) for r in current]
            conn.execute(
                "UPDATE ekbs_chunk SET shard_id = ?, faiss_row = NULL WHERE doc_id = ?",
                (target_shard, doc_id),
            )
            for chunk_id, row in new_rows.items():
                conn.execute(
                    "UPDATE ekbs_chunk SET faiss_row = ?, embedding_fingerprint = ? "
                    "WHERE chunk_id = ?",
                    (row, self.providers.embed.ref.fingerprint, chunk_id),
                )
            # 目标分片的登记值以内存索引为准（append 已返回真实行号）
            index = self.shards.get(target_shard)
            if index is not None:
                conn.execute(
                    "UPDATE ekbs_shard SET vector_count = ?, dim = ?, embedding_fingerprint = ?, "
                    "updated_at = datetime('now') WHERE shard_id = ?",
                    (
                        index.vector_count,
                        int(index.dim),  # ⚠️ 必须是维度整数：写成指纹会让 smoke.sh 的 dim 自检失败
                        self.providers.embed.ref.fingerprint,
                        target_shard,
                    ),
                )
            conn.commit()

        # FTS5 行必须跟着换分片：否则稀疏召回仍从旧分片命中（C1 的等价性要求）
        chunk_ids = [int(r["chunk_id"]) for r in rows if r["chunk_type"] == "child"]
        self.fts.delete_chunk_ids(chunk_ids)
        self.fts.upsert_chunks(
            [
                (int(r["chunk_id"]), target_shard, str(r["content"]), r["heading_path"])
                for r in rows
                if r["chunk_type"] == "child"
            ]
        )
        log.warning(
            "document_migrated",
            doc_id=doc_id,
            from_shards=old_shards,
            to_shard=target_shard,
            chunks=len(rows),
            hint="旧分片里的陈旧向量不会被召回（按 shard_id 过滤），建议 rebuild_shard 回收容量",
        )
        return len(rows)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _load_document(self, doc_id: int) -> Any:
        with self.db.sync_connect() as conn:
            row = conn.execute(
                "SELECT doc_id, kb_id, title, level, status, visible_dept_ids FROM ekbs_document "
                "WHERE doc_id = ?",
                (doc_id,),
            ).fetchone()
        if row is None:
            raise ConfigError(f"文档不存在：{doc_id}")
        return row

    def _current_shard(self, doc_id: int) -> str:
        with self.db.sync_connect() as conn:
            row = conn.execute(
                "SELECT shard_id FROM ekbs_chunk WHERE doc_id = ? ORDER BY chunk_id LIMIT 1",
                (doc_id,),
            ).fetchone()
        return str(row["shard_id"]) if row else ""

    async def _invalidate_retrieval_caches(self) -> None:
        """回收旧指纹下的检索/答案缓存。

        注意：**正确性不依赖这一步** —— 指纹已随 policy_version 变化，
        旧键永远不会再被命中（C2）。这里只是回收空间，因此失败不影响结果。
        """
        if self.cache is None:
            return
        try:
            for prefix in (PREFIX_RETRIEVE, PREFIX_ANSWER):
                self.cache.invalidate(prefix)
                await asyncio.to_thread(self._delete_prefix_l2, prefix)
        except Exception as exc:  # pragma: no cover - 清理失败不应影响权限变更
            log.warning("cache_invalidate_failed", error=str(exc))

    def _delete_prefix_l2(self, prefix: str) -> int:
        with self.db.sync_connect() as conn:
            cursor = conn.execute("DELETE FROM ekbs_cache WHERE cache_key LIKE ?", (f"{prefix}%",))
            conn.commit()
            return int(cursor.rowcount or 0)
