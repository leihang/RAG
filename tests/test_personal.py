"""个人私有库，以及人员变更后知识库可见范围是否跟着变。"""

from __future__ import annotations

import json

import pytest

from ekbs.index.shard import resolve_shard_for_document
from ekbs.permission.router import ShardRouter
from ekbs.errors import ConfigError
from ekbs.personal import (
    attach_personal_owners,
    delete_personal_file,
    list_personal_files,
    personal_accepted_message,
    personal_duplicate_message,
    personal_failed_message,
    personal_kb_name,
    personal_max_file_mb,
    personal_raw_rel,
    personal_raw_rel_from_code,
    personal_ready_message,
    personal_storage_limit_bytes,
    personal_shard_id,
    personal_upload_exceeds,
    personal_upload_reject_text,
    purge_demo_accounts,
    register_personal_upload,
    scope_ref,
)
from ekbs.discovery import is_personal_list_command, personal_delete_title
from ekbs.tools.sync_org import rebuild_dept_closure, refresh_user_dept_chain
from tests.conftest import user_id_of


def test_personal_raw_rel_uses_slash_not_dash() -> None:
    """管理台与钉钉必须共用 personal/<uid>，不能再写到 personal-<uid>。"""
    assert personal_raw_rel(5) == "personal/5"
    assert personal_raw_rel_from_code("personal-5") == "personal/5"
    assert personal_raw_rel_from_code("gongkai") is None


def test_personal_status_messages_say_processing_and_done() -> None:
    """落盘后的回执说明解析中；成功/失败由 worker 再推一次。"""
    assert "解析入库" in personal_accepted_message(title="九月汇报")
    assert personal_ready_message(title="九月汇报") == "处理完毕，可以使用了"
    assert personal_failed_message(title="九月汇报") == "处理失败，请换一份再发"
    assert "正在解析" in personal_duplicate_message(title="九月汇报", status="pending")
    assert personal_duplicate_message(title="九月汇报", status="published") == (
        "处理完毕，可以使用了"
    )


def test_register_personal_upload_message_is_processing(seeded) -> None:
    runtime, _ = seeded
    alice = user_id_of(runtime.db, "demo_alice")
    stored = register_personal_upload(
        runtime.db,
        runtime.settings,
        user_id=alice,
        filename="进度提示.txt",
        content=b"status-message-probe",
    )
    assert stored["created"] is True
    assert "解析入库" in stored["message"]
    again = register_personal_upload(
        runtime.db,
        runtime.settings,
        user_id=alice,
        filename="进度提示.txt",
        content=b"status-message-probe",
    )
    assert again["created"] is False
    assert "正在解析" in again["message"] or "私有库" in again["message"]


def test_personal_owner_label_has_name_and_ids(seeded) -> None:
    """个人库代号 personal-5 / p5 必须能还原成姓名、用户编号和钉钉 ID。"""
    runtime, _ = seeded
    alice = user_id_of(runtime.db, "demo_alice")
    with runtime.db.sync_connect() as conn:
        person = conn.execute(
            "SELECT nick, dingtalk_userid FROM ekbs_user WHERE user_id = ?", (alice,)
        ).fetchone()
        rows = [
            {"code": f"personal-{alice}"},
            {"dept_code": f"p{alice}"},
            {"kb_code": "main"},
        ]
        attach_personal_owners(conn, rows)
    assert rows[0]["owner_user_id"] == alice
    assert rows[0]["owner_nick"] == person["nick"]
    assert rows[0]["owner_dingtalk_userid"] == person["dingtalk_userid"]
    assert rows[1]["owner_nick"] == person["nick"]
    assert "owner_user_id" not in rows[2]
    assert personal_kb_name(alice, str(person["nick"])) == f"{person['nick']}（用户 {alice}）的私有库"


def test_personal_commands_do_not_swallow_ordinary_questions() -> None:
    assert is_personal_list_command("我的文件")
    assert personal_delete_title("删除我的文件 合同") == "合同"
    assert personal_delete_title("年假有多少天") is None
    assert personal_delete_title("删除我的年假") is None
    assert personal_delete_title("把财务制度发给我") is None


def test_personal_shard_is_not_a_department(seeded) -> None:
    runtime, _ = seeded
    placement = resolve_shard_for_document(
        level=2, visible_dept_ids=["user:5"], settings=runtime.settings
    )
    assert placement.primary_shard_id == "kb_p5_L2"


def test_upload_list_and_delete_stay_inside_one_user(seeded) -> None:
    runtime, _ = seeded
    alice = user_id_of(runtime.db, "demo_alice")
    bob = user_id_of(runtime.db, "demo_bob")
    stored = register_personal_upload(
        runtime.db, runtime.settings,
        user_id=alice, filename="私人备忘.txt", content="只有 Alice 能看".encode(),
    )
    assert stored["created"] is True
    assert stored["status"] == "pending"
    with runtime.db.sync_connect() as conn:
        path = str(conn.execute(
            "SELECT source_path FROM ekbs_document WHERE doc_id = ?",
            (stored["doc_id"],),
        ).fetchone()["source_path"])
    assert f"/personal/{alice}/" in path.replace("\\", "/")
    assert f"/personal-{alice}/" not in path.replace("\\", "/")
    again = register_personal_upload(
        runtime.db, runtime.settings,
        user_id=alice, filename="私人备忘.txt", content="只有 Alice 能看".encode(),
    )
    assert again["created"] is False and again["doc_id"] == stored["doc_id"]
    assert [item["title"] for item in list_personal_files(runtime.db, user_id=alice)] == ["私人备忘"]
    assert list_personal_files(runtime.db, user_id=bob) == []
    denied = delete_personal_file(runtime.db, user_id=bob, title="私人备忘")
    assert denied["deleted"] is False
    removed = delete_personal_file(runtime.db, user_id=alice, title="私人备忘")
    assert removed["deleted"] is True
    assert list_personal_files(runtime.db, user_id=alice) == []


def _insert_shard(conn, shard_id: str, dept_code: str, level: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO ekbs_shard(shard_id, dept_code, level, faiss_path, dim, "
        "embedding_fingerprint, vector_count, status) "
        "VALUES (?, ?, ?, 'unused', 8, 'test', 0, 'active')",
        (shard_id, dept_code, level),
    )


def test_personnel_change_moves_dept_visibility_but_keeps_personal(seeded) -> None:
    """调岗丢掉原部门知识库、获得新部门；私有库仍在。离职后连私有库也不可见。别人始终看不到。"""
    runtime, _ = seeded
    alice = user_id_of(runtime.db, "demo_alice")
    bob = user_id_of(runtime.db, "demo_bob")
    shard = personal_shard_id(alice)
    with runtime.db.sync_connect() as conn:
        _insert_shard(conn, shard, f"p{alice}", 2)
        _insert_shard(conn, "kb_finance_L2", "finance", 2)
        kb_id = conn.execute(
            "SELECT kb_id FROM ekbs_kb WHERE code = ?", (f"personal-{alice}",)
        ).fetchone()
        if kb_id is None:
            conn.execute(
                "INSERT INTO ekbs_kb(code, name, default_level, retrieval_config, status) "
                "VALUES (?, 'Alice的私有库', 2, ?, 1)",
                (
                    f"personal-{alice}",
                    json.dumps({"personal": True, "owner_user_id": alice}),
                ),
            )
            kb_id = conn.execute(
                "SELECT kb_id FROM ekbs_kb WHERE code = ?", (f"personal-{alice}",)
            ).fetchone()
        doc = conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, visible_dept_ids, "
            "status, created_by) VALUES (?, '私人备忘', 'raw/personal/note.txt', 2, ?, "
            "'published', ?)",
            (int(kb_id["kb_id"]), json.dumps([scope_ref(alice)]), alice),
        )
        doc_id = int(doc.lastrowid)
        conn.execute(
            "INSERT INTO ekbs_chunk(doc_id, seq, content, shard_id) VALUES (?, 1, 'secret', ?)",
            (doc_id, shard),
        )
        conn.execute(
            "INSERT INTO ekbs_acl_entry(object_type, object_id, subject_type, subject_id, "
            "permission, effect) VALUES ('shard', ?, 'user', ?, 'read', 'allow')",
            (shard, str(alice)),
        )
        conn.commit()

    router = ShardRouter(runtime.db, runtime.settings)
    before = router.route(alice)
    assert "kb_hr_L2" in before.visible_shard_ids
    assert shard in before.visible_shard_ids
    assert shard not in router.route(bob).visible_shard_ids

    gate = runtime.gate
    facts = gate.load_facts([doc_id])[doc_id]
    alice_ctx = router.build_context(alice)
    bob_ctx = router.build_context(bob)
    assert gate.can_see_document(alice_ctx, facts, for_retrieval=True).allowed
    assert not gate.can_see_document(bob_ctx, facts, for_retrieval=True).allowed

    with runtime.db.sync_connect() as conn:
        finance = int(conn.execute(
            "SELECT dept_id FROM ekbs_dept WHERE dingtalk_dept_id = '103'"
        ).fetchone()["dept_id"])
        conn.execute("DELETE FROM ekbs_user_dept WHERE user_id = ?", (alice,))
        conn.execute(
            "INSERT INTO ekbs_user_dept(user_id, dept_id, is_primary) VALUES (?, ?, 1)",
            (alice, finance),
        )
        rebuild_dept_closure(conn)
        refresh_user_dept_chain(conn)
        conn.commit()

    moved = router.route(alice)
    assert "kb_hr_L2" not in moved.visible_shard_ids
    assert "kb_finance_L2" in moved.visible_shard_ids
    assert shard in moved.visible_shard_ids
    moved_ctx = router.build_context(alice)
    assert gate.can_see_document(moved_ctx, facts, for_retrieval=True).allowed

    with runtime.db.sync_connect() as conn:
        conn.execute("UPDATE ekbs_user SET status = 'left' WHERE user_id = ?", (alice,))
        conn.commit()
    with pytest.raises(PermissionError):
        router.route(alice)


def test_purge_removes_demo_people_and_empty_demo_departments(seeded) -> None:
    runtime, _ = seeded
    report = purge_demo_accounts(runtime.db)
    assert "demo_alice" in report["users"]
    assert "人力资源部" in report["departments"]
    assert "财务部" in report["departments"]
    with runtime.db.sync_connect() as conn:
        alice = conn.execute(
            "SELECT status, deleted_at FROM ekbs_user WHERE dingtalk_userid = 'demo_alice'"
        ).fetchone()
        assert alice["status"] == "left" and alice["deleted_at"]
        hr = conn.execute(
            "SELECT deleted_at FROM ekbs_dept WHERE dingtalk_dept_id = '102'"
        ).fetchone()
        assert hr["deleted_at"]
        # 总部不是演示空壳（演示人员清掉后部门 id 不是 102/103），仍保留
        root = conn.execute(
            "SELECT deleted_at FROM ekbs_dept WHERE dingtalk_dept_id = '1'"
        ).fetchone()
        assert root["deleted_at"] is None


def test_personal_upload_limit_matches_admin_config(seeded) -> None:
    """钉钉单聊入库与管理台上传共用 limits.max_file_size_mb，缺省 100。"""
    runtime, _ = seeded
    assert runtime.settings.limits["max_file_size_mb"] == 100
    assert personal_max_file_mb(runtime.settings) == 100
    limit = 100 * 1024 * 1024
    assert personal_upload_exceeds(limit, runtime.settings) is False
    assert personal_upload_exceeds(limit + 1, runtime.settings) is True
    assert personal_upload_reject_text(runtime.settings) == "文件超过 100 MB，私有库暂不接收"

    missing = dict(runtime.settings.limits)
    missing.pop("max_file_size_mb", None)
    runtime.settings.limits = missing
    assert personal_max_file_mb(runtime.settings) == 100

    runtime.settings.limits = {**missing, "max_file_size_mb": 1}
    alice = user_id_of(runtime.db, "demo_alice")
    one_mb = b"a" * (1 * 1024 * 1024)
    stored = register_personal_upload(
        runtime.db, runtime.settings,
        user_id=alice, filename="刚好.txt", content=one_mb,
    )
    assert stored["created"] is True
    with pytest.raises(ConfigError, match="超过 1 MB"):
        register_personal_upload(
            runtime.db, runtime.settings,
            user_id=alice, filename="超了.txt", content=one_mb + b"b",
        )


def test_personal_storage_cap_blocks_the_next_file_and_frees_on_delete(seeded) -> None:
    """每人私有库总容量默认 500 MB；用满后再传会被拒，删掉后额度退回。"""
    runtime, _ = seeded
    missing = dict(runtime.settings.limits)
    missing.pop("personal_storage_mb", None)
    runtime.settings.limits = missing
    assert personal_storage_limit_bytes(runtime.settings) == 500 * 1024 * 1024

    runtime.settings.limits = {**missing, "personal_storage_mb": 2, "max_file_size_mb": 100}
    alice = user_id_of(runtime.db, "demo_alice")
    one_mb = b"a" * (1 * 1024 * 1024)
    first = register_personal_upload(
        runtime.db, runtime.settings, user_id=alice, filename="甲.txt", content=one_mb,
    )
    second = register_personal_upload(
        runtime.db, runtime.settings, user_id=alice, filename="乙.txt", content=b"b" * (1 * 1024 * 1024),
    )
    assert first["created"] is True and second["created"] is True
    with pytest.raises(ConfigError, match="最多存放 2 MB"):
        register_personal_upload(
            runtime.db, runtime.settings, user_id=alice, filename="丙.txt", content=b"c",
        )
    again = register_personal_upload(
        runtime.db, runtime.settings, user_id=alice, filename="甲.txt", content=one_mb,
    )
    assert again["created"] is False
    removed = delete_personal_file(runtime.db, user_id=alice, title="乙")
    assert removed["deleted"] is True
    third = register_personal_upload(
        runtime.db, runtime.settings,
        user_id=alice, filename="丙.txt", content=b"c" * (1 * 1024 * 1024),
    )
    assert third["created"] is True


def test_personal_delete_removes_raw_file_and_empty_folder(seeded) -> None:
    """删私有库文件要真删干净：原件 + 空文件夹。

    回归：之前只软删 DB 行（status='deleted'），``raw/personal/<uid>/`` 里的原件和
    空目录会一直留着 —— 用户删完文件后个人文件夹还在。
    """
    runtime, _ = seeded
    alice = user_id_of(runtime.db, "demo_alice")
    registered = register_personal_upload(
        runtime.db, runtime.settings, user_id=alice, filename="甲.txt", content=b"x",
    )
    raw_dir = runtime.settings.data_paths()["raw"] / "personal" / str(alice)
    source = raw_dir / "甲.txt"
    assert source.is_file(), f"前置条件：个人原件应存在（{source}）"

    removed = delete_personal_file(
        runtime.db, user_id=alice, title="甲", runtime=runtime,
    )

    assert removed["deleted"] is True
    assert not source.exists(), "个人原件应被删除"
    assert not raw_dir.exists(), "空掉的个人文件夹应被清理"
    with runtime.db.sync_connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM ekbs_document WHERE doc_id = ?",
            (registered["doc_id"],),
        ).fetchone()["n"] == 0
