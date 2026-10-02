"""权限模块的补充用例 —— 把 `02` 的覆盖率要求（权限模块 ≥95%）补到位。

覆盖的都是「真实会走到但主用例没覆盖」的分支：ACL 批量加载、文档级 allow、
kb 级 deny、安全事件留痕、离职用户无分片。
"""

from __future__ import annotations

import pytest

from tests.conftest import user_id_of


async def test_load_facts_and_acl_batch(seeded) -> None:
    """C5 的批量加载接口：一次 SQL 取回事实与 ACL，避免候选循环里 N+1。"""
    runtime, _ = seeded
    with runtime.db.sync_connect() as conn:
        doc_ids = [
            int(r["doc_id"]) for r in conn.execute("SELECT doc_id FROM ekbs_document").fetchall()
        ]
    facts = runtime.gate.load_facts(doc_ids)
    assert set(facts) == set(doc_ids)
    sample = facts[doc_ids[0]]
    assert sample.status == "published"
    assert isinstance(sample.visible_dept_ids, tuple)
    assert sample.shard_id is not None

    acl = runtime.gate.load_document_acl(doc_ids)
    assert set(acl) == set(doc_ids)
    assert all(isinstance(entries, list) for entries in acl.values())
    # 空输入不应打库
    assert runtime.gate.load_facts([]) == {}
    assert runtime.gate.load_document_acl([]) == {}


async def test_document_level_allow_overrides_missing_department(seeded) -> None:
    """文档级 allow：跨部门授权的另一条路径（03 §6.3 的 4.2 步）。"""
    runtime, _ = seeded
    alice_id = user_id_of(runtime.db, "demo_alice")
    alice = await runtime.perm_context(alice_id)
    with runtime.db.sync_connect() as conn:
        doc_id = int(
            conn.execute("SELECT doc_id FROM ekbs_document WHERE title = '年假管理制度'").fetchone()[
                "doc_id"
            ]
        )
        conn.execute(
            "INSERT INTO ekbs_acl_entry(object_type, object_id, subject_type, subject_id, "
            "permission, effect) VALUES ('document', ?, 'user', ?, 'read', 'allow')",
            (str(doc_id), str(alice_id)),
        )
        conn.commit()
    facts = runtime.gate.load_facts([doc_id])[doc_id]
    entries = runtime.gate.load_document_acl([doc_id])[doc_id]
    # 把部门改成不匹配，验证「靠 ACL allow 放行」而不是靠部门
    from ekbs.permission.acl import DocumentFacts

    unmatched = DocumentFacts(
        doc_id=facts.doc_id, kb_id=facts.kb_id, title=facts.title, level=facts.level,
        status=facts.status, visible_dept_ids=("999",), effective_date=None,
        expire_date=None, shard_id=None,
    )
    decision = runtime.gate.can_read_document(alice, unmatched, entries)
    assert decision.allowed is True
    assert decision.reason == "acl_allow"


async def test_kb_level_deny_hides_document(seeded) -> None:
    """kb 级 deny 收敛可见知识库，进而让 C5 在第 3 步拒绝（03 §6.3）。"""
    runtime, _ = seeded
    alice_id = user_id_of(runtime.db, "demo_alice")
    with runtime.db.sync_connect() as conn:
        kb_id = int(conn.execute("SELECT kb_id FROM ekbs_kb LIMIT 1").fetchone()["kb_id"])
        # ⚠️ 必须**(a) 限定密级 (b) 带 ORDER BY**：
        #    C5 的第 2 步（密级）在 kb 之前，取到 L3 文档就会以 reason='level' 拒绝，
        #    掩盖本用例要验的 kb 步；而 `LIMIT 1` 不带 ORDER BY 时返回哪一行由
        #    **执行计划**决定 —— 本轮新增 idx_doc_deleted 后就改取到了 L3 文档而误红。
        doc_id = int(
            conn.execute(
                "SELECT doc_id FROM ekbs_document WHERE level <= 2 ORDER BY doc_id LIMIT 1"
            ).fetchone()["doc_id"]
        )
        conn.execute(
            "INSERT INTO ekbs_acl_entry(object_type, object_id, subject_type, subject_id, "
            "permission, effect) VALUES ('kb', ?, 'user', ?, 'read', 'deny')",
            (str(kb_id), str(alice_id)),
        )
        conn.commit()
    alice = await runtime.perm_context(alice_id)
    assert kb_id not in alice.visible_kb_ids
    facts = runtime.gate.load_facts([doc_id])[doc_id]
    decision = runtime.gate.can_read_document(alice, facts, [])
    assert decision.allowed is False
    assert decision.reason == "kb"


async def test_record_security_event_writes_append_only_row(seeded) -> None:
    """C5 拒绝与幻觉引用都要留痕到 ekbs_security_event（03 §5.2.1）。"""
    runtime, _ = seeded
    runtime.gate.record_security_event(
        "access_denied", user_id=1, severity="high", detail={"chunk_id": 5, "reason": "c5_denied"}
    )
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT event_type, severity, detail FROM ekbs_security_event ORDER BY event_id DESC "
            "LIMIT 1"
        ).fetchone()
        assert row["event_type"] == "access_denied"
        assert row["severity"] == "high"
        assert "c5_denied" in row["detail"]
        # append-only：改不了
        import sqlite3

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE ekbs_security_event SET severity = 'low'")


async def test_route_rejects_non_active_user(seeded) -> None:
    """离职/停用用户不参与任何检索（04 §4.6：身份失效必须立刻生效）。"""
    runtime, _ = seeded
    alice_id = user_id_of(runtime.db, "demo_alice")
    with runtime.db.sync_connect() as conn:
        conn.execute("UPDATE ekbs_user SET status = 'left' WHERE user_id = ?", (alice_id,))
        conn.commit()
    with pytest.raises(PermissionError):
        await runtime.perm_context(alice_id)


async def test_route_rejects_unknown_user(seeded) -> None:
    runtime, _ = seeded
    with pytest.raises(PermissionError):
        await runtime.perm_context(999_999)


async def test_fingerprint_has_no_timestamp_component(seeded) -> None:
    """指纹里不能含 now_ts，否则缓存永不命中（03 §4.3）。"""
    import time

    runtime, _ = seeded
    alice_id = user_id_of(runtime.db, "demo_alice")
    first = await runtime.perm_context(alice_id)
    time.sleep(1.1)
    second = await runtime.perm_context(alice_id)
    assert first.fingerprint == second.fingerprint


def test_shard_placement_rules(settings) -> None:
    """分片归属判定（06 §2.2 / docs/spec/20 §4）：L4 拒绝、L1 无部门落公共分片、L2 必须给部门。"""
    from ekbs.errors import ConfigError
    from ekbs.index.shard import resolve_shard_for_document

    l1 = resolve_shard_for_document(level=1, visible_dept_ids=[], settings=settings)
    assert l1.primary_shard_id == "kb_all_L1"

    l2 = resolve_shard_for_document(level=2, visible_dept_ids=["102"], settings=settings)
    assert l2.primary_shard_id == "kb_hr_L2"

    multi = resolve_shard_for_document(level=2, visible_dept_ids=["102", "103"], settings=settings)
    assert multi.primary_shard_id == "kb_hr_L2"
    assert multi.extra_grants == (("103", "kb_hr_L2"),)

    with pytest.raises(ConfigError):
        resolve_shard_for_document(level=2, visible_dept_ids=[], settings=settings)
    with pytest.raises(ConfigError):
        resolve_shard_for_document(level=4, visible_dept_ids=["102"], settings=settings)
    with pytest.raises(ConfigError):
        resolve_shard_for_document(level=2, visible_dept_ids=["不存在的部门"], settings=settings)


async def test_ingest_rejects_l3_when_gate_closed(seeded) -> None:
    """L3 闸门必须真的拦在入库路径上（00 C5）—— 不是「配置项摆着好看」。"""
    from ekbs.errors import ConfigError
    from ekbs.ingest.pipeline import IngestPipeline

    runtime, summary = seeded
    pipeline = IngestPipeline(
        db=runtime.db, settings=runtime.settings, providers=runtime.providers,
        shards=runtime.shards, fts=runtime.fts,
    )
    assert runtime.settings.permission.l3_ingest_enabled is False
    with pytest.raises(ConfigError):
        await pipeline.ingest_markdown(
            title="机密测试", markdown="# 机密\n\n正文", kb_id=int(summary["kb_id"]),
            level=3, visible_dept_ids=["103"],
        )


async def test_document_acl_expired_and_malformed_are_ignored(seeded) -> None:
    """过期 ACL 不生效；object_id 非法的行被跳过而不是让整个查询炸掉。"""
    runtime, _ = seeded
    alice_id = user_id_of(runtime.db, "demo_alice")
    with runtime.db.sync_connect() as conn:
        # 同上：挑一份 Alice 密级读得到的文档，否则会在密级步就拒，验不到"过期 ACL 不生效"
        doc_id = int(
            conn.execute(
                "SELECT doc_id FROM ekbs_document WHERE level <= 2 ORDER BY doc_id LIMIT 1"
            ).fetchone()["doc_id"]
        )
        conn.execute(
            "INSERT INTO ekbs_acl_entry(object_type, object_id, subject_type, subject_id, "
            "permission, effect, expire_at) VALUES ('document', ?, 'user', ?, 'read', 'allow', "
            "'2000-01-01 00:00:00')",
            (str(doc_id), str(alice_id)),
        )
        conn.execute(
            "INSERT INTO ekbs_acl_entry(object_type, object_id, subject_type, subject_id, "
            "permission, effect) VALUES ('document', 'not-a-number', 'user', ?, 'read', 'allow')",
            (str(alice_id),),
        )
        conn.commit()
    entries = runtime.gate.load_document_acl([doc_id])[doc_id]
    assert entries == []  # 过期条目已在 SQL 层被过滤


async def test_acl_permission_kind_matters(seeded) -> None:
    """只有 read/manage 授予读；write/audit 不算（03 §6.3）。"""
    from ekbs.permission.acl import AclEntry, DocumentFacts

    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    write_only = [
        AclEntry(
            object_type="document", object_id="1", subject_type="user",
            subject_id=str(alice.user_id), permission="write", effect="allow",
        )
    ]
    facts = DocumentFacts(
        doc_id=1, kb_id=alice.visible_kb_ids[0], title="t", level=2, status="published",
        visible_dept_ids=("999",), effective_date=None, expire_date=None, shard_id=None,
    )
    decision = runtime.gate.can_read_document(alice, facts, write_only)
    assert decision.allowed is False and decision.reason == "dept"


async def test_document_facts_tolerates_bad_json(seeded) -> None:
    """visible_dept_ids 存了坏 JSON 时按「无部门限制」处理，不抛异常（防御性）。"""
    runtime, _ = seeded
    with runtime.db.sync_connect() as conn:
        # 任意文档都行，但必须带 ORDER BY：不带时取哪一行由执行计划决定（加索引就会变）
        doc_id = int(
            conn.execute("SELECT doc_id FROM ekbs_document ORDER BY doc_id LIMIT 1").fetchone()[
                "doc_id"
            ]
        )
        conn.execute(
            "UPDATE ekbs_document SET visible_dept_ids = 'not-json' WHERE doc_id = ?", (doc_id,)
        )
        conn.commit()
    facts = runtime.gate.load_facts([doc_id])[doc_id]
    assert facts.visible_dept_ids == ()


async def test_effective_date_in_future_denies(seeded) -> None:
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    from ekbs.permission.acl import DocumentFacts

    facts = DocumentFacts(
        doc_id=1, kb_id=alice.visible_kb_ids[0], title="t", level=2, status="published",
        visible_dept_ids=("102",), effective_date="2999-01-01", expire_date=None, shard_id=None,
    )
    decision = runtime.gate.can_read_document(alice, facts, [])
    assert decision.allowed is False and decision.reason == "not_effective"


async def test_dept_subjects_include_integer_ids(seeded) -> None:
    """部门主体同时接受 dingtalk ID 与整数 dept_id（K1 值域未定稿的兼容）。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    assert "102" in alice.dept_subjects  # dingtalk id
    with runtime.db.sync_connect() as conn:
        integer_ids = {
            str(r["dept_id"])
            for r in conn.execute("SELECT dept_id FROM ekbs_dept").fetchall()
        }
    assert alice.dept_subjects & integer_ids  # 至少命中一个整数 dept_id


async def test_matched_dept_codes_recorded(seeded) -> None:
    """路由结果带上命中的 dept_code，便于审计与排障。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    assert "hr" in alice.matched_dept_codes
