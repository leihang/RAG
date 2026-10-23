"""分片路由与 C5 闸门 —— **权限红线的核心用例**（`00` C1/C2/C5、`03` §4/§6）。"""

from __future__ import annotations

from tests.conftest import user_id_of


async def test_console_widen_opens_other_dept_within_clearance(seeded) -> None:
    """后台检索补部门，不提高密级，也不改钉钉问答用的原始路由。"""
    from ekbs.permission.acl import DocumentFacts, DocumentGate
    from ekbs.permission.console import widen_for_console

    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    wide = widen_for_console(alice, runtime.db)
    gate = DocumentGate(runtime.db, runtime.settings)
    kb_id = int(alice.visible_kb_ids[0])
    other = DocumentFacts(
        doc_id=9001, kb_id=kb_id, title="财务", level=2, status="published",
        visible_dept_ids=("103",), effective_date=None, expire_date=None,
    )
    secret = DocumentFacts(
        doc_id=9002, kb_id=kb_id, title="财务机密", level=3, status="published",
        visible_dept_ids=("103",), effective_date=None, expire_date=None,
    )
    assert gate.can_read_document(alice, other, []).allowed is False
    assert gate.can_read_document(wide, other, []).allowed is True
    assert gate.can_read_document(wide, secret, []).reason == "level"
    assert alice.visible_shard_ids == ("kb_all_L1", "kb_hr_L2")
    assert "kb_finance_L3" not in wide.visible_shard_ids


async def test_visible_shards_by_clearance_and_dept(seeded) -> None:
    """路由结果必须是「密级 ≤ clearance 且部门命中」，且顺序稳定（升序）。"""
    runtime, _summary = seeded
    carol = await runtime.perm_context(user_id_of(runtime.db, "demo_carol"))
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    bob = await runtime.perm_context(user_id_of(runtime.db, "demo_bob"))

    assert carol.visible_shard_ids == ("kb_all_L1",)
    assert alice.visible_shard_ids == ("kb_all_L1", "kb_hr_L2")
    assert bob.visible_shard_ids == ("kb_all_L1", "kb_finance_L3")
    # 顺序稳定是契约的一部分（06 §6.4.4 TC-SHARD-001）
    assert list(alice.visible_shard_ids) == sorted(alice.visible_shard_ids)


async def test_acl_allow_cannot_exceed_clearance(seeded) -> None:
    """ACL allow 是横向补丁，**不能突破 clearance 这条纵向上限**（00 §3.2 规则 1）。"""
    runtime, _ = seeded
    alice_id = user_id_of(runtime.db, "demo_alice")
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_acl_entry(object_type, object_id, subject_type, subject_id, "
            "permission, effect) VALUES ('shard', 'kb_finance_L3', 'dept', '102', 'read', 'allow')"
        )
        conn.commit()
    alice = await runtime.perm_context(alice_id)
    assert "kb_finance_L3" not in alice.visible_shard_ids  # L3 > clearance 2


async def test_acl_deny_beats_allow(seeded) -> None:
    """deny 优先：用户级 deny 必须压过部门级 allow（`03` §1.2 P1）。

    注意 Schema 的 ``UNIQUE(object_type, object_id, subject_type, subject_id, permission)``
    **不含 effect** —— 同一主体对同一对象只有一个效果，因此「deny 压 allow」实际发生在
    **不同主体之间**（本例：dept 102 的 allow vs user 的 deny）。
    """
    runtime, _ = seeded
    alice_id = user_id_of(runtime.db, "demo_alice")
    alice = await runtime.perm_context(alice_id)
    assert "kb_hr_L2" in alice.visible_shard_ids  # 部门 allow 生效
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_acl_entry(object_type, object_id, subject_type, subject_id, "
            "permission, effect) VALUES ('shard', 'kb_hr_L2', 'user', ?, 'read', 'deny')",
            (str(alice_id),),
        )
        conn.commit()
    after = await runtime.perm_context(alice_id)
    assert "kb_hr_L2" not in after.visible_shard_ids


async def test_fingerprint_is_stable_and_user_specific(seeded) -> None:
    """同一身份两次构建指纹相同；不同身份指纹不同（C2 的不变量）。"""
    runtime, _ = seeded
    alice_id = user_id_of(runtime.db, "demo_alice")
    bob_id = user_id_of(runtime.db, "demo_bob")
    first = await runtime.perm_context(alice_id)
    second = await runtime.perm_context(alice_id)
    bob = await runtime.perm_context(bob_id)
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint != bob.fingerprint


async def test_policy_version_bump_changes_fingerprint(seeded) -> None:
    """policy_version 自增是「全量缓存失效」的实现手段（00 §3.4）。"""
    runtime, _ = seeded
    alice_id = user_id_of(runtime.db, "demo_alice")
    before = await runtime.perm_context(alice_id)
    with runtime.db.sync_connect() as conn:
        from ekbs.permission.router import bump_policy_version

        bump_policy_version(conn, reason="test")
        conn.commit()
    after = await runtime.perm_context(alice_id)
    assert before.policy_version != after.policy_version
    assert before.fingerprint != after.fingerprint
    assert before.visible_shard_ids == after.visible_shard_ids  # 分片集合没变，只是版本变了


def _facts(**kwargs):
    from ekbs.permission.acl import DocumentFacts

    base = dict(
        doc_id=1, kb_id=1, title="t", level=2, status="published",
        visible_dept_ids=(), effective_date=None, expire_date=None, shard_id="kb_hr_L2",
    )
    base.update(kwargs)
    return DocumentFacts(**base)


async def test_c5_denies_wrong_department(seeded) -> None:
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    # 财务部文档（L3）对 HR 的 alice：密级与部门双重不符
    decision = runtime.gate.can_read_document(alice, _facts(level=3, visible_dept_ids=("103",)))
    assert decision.allowed is False
    assert decision.reason in {"level", "dept"}


async def test_c5_allows_same_department(seeded) -> None:
    """部门命中放行（shard 级 ACL 之外的常规路径）。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    decision = runtime.gate.can_read_document(
        alice, _facts(visible_dept_ids=("102",), shard_id=None)
    )
    assert decision.allowed is True
    assert decision.reason == "dept"


async def test_c5_deny_entry_beats_department_match(seeded) -> None:
    """片内 deny：分片粒度表达不了，必须靠 C5 兜底（00 C5）。"""
    from ekbs.permission.acl import AclEntry

    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    entries = [
        AclEntry(
            object_type="document", object_id="1", subject_type="user",
            subject_id=str(alice.user_id), permission="read", effect="deny",
        )
    ]
    decision = runtime.gate.can_read_document(
        alice, _facts(visible_dept_ids=("102",)), entries
    )
    assert decision.allowed is False
    assert decision.reason == "acl_deny"


async def test_c5_allows_cross_department_via_shard_acl(seeded) -> None:
    """K6 修正：跨部门文档在 06 §2.2 是靠 shard 级 allow 授权的，闸门必须认这条路径。"""
    from ekbs.permission.acl import AclEntry

    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    # kb_hr_L2 在 alice 的 acl_allow_shards 里；文档级无任何条目、部门不匹配
    assert "kb_hr_L2" in alice.acl_allow_shards
    decision = runtime.gate.can_read_document(
        alice, _facts(visible_dept_ids=("999",), shard_id="kb_hr_L2"), []
    )
    assert decision.allowed is True
    assert decision.reason == "shard_acl_allow"


async def test_c5_rejects_unpublished_and_expired(seeded) -> None:
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    assert runtime.gate.can_read_document(alice, _facts(status="offline")).reason == "status"
    assert (
        runtime.gate.can_read_document(alice, _facts(expire_date="2000-01-01")).reason == "expired"
    )


async def test_c5_l4_requires_explicit_allow(seeded) -> None:
    """L4 白名单：没有显式 allow 一律拒绝（00 §3.2 规则 5）。

    注意判定顺序：密级闸（``level > clearance``）在 L4 白名单之前，因此要让 L4 分支可达，
    用户得持有 clearance=4；clearance=3 的用户会先在密级闸被拒 —— 这是**更严**的结果。
    """
    runtime, _ = seeded
    bob_id = user_id_of(runtime.db, "demo_bob")
    bob = await runtime.perm_context(bob_id)
    assert runtime.gate.can_read_document(bob, _facts(level=4)).reason == "level"

    with runtime.db.sync_connect() as conn:
        conn.execute("UPDATE ekbs_user SET clearance = 4 WHERE user_id = ?", (bob_id,))
        conn.commit()
    bob_l4 = await runtime.perm_context(bob_id)
    assert bob_l4.clearance == 4
    assert runtime.gate.can_read_document(bob_l4, _facts(level=4)).reason == "l4_whitelist"


async def test_filter_chunks_reports_dropped(seeded) -> None:
    """C5 批量闸门要能分别返回放行与丢弃，丢弃者不得进入下游。"""
    runtime, _ = seeded
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT c.chunk_id, c.doc_id, d.title FROM ekbs_chunk c "
            "JOIN ekbs_document d ON d.doc_id = c.doc_id"
        ).fetchall()
    pairs = [(int(r["chunk_id"]), int(r["doc_id"])) for r in rows]
    finance_doc = next(int(r["doc_id"]) for r in rows if "财务" in str(r["title"]))
    kept, dropped = runtime.gate.filter_chunks(alice, pairs)
    assert dropped, "财务文档的块必须被 C5 丢弃"
    assert all(doc_id != finance_doc for _, doc_id in kept)
