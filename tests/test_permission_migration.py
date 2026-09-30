"""权限数据迁移（`ekbs.tools.migrate_permissions`）。

背景：权限点是**数据**不是代码 —— 我把自造的命名改成 `05` §8.2 的权威命名后，
已在跑的库不会自动跟着变，结果是所有人 403（实测踩到过：开发库未重建时 kb_admin 查文档被拒）。
这组用例把「旧库迁移到新目录」这条路钉住。
"""

from __future__ import annotations

import importlib

import pytest

from ekbs.db import Database
from ekbs.rbac_catalog import LEGACY_PERMISSION_MAP, PERMISSIONS, ROLE_GRANTS

migrate_tool = importlib.import_module("ekbs.tools.migrate_permissions")

#: 迁移前的“旧世界”：只有骨架早期自造的这几个权限点
LEGACY_PERMISSIONS = [
    ("kb:knowledge_base:create", "新建知识库", "kb"),
    ("kb:document:upload", "上传文档", "kb"),
    ("kb:document:publish", "发布文档", "kb"),
    ("qa:chat", "发起问答", "qa"),
    ("audit:qa:read", "查看问答日志", "audit"),
    ("system:model:config", "模型配置", "system"),
]

LEGACY_ROLE_GRANTS = {
    "end_user": ["qa:chat"],
    "kb_admin": ["kb:document:upload", "kb:document:publish", "kb:knowledge_base:create"],
}


@pytest.fixture
def legacy_db(settings) -> Database:
    """造一个「部署在旧版本」的库：旧权限点 + 旧授权 + 一个绑定了角色的用户。"""
    database = Database(settings)
    database.apply_schema()
    with database.sync_connect() as conn:
        conn.execute("DELETE FROM ekbs_permission")
        for code, name, module in LEGACY_PERMISSIONS:
            conn.execute(
                "INSERT INTO ekbs_permission(code, name, module) VALUES (?, ?, ?)",
                (code, name, module),
            )
        for code, name in (("super_admin", "超级管理员"), ("kb_admin", "知识库管理员"),
                           ("end_user", "普通用户")):
            conn.execute(
                "INSERT INTO ekbs_role(code, name, is_builtin, status, created_at) "
                "VALUES (?, ?, 1, 1, datetime('now'))",
                (code, name),
            )
        for role_code, codes in LEGACY_ROLE_GRANTS.items():
            for code in codes:
                conn.execute(
                    "INSERT INTO ekbs_role_permission(role_id, perm_id) "
                    "SELECT r.role_id, p.perm_id FROM ekbs_role r, ekbs_permission p "
                    "WHERE r.code = ? AND p.code = ?",
                    (role_code, code),
                )
        conn.execute(
            "INSERT INTO ekbs_user(dingtalk_userid, nick, status) VALUES ('legacy_user', '旧库用户', 'active')"
        )
        user_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "INSERT INTO ekbs_user_role(user_id, role_id, scope_type, scope_id) "
            "SELECT ?, role_id, 'global', 0 FROM ekbs_role WHERE code = 'kb_admin'",
            (user_id,),
        )
        conn.commit()
    return database


def _codes(database: Database) -> set[str]:
    with database.sync_connect() as conn:
        return {
            str(r["code"]) for r in conn.execute("SELECT code FROM ekbs_permission").fetchall()
        }


def _grants(database: Database, role_code: str) -> set[str]:
    with database.sync_connect() as conn:
        return {
            str(r["code"])
            for r in conn.execute(
                "SELECT p.code FROM ekbs_role_permission rp "
                "JOIN ekbs_role r ON r.role_id = rp.role_id "
                "JOIN ekbs_permission p ON p.perm_id = rp.perm_id WHERE r.code = ?",
                (role_code,),
            ).fetchall()
        }


def test_migration_aligns_catalog_and_grants(legacy_db: Database) -> None:
    report = migrate_tool.migrate(legacy_db)

    expected = {code for code, _, _ in PERMISSIONS}
    assert expected <= _codes(legacy_db), "目录里的权限点必须全部补齐"
    # 历史命名被合并、旧点被删除
    assert "kb:document:upload" not in _codes(legacy_db)
    assert "kb:document:publish" not in _codes(legacy_db)
    assert report.permissions_renamed == {
        "kb:document:upload": "doc:document:create",
        "kb:document:publish": "doc:document:publish",
    }
    # 废弃点（模型配置走 YAML，不暴露写接口）
    assert "system:model:config" in report.permissions_retired
    assert "system:model:config" not in _codes(legacy_db)

    # 旧授权被搬到新代码上：kb_admin 原有「上传」→ 现在有 doc:document:create
    kb_admin_grants = _grants(legacy_db, "kb_admin")
    assert "doc:document:create" in kb_admin_grants
    assert "doc:document:read" in kb_admin_grants
    # 并按 05 §3.4 重放矩阵
    assert set(ROLE_GRANTS["kb_admin"]) <= kb_admin_grants
    assert set(ROLE_GRANTS["end_user"]) <= _grants(legacy_db, "end_user")

    # 留痕（合规要求）
    with legacy_db.sync_connect() as conn:
        row = conn.execute(
            "SELECT change_type, reason FROM ekbs_perm_change_log "
            "WHERE object_type = 'rbac' ORDER BY change_id DESC LIMIT 1"
        ).fetchone()
    assert row is not None and row["change_type"] == "acl_grant"
    assert "05 §8.2" in row["reason"]


def test_migration_is_idempotent(legacy_db: Database) -> None:
    first = migrate_tool.migrate(legacy_db)
    second = migrate_tool.migrate(legacy_db)
    assert first.permissions_added
    assert second.permissions_added == []
    assert second.permissions_renamed == {} and second.permissions_retired == []
    assert second.grants_added == 0, "重复执行不该再新增授权"
    with legacy_db.sync_connect() as conn:
        duplicates = int(
            conn.execute(
                "SELECT COUNT(*) FROM (SELECT role_id, perm_id FROM ekbs_role_permission "
                "GROUP BY role_id, perm_id HAVING COUNT(*) > 1)"
            ).fetchone()[0]
        )
    assert duplicates == 0


def test_dry_run_changes_nothing(legacy_db: Database) -> None:
    before = _codes(legacy_db)
    report = migrate_tool.migrate(legacy_db, dry_run=True)
    assert report.dry_run is True
    assert report.permissions_added, "演练也要报告将要发生的变更"
    assert _codes(legacy_db) == before, "演练不得写库"
    with legacy_db.sync_connect() as conn:
        assert int(
            conn.execute(
                "SELECT COUNT(*) FROM ekbs_perm_change_log WHERE object_type = 'rbac'"
            ).fetchone()[0]
        ) == 0


def test_rbac_works_after_migration(legacy_db: Database) -> None:
    """迁移的意义：接口要求的权限点真的生效（否则线上就是一片 403）。"""
    from ekbs.authz import AuthzService

    with legacy_db.sync_connect() as conn:
        user_id = int(
            conn.execute(
                "SELECT user_id FROM ekbs_user WHERE dingtalk_userid = 'legacy_user'"
            ).fetchone()["user_id"]
        )
    authz = AuthzService(legacy_db)
    before = authz.load(user_id)
    assert not before.has("doc:document:read"), "旧库必然缺这个点"

    migrate_tool.migrate(legacy_db)
    after = authz.load(user_id)
    assert after.has("doc:document:read")
    assert after.has("doc:document:create")
    assert after.has("parse:task:read")


def test_legacy_map_targets_exist_in_catalog() -> None:
    """映射表的目标必须都在目录里 —— 否则迁移会把授权搬到空气上。"""
    catalog = {code for code, _, _ in PERMISSIONS}
    for legacy, target in LEGACY_PERMISSION_MAP.items():
        if target is not None:
            assert target in catalog, f"{legacy} → {target} 不在目录里"
