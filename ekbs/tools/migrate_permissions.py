"""``python -m ekbs.tools.migrate_permissions`` —— 权限数据迁移（幂等）。

**为什么需要它**：权限点是**数据**（``ekbs_permission`` / ``ekbs_role_permission``），
不是代码。第 10~11 轮我把自造的权限点命名改成 `05` §8.2 的权威命名，
新库由 ``seed_demo`` 建对，但**已经在跑的库不会自动跟着变** ——
于是「接口要求 ``doc:document:read``，而库里只有 ``kb:document:upload``」，
结果是所有人 403（实测踩到：开发库没重建时 kb_admin 查文档被拒）。

本工具做四件事（全部幂等，可重复执行）：

1. **补权限点**：把 :data:`ekbs.rbac_catalog.PERMISSIONS` 里缺的插入 ``ekbs_permission``；
2. **合并历史命名**：按 :data:`~ekbs.rbac_catalog.LEGACY_PERMISSION_MAP` 把旧代码的
   ``role_permission`` 授权搬到新代码上，再删掉旧权限点（FK 级联清掉残留授权）；
3. **按 `05` §3.4 重放角色授权**：:data:`~ekbs.rbac_catalog.ROLE_GRANTS`；
4. **留痕**：往 ``ekbs_perm_change_log`` 写一条迁移记录（谁在何时改了什么）。

用法：

.. code-block:: bash

    python -m ekbs.tools.migrate_permissions --dry-run   # 先看会发生什么
    python -m ekbs.tools.migrate_permissions             # 实际执行
    python -m ekbs.tools.migrate_permissions --json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Any

from ekbs.config import load_settings
from ekbs.db import Database
from ekbs.logging import configure_logging, get_logger
from ekbs.rbac_catalog import (
    BUILTIN_ROLES,
    LEGACY_PERMISSION_MAP,
    PERMISSIONS,
    ROLE_GRANTS,
)

log = get_logger(component="tools.migrate_permissions")


@dataclass
class MigrationReport:
    dry_run: bool = False
    permissions_added: list[str] = field(default_factory=list)
    permissions_renamed: dict[str, str] = field(default_factory=dict)
    permissions_retired: list[str] = field(default_factory=list)
    grants_added: int = 0
    roles_created: list[str] = field(default_factory=list)
    users_affected: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "permissions_added": self.permissions_added,
            "permissions_renamed": self.permissions_renamed,
            "permissions_retired": self.permissions_retired,
            "grants_added": self.grants_added,
            "roles_created": self.roles_created,
            "users_affected": self.users_affected,
        }


def migrate(db: Database, *, dry_run: bool = False, operator_id: int | None = None) -> MigrationReport:
    report = MigrationReport(dry_run=dry_run)
    with db.sync_connect() as conn:
        # ① 内置角色（0 号之前部署的库可能缺）
        for code, name in BUILTIN_ROLES:
            row = conn.execute("SELECT role_id FROM ekbs_role WHERE code = ?", (code,)).fetchone()
            if row is None:
                report.roles_created.append(code)
                if not dry_run:
                    conn.execute(
                        "INSERT INTO ekbs_role(code, name, is_builtin, status, created_at) "
                        "VALUES (?, ?, 1, 1, datetime('now'))",
                        (code, name),
                    )

        # ② 补权限点
        existing = {
            str(r["code"]): int(r["perm_id"])
            for r in conn.execute("SELECT perm_id, code FROM ekbs_permission").fetchall()
        }
        for code, name, module in PERMISSIONS:
            if code in existing:
                continue
            report.permissions_added.append(code)
            if not dry_run:
                conn.execute(
                    "INSERT INTO ekbs_permission(code, name, module) VALUES (?, ?, ?)",
                    (code, name, module),
                )

        # ③ 合并历史命名：把授权搬到新代码上，再删旧点
        for legacy, target in LEGACY_PERMISSION_MAP.items():
            if legacy not in existing:
                continue
            if target is None:
                report.permissions_retired.append(legacy)
                if not dry_run:
                    conn.execute("DELETE FROM ekbs_permission WHERE code = ?", (legacy,))
                continue
            report.permissions_renamed[legacy] = target
            if not dry_run:
                conn.execute(
                    "INSERT OR IGNORE INTO ekbs_role_permission(role_id, perm_id) "
                    "SELECT rp.role_id, (SELECT perm_id FROM ekbs_permission WHERE code = ?) "
                    "FROM ekbs_role_permission rp "
                    "JOIN ekbs_permission old ON old.perm_id = rp.perm_id "
                    "WHERE old.code = ? AND (SELECT perm_id FROM ekbs_permission WHERE code = ?) "
                    "IS NOT NULL",
                    (target, legacy, target),
                )
                conn.execute("DELETE FROM ekbs_permission WHERE code = ?", (legacy,))

        if dry_run:
            # 演练：只统计将要新增的授权数，不写库
            for role_code, codes in ROLE_GRANTS.items():
                role_row = conn.execute(
                    "SELECT role_id FROM ekbs_role WHERE code = ?", (role_code,)
                ).fetchone()
                if role_row is None:
                    continue
                for code in codes:
                    perm_row = conn.execute(
                        "SELECT perm_id FROM ekbs_permission WHERE code = ?", (code,)
                    ).fetchone()
                    if perm_row is None:
                        continue
                    already = conn.execute(
                        "SELECT 1 FROM ekbs_role_permission WHERE role_id = ? AND perm_id = ?",
                        (int(role_row["role_id"]), int(perm_row["perm_id"])),
                    ).fetchone()
                    if already is None:
                        report.grants_added += 1
            conn.rollback()
            return report

        # ④ 按 05 §3.4 重放角色授权
        for role_code, codes in ROLE_GRANTS.items():
            role_row = conn.execute(
                "SELECT role_id FROM ekbs_role WHERE code = ?", (role_code,)
            ).fetchone()
            if role_row is None:
                continue
            for code in codes:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO ekbs_role_permission(role_id, perm_id) "
                    "SELECT ?, perm_id FROM ekbs_permission WHERE code = ?",
                    (int(role_row["role_id"]), code),
                )
                report.grants_added += int(cursor.rowcount or 0)
        # super_admin 显式授全量（判定处另有通配兜底，但管理台要能展示完整表）
        conn.execute(
            "INSERT OR IGNORE INTO ekbs_role_permission(role_id, perm_id) "
            "SELECT r.role_id, p.perm_id FROM ekbs_role r, ekbs_permission p "
            "WHERE r.code = 'super_admin'"
        )

        report.users_affected = int(
            conn.execute("SELECT COUNT(*) FROM ekbs_user_role").fetchone()[0]
        )
        conn.execute(
            "INSERT INTO ekbs_perm_change_log(operator_id, object_type, object_id, change_type, "
            "field, old_value, new_value, reason) "
            "VALUES (?, 'rbac', 'permission_catalog', 'acl_grant', 'catalog', ?, ?, ?)",
            (
                operator_id,
                json.dumps(LEGACY_PERMISSION_MAP, ensure_ascii=False),
                json.dumps({"added": report.permissions_added, "grants": report.grants_added},
                           ensure_ascii=False),
                "权限点对齐 05 §8.2（迁移工具）",
            ),
        )
        conn.commit()
    log.info("permission_migration_done", **report.as_dict())
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="权限点与角色授权迁移（幂等）")
    parser.add_argument("--dry-run", action="store_true", help="只报告将发生的变更")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--operator-id", type=int, default=None, help="操作人 user_id（写审计用）")
    args = parser.parse_args(argv)

    settings = load_settings()
    configure_logging(settings, "tools")
    db = Database(settings)
    db.require_ready()
    report = migrate(db, dry_run=args.dry_run, operator_id=args.operator_id)

    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        mode = "演练（未写库）" if report.dry_run else "已执行"
        print(f"权限迁移 {mode}：")
        print(f"  新增权限点 {len(report.permissions_added)}：{report.permissions_added}")
        print(f"  合并历史命名 {len(report.permissions_renamed)}：{report.permissions_renamed}")
        print(f"  废弃权限点 {len(report.permissions_retired)}：{report.permissions_retired}")
        print(f"  新增角色授权 {report.grants_added} 条；新建角色 {report.roles_created}")
        print(f"  受影响的用户-角色关系 {report.users_affected} 条")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
