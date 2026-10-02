"""权限变更服务 —— `03` §6.4 的四件事（数据更新 + 分片迁移 + 缓存失效 + 审计）。

其中第一条用例是**修漏洞**的回归测试：文档调高密级后必须离开旧分片，
否则低密级用户仍能从旧分片召回它。
"""

from __future__ import annotations

import json

import pytest

from ekbs.errors import ConfigError
from ekbs.permission.changes import PermissionChangeService
from tests.conftest import user_id_of


@pytest.fixture
def l3(seeded):
    """打开 L3 闸门（生产配置保持 false；这里只做测试内开闸，见 00 C5）。"""
    runtime, _ = seeded
    runtime.settings.permission.l3_ingest_enabled = True
    return runtime


def service(runtime) -> PermissionChangeService:
    return PermissionChangeService(
        db=runtime.db, settings=runtime.settings, providers=runtime.providers,
        shards=runtime.shards, fts=runtime.fts, cache=runtime.cache,
    )


def _doc_id(runtime, title: str) -> int:
    with runtime.db.sync_connect() as conn:
        return int(
            conn.execute("SELECT doc_id FROM ekbs_document WHERE title = ?", (title,)).fetchone()[
                "doc_id"
            ]
        )


def _shards_of(runtime, doc_id: int) -> set[str]:
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT shard_id FROM ekbs_chunk WHERE doc_id = ?", (doc_id,)
        ).fetchall()
    return {str(r["shard_id"]) for r in rows}


def _policy_version(runtime) -> int:
    with runtime.db.sync_connect() as conn:
        return int(conn.execute("SELECT version FROM ekbs_policy_version").fetchone()["version"])


async def test_level_upgrade_migrates_chunks_and_closes_leak(l3) -> None:
    """**回归测试**：L2 → L3 后，只有 L2 权限的用户不得再从旧分片召回到它。"""
    runtime = l3
    doc_id = _doc_id(runtime, "年假管理制度")
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))

    before = await runtime.pipeline.retrieve("年假有多少天", alice)
    assert any(c.doc_id == doc_id for c in before.chunks), "调密级前应能召回"
    assert _shards_of(runtime, doc_id) == {"kb_hr_L2"}

    version_before = _policy_version(runtime)
    result = await service(runtime).change_document_level(
        doc_id, 3, operator_id=1, reason="升级为秘密"
    )
    assert result.from_shard == "kb_hr_L2" and result.to_shard == "kb_hr_L3"
    assert result.migrated_chunks > 0
    assert _shards_of(runtime, doc_id) == {"kb_hr_L3"}
    assert result.policy_version == version_before + 1

    # 密级闸（level <= clearance）现在拦住 alice：走的是密级而不是「取回后再过滤」
    alice_after = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    assert "kb_hr_L3" not in alice_after.visible_shard_ids
    after = await runtime.pipeline.retrieve("年假有多少天", alice_after)
    assert all(c.doc_id != doc_id for c in after.chunks)

    # 审计留痕（合规要求 ≥365 天，00 §3.5）
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT change_type, field, old_value, new_value, policy_version_before, "
            "policy_version_after, reason FROM ekbs_perm_change_log ORDER BY change_id DESC LIMIT 1"
        ).fetchone()
    assert row["change_type"] == "level" and row["field"] == "level"
    assert (row["old_value"], row["new_value"]) == ("L2", "L3")
    assert row["policy_version_after"] == row["policy_version_before"] + 1


async def test_level_downgrade_migrates_to_lower_shard(seeded) -> None:
    """降密同样要迁移：否则高密级分片里躺着本可公开的内容，低权限用户反而看不到。"""
    runtime, _ = seeded
    doc_id = _doc_id(runtime, "财务保密制度")
    result = await service(runtime).change_document_level(doc_id, 2, reason="降为内部")
    assert result.from_shard == "kb_finance_L3"
    assert result.to_shard == "kb_finance_L2"
    assert _shards_of(runtime, doc_id) == {"kb_finance_L2"}


async def test_dept_change_migrates_shard(seeded) -> None:
    runtime, _ = seeded
    doc_id = _doc_id(runtime, "年假管理制度")
    result = await service(runtime).change_document_depts(
        doc_id, ["103"], reason="改由财务部维护"
    )
    assert result.to_shard == "kb_finance_L2"
    assert _shards_of(runtime, doc_id) == {"kb_finance_L2"}
    with runtime.db.sync_connect() as conn:
        depts = json.loads(
            conn.execute(
                "SELECT visible_dept_ids FROM ekbs_document WHERE doc_id = ?", (doc_id,)
            ).fetchone()["visible_dept_ids"]
        )
    assert depts == ["103"]


async def test_fts_rows_follow_the_migration(l3) -> None:
    """FTS5 行必须跟着换分片，否则稀疏召回仍会从旧分片命中（FAISS/FTS5 语义等价要求）。"""
    runtime = l3
    doc_id = _doc_id(runtime, "年假管理制度")
    await service(runtime).change_document_level(doc_id, 3)
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT c.shard_id AS shard_id FROM ekbs_chunk_fts f JOIN ekbs_chunk c "
            "ON c.chunk_id = CAST(f.chunk_id AS INTEGER) WHERE c.doc_id = ?",
            (doc_id,),
        ).fetchall()
    assert {str(r["shard_id"]) for r in rows} == {"kb_hr_L3"}

    # 三方对账：FTS **去重后的块数** == 已发布子块数 == FAISS 向量数
    #
    # ⚠️ 不能再拿 `runtime.fts.count()`（总行数）对账：Doc2Query 会给**同一个 chunk_id**
    #    追加若干「假设问句」行（`ekbs/ingest/doc2query.py`，同 chunk_id、不同 content），
    #    所以总行数天然大于块数（实测 79 vs 23）。要验的契约是「每个可检索子块都在 FTS 里」，
    #    按 **distinct chunk_id**（且限定已发布子块）对账才与 block 数一一对应。
    with runtime.db.sync_connect() as conn:
        children = int(
            conn.execute(
                "SELECT COUNT(*) FROM ekbs_chunk c JOIN ekbs_document d ON d.doc_id = c.doc_id "
                "WHERE c.chunk_type = 'child' AND d.status = 'published'"
            ).fetchone()[0]
        )
        fts_chunks = int(
            conn.execute(
                "SELECT COUNT(DISTINCT CAST(f.chunk_id AS INTEGER)) FROM ekbs_chunk_fts f "
                "JOIN ekbs_chunk c ON c.chunk_id = CAST(f.chunk_id AS INTEGER) "
                "JOIN ekbs_document d ON d.doc_id = c.doc_id "
                "WHERE c.chunk_type = 'child' AND d.status = 'published'"
            ).fetchone()[0]
        )
    assert fts_chunks == children
    # 迁移后旧分片里的陈旧向量仍在（IndexFlatIP 不支持删除，见 changes.py 的说明），
    # 因此 total_vectors() ≥ children 才是正确契约；关键是它们**召不回**：
    staleness = runtime.shards.total_vectors() - children
    assert staleness >= 0
    with runtime.db.sync_connect() as conn:
        old_rows = [
            int(r["faiss_row"])
            for r in conn.execute(
                "SELECT faiss_row FROM ekbs_chunk WHERE shard_id = 'kb_hr_L2' "
                "AND faiss_row IS NOT NULL"
            ).fetchall()
        ]
    assert old_rows == [], "迁移后 chunk 不应再挂在旧分片上"
    # 陈旧行号在按 shard_id 过滤的映射里查不到任何 chunk ⇒ 永远进不了召回结果
    assert runtime.pipeline._chunk_ids_by_faiss_row("kb_hr_L2", [0, 1, 2]) == {}


async def test_migration_target_shard_registration_is_consistent(l3) -> None:
    """`smoke.sh` 会比对 `idx.ntotal` 与 `ekbs_shard.vector_count`，迁移后必须仍然一致。"""
    runtime = l3
    doc_id = _doc_id(runtime, "年假管理制度")
    await service(runtime).change_document_level(doc_id, 3)
    checks = {c.shard_id: c for c in runtime.shards.self_check()}
    assert checks["kb_hr_L3"].ok, checks["kb_hr_L3"].detail
    assert checks["kb_hr_L2"].ok


async def test_user_clearance_change_bumps_policy_and_fingerprint(seeded) -> None:
    runtime, _ = seeded
    alice_id = user_id_of(runtime.db, "demo_alice")
    before = await runtime.perm_context(alice_id)
    result = await service(runtime).change_user_clearance(alice_id, 3, reason="晋升主管")
    after = await runtime.perm_context(alice_id)
    assert result.policy_version > before.policy_version
    assert after.clearance == 3
    assert after.fingerprint != before.fingerprint
    assert after.visible_shard_ids == ("kb_all_L1", "kb_hr_L2")


async def test_grant_acl_bumps_version_in_same_transaction(seeded) -> None:
    """ACL 与 policy_version 必须同事务自增，否则有「ACL 变了、指纹没变」的窗口。"""
    runtime, _ = seeded
    alice_id = user_id_of(runtime.db, "demo_alice")
    before = _policy_version(runtime)
    result = await service(runtime).grant_acl(
        object_type="shard", object_id="kb_finance_L3", subject_type="dept", subject_id="102",
        operator_id=1, reason="跨部门支援",
    )
    assert result.policy_version == before + 1
    # 但密级闸仍然拦住：ACL 是横向补丁，不能突破 clearance
    alice = await runtime.perm_context(alice_id)
    assert "kb_finance_L3" not in alice.visible_shard_ids


async def test_revoke_acl_and_missing_entry(seeded) -> None:
    runtime, _ = seeded
    svc = service(runtime)
    await svc.grant_acl(
        object_type="document", object_id="1", subject_type="user", subject_id="2",
        reason="临时授权",
    )
    result = await svc.revoke_acl(
        object_type="document", object_id="1", subject_type="user", subject_id="2",
        reason="到期回收",
    )
    assert result.change_type == "acl_revoke"
    with pytest.raises(ConfigError):
        await svc.revoke_acl(
            object_type="document", object_id="1", subject_type="user", subject_id="2"
        )


async def test_caches_are_invalidated_on_change(l3) -> None:
    """变更后旧指纹下的检索/答案缓存要被清掉（正确性靠指纹，清理是回收空间）。"""
    runtime = l3
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    await runtime.pipeline.answer("年假有多少天", alice)
    with runtime.db.sync_connect() as conn:
        before = int(conn.execute("SELECT COUNT(*) FROM ekbs_cache").fetchone()[0])
    assert before > 0
    await service(runtime).change_document_level(_doc_id(runtime, "年假管理制度"), 3)
    with runtime.db.sync_connect() as conn:
        retrieve_left = int(
            conn.execute(
                "SELECT COUNT(*) FROM ekbs_cache WHERE cache_key LIKE 'ekbs:cache:retrieve:%'"
            ).fetchone()[0]
        )
        answer_left = int(
            conn.execute(
                "SELECT COUNT(*) FROM ekbs_cache WHERE cache_key LIKE 'ekbs:cache:answer:%'"
            ).fetchone()[0]
        )
        embed_left = int(
            conn.execute(
                "SELECT COUNT(*) FROM ekbs_cache WHERE cache_key LIKE 'ekbs:cache:embed:%'"
            ).fetchone()[0]
        )
    assert (retrieve_left, answer_left) == (0, 0)
    # 查询嵌入缓存与权限无关，**刻意保留**（03 §4.4：可全局共享，清了反而浪费一次云端 RTT）
    assert embed_left >= 1


async def test_gates_and_noop_changes_are_rejected(seeded) -> None:
    runtime, _ = seeded
    svc = service(runtime)
    doc_id = _doc_id(runtime, "年假管理制度")

    with pytest.raises(ConfigError):
        await svc.change_document_level(doc_id, 2)  # 未变化
    with pytest.raises(ConfigError):
        await svc.change_document_depts(doc_id, ["102"])  # 未变化
    # C5 未验收前 L3 禁止（测试里没开闸）
    runtime.settings.permission.l3_ingest_enabled = False
    with pytest.raises(ConfigError):
        await svc.change_document_level(doc_id, 3)
    # L4 首期不入库
    with pytest.raises(ConfigError):
        await svc.change_document_level(doc_id, 4)
    with pytest.raises(ConfigError):
        await svc.change_document_level(999_999, 3)
