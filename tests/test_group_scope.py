"""群聊只允许公开文档：个人库必须从群聊权限/帮助里剪掉。"""

from __future__ import annotations

from ekbs.discovery import format_help_text
from ekbs.permission.context import PermContext
from ekbs.permission.public_scope import public_scope_perm
from ekbs.personal import ensure_personal_kb


def _perm(visible_shards, visible_kbs, allow=frozenset()) -> PermContext:
    return PermContext(
        user_id=5,
        dingtalk_userid="dt5",
        clearance=3,
        dept_chain=(),
        visible_shard_ids=tuple(visible_shards),
        visible_kb_ids=tuple(visible_kbs),
        policy_version=1,
        acl_allow_shards=frozenset(allow),
    )


async def test_public_scope_perm_removes_personal_shard_and_kb(seeded) -> None:
    runtime, _summary = seeded
    with runtime.db.sync_connect() as conn:
        personal_kb = ensure_personal_kb(conn, user_id=5, nick="雷航")
        public_kb = int(
            conn.execute(
                "SELECT kb_id FROM ekbs_kb WHERE code NOT LIKE 'personal-%' ORDER BY kb_id LIMIT 1"
            ).fetchone()["kb_id"]
        )
    perm = _perm(
        ["kb_all_L1", "kb_p5_L2"],
        [public_kb, personal_kb],
        allow={"kb_p5_L2"},
    )
    limited = public_scope_perm(perm, runtime.db)
    assert limited.visible_shard_ids == ("kb_all_L1",)
    assert limited.visible_kb_ids == (public_kb,)
    assert limited.acl_allow_shards == frozenset()
    assert limited.fingerprint != perm.fingerprint, "群聊范围必须换缓存指纹，不能串单聊答案"


def test_format_help_text_group_uses_public_wording() -> None:
    docs = [{"title": "XJWY-GC-09维修作业指导书", "doc_id": 1}]
    suggestions = [{"question": "「XJWY-GC-09维修作业指导书」主要讲了什么？"}]
    text = format_help_text(docs, suggestions, group_chat=True)
    assert "当前群聊可查询的公开文档" in text
    assert "私聊" in text
    assert "在单聊里直接发文件" not in text
    single = format_help_text(docs, suggestions)
    assert "你当前可见的文档" in single
    assert "在单聊里直接发文件" in single
