"""入库时点名的个人：只打开这些文件，不打开同部门其他人，也不打开个人库。"""

from __future__ import annotations

import json

import pytest

from ekbs.errors import ConfigError
from ekbs.permission.viewers import assert_viewers_assignable
from tests.conftest import user_id_of


def _shard(conn, shard_id: str, dept_code: str, level: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO ekbs_shard(shard_id, dept_code, level, faiss_path, dim, "
        "embedding_fingerprint, vector_count, status) "
        "VALUES (?, ?, ?, 'unused', 8, 'test', 0, 'active')",
        (shard_id, dept_code, level),
    )


def _doc(conn, kb_id: int, title: str, level: int, dept_id: str, shard_id: str) -> int:
    cursor = conn.execute(
        "INSERT INTO ekbs_document(kb_id, title, source_path, level, visible_dept_ids, status) "
        "VALUES (?, ?, 'raw/wuye/a.docx', ?, ?, 'published')",
        (kb_id, title, level, json.dumps([dept_id])),
    )
    doc_id = int(cursor.lastrowid)
    conn.execute(
        "INSERT INTO ekbs_chunk(doc_id, seq, content, shard_id) VALUES (?, 1, '正文', ?)",
        (doc_id, shard_id),
    )
    return doc_id


async def test_named_person_sees_only_the_granted_file(seeded) -> None:
    runtime, _ = seeded
    alice = user_id_of(runtime.db, "demo_alice")
    bob = user_id_of(runtime.db, "demo_bob")
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, default_level, status) VALUES ('wuye-viewers', '物业', 2, 1)"
        )
        kb_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        _shard(conn, "kb_97486022_L2", "97486022", 2)
        _shard(conn, "kb_97486022_L3", "97486022", 3)
        granted = _doc(conn, kb_id, "点名给 Alice", 2, "97486022", "kb_97486022_L2")
        other = _doc(conn, kb_id, "同部门另一份", 2, "97486022", "kb_97486022_L2")
        secret = _doc(conn, kb_id, "超过密级", 3, "97486022", "kb_97486022_L3")
        conn.execute(
            "INSERT INTO ekbs_acl_entry(object_type, object_id, subject_type, subject_id, "
            "permission, effect) VALUES ('document', ?, 'user', ?, 'read', 'allow')",
            (str(granted), str(alice)),
        )
        conn.execute(
            "INSERT INTO ekbs_acl_entry(object_type, object_id, subject_type, subject_id, "
            "permission, effect) VALUES ('document', ?, 'user', ?, 'read', 'allow')",
            (str(secret), str(alice)),
        )
        conn.commit()

    alice_perm = await runtime.perm_context(alice)
    bob_perm = await runtime.perm_context(bob)
    assert "kb_97486022_L2" in alice_perm.visible_shard_ids
    assert "kb_97486022_L2" not in alice_perm.acl_allow_shards
    assert "kb_97486022_L3" not in alice_perm.visible_shard_ids
    assert "kb_97486022_L2" not in bob_perm.visible_shard_ids

    facts = runtime.gate.load_facts([granted, other])
    acl = runtime.gate.load_document_acl([granted, other])
    assert runtime.gate.can_read_document(alice_perm, facts[granted], acl[granted]).allowed
    assert runtime.gate.can_read_document(alice_perm, facts[other], acl[other]).allowed is False
    assert runtime.gate.can_read_document(bob_perm, facts[granted], acl[granted]).allowed is False


def test_personal_library_rejects_named_viewers(seeded) -> None:
    runtime, _ = seeded
    alice = user_id_of(runtime.db, "demo_alice")
    bob = user_id_of(runtime.db, "demo_bob")
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, default_level, retrieval_config, status) "
            "VALUES (?, '私有', 2, ?, 1)",
            (f"personal-{alice}", json.dumps({"personal": True, "owner_user_id": alice})),
        )
        kb_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        with pytest.raises(ConfigError, match="个人库"):
            assert_viewers_assignable(conn, kb_id=kb_id, viewer_user_ids=[bob])
        assert assert_viewers_assignable(conn, kb_id=kb_id, viewer_user_ids=[]) == []
        with pytest.raises(ConfigError, match="不存在或已停用"):
            assert_viewers_assignable(
                conn,
                kb_code="wuye-viewers",
                viewer_user_ids=[999999],
            )
