"""``python -m ekbs.tools.sync_org`` —— 组织与用户同步、派生字段重建（`04` §4）。

三种模式与 `04` 的定时作业口径一致：

* ``--mode full``：全量同步（**需要钉钉企业应用凭据**，当前未接入 → 明确报错，不假装成功）；
* ``--mode delta``：增量（同上）；
* ``--mode reconcile``：对账（本地与钉钉比对，同上）。

在没有凭据的阶段，可以用 ``--fixture`` 导入一份 JSON 做本地联调与测试：

.. code-block:: json

    {
      "depts": [{"dingtalk_dept_id": "1", "name": "总部", "parent_dingtalk_dept_id": null}],
      "users": [{"dingtalk_userid": "u1", "nick": "张三", "clearance": 2, "dept_ids": ["102"]}]
    }

无论哪种模式，导入后都必须重建两个派生结构（`03` §2.5）：

1. ``ekbs_dept_closure``：部门 → 祖先闭包（含自身 ``depth=0``）；
2. ``ekbs_user.dept_chain``：本部门 + 全部祖先部门的 ``dingtalk_dept_id`` JSON 数组。

⚠️ 方向性：闭包只向上（祖先），**绝不按子部门向下展开** —— 文档授权给部门 D，
则 D 与其所有祖先部门的成员可见（`03` §2.5）。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from ekbs.config import load_settings
from ekbs.db import Database
from ekbs.logging import configure_logging, get_logger
from ekbs.permission.router import bump_policy_version

log = get_logger(component="tools.sync_org")


def rebuild_dept_closure(conn: sqlite3.Connection) -> int:
    """重建部门闭包（`03` §2.5 的 ``WITH RECURSIVE up``）。"""
    conn.execute("DELETE FROM ekbs_dept_closure")
    conn.execute(
        """
        WITH RECURSIVE up(dept_id, ancestor_id, depth) AS (
            SELECT dept_id, dept_id, 0 FROM ekbs_dept WHERE deleted_at IS NULL
            UNION ALL
            SELECT up.dept_id, d.parent_dept_id, up.depth + 1
              FROM up JOIN ekbs_dept d ON d.dept_id = up.ancestor_id
             WHERE d.parent_dept_id IS NOT NULL
        )
        INSERT INTO ekbs_dept_closure(dept_id, ancestor_id, depth)
        SELECT dept_id, ancestor_id, depth FROM up
        """
    )
    row = conn.execute("SELECT COUNT(*) AS c FROM ekbs_dept_closure").fetchone()
    return int(row["c"])


def refresh_user_dept_chain(conn: sqlite3.Connection) -> int:
    """把「本部门 + 全部祖先部门」物化到 ``ekbs_user.dept_chain``（TEXT JSON 数组）。"""
    conn.execute(
        """
        UPDATE ekbs_user
           SET dept_chain = COALESCE((
                SELECT json_group_array(x.did) FROM (
                    SELECT DISTINCT d.dingtalk_dept_id AS did
                      FROM ekbs_user_dept ud
                      JOIN ekbs_dept_closure dc ON dc.dept_id = ud.dept_id
                      JOIN ekbs_dept d ON d.dept_id = dc.ancestor_id
                     WHERE ud.user_id = ekbs_user.user_id AND d.deleted_at IS NULL
                ) x
           ), '[]')
         WHERE status = 'active' AND deleted_at IS NULL
        """
    )
    row = conn.execute("SELECT COUNT(*) AS c FROM ekbs_user WHERE status = 'active'").fetchone()
    return int(row["c"])


def import_fixture(db: Database, payload: dict[str, Any]) -> dict[str, int]:
    """导入部门/用户 JSON（幂等：按钉钉 ID upsert）。"""
    depts = payload.get("depts", [])
    users = payload.get("users", [])
    created = {"depts": 0, "users": 0, "memberships": 0}

    with db.sync_connect() as conn:
        for dept in depts:
            conn.execute(
                "INSERT INTO ekbs_dept(dingtalk_dept_id, name, parent_dingtalk_dept_id, "
                "dept_path, depth, order_no, is_root, status, synced_at) "
                "VALUES (?, ?, ?, '/', 0, ?, ?, 'active', datetime('now')) "
                "ON CONFLICT(dingtalk_dept_id) DO UPDATE SET name = excluded.name, "
                "parent_dingtalk_dept_id = excluded.parent_dingtalk_dept_id, "
                "synced_at = datetime('now')",
                (
                    str(dept["dingtalk_dept_id"]), str(dept.get("name", "")),
                    dept.get("parent_dingtalk_dept_id"),
                    int(dept.get("order_no", 0)),
                    1 if dept.get("parent_dingtalk_dept_id") in (None, "") else 0,
                ),
            )
            created["depts"] += 1
        # 第二遍解析父子关系（父可能在后出现）
        for dept in depts:
            parent = dept.get("parent_dingtalk_dept_id")
            if parent in (None, ""):
                continue
            conn.execute(
                "UPDATE ekbs_dept SET parent_dept_id = (SELECT dept_id FROM ekbs_dept "
                "WHERE dingtalk_dept_id = ?), depth = 1 WHERE dingtalk_dept_id = ?",
                (str(parent), str(dept["dingtalk_dept_id"])),
            )

        for user in users:
            conn.execute(
                "INSERT INTO ekbs_user(dingtalk_userid, nick, clearance, title, "
                "mobile_masked, email, status, synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active', datetime('now')) "
                "ON CONFLICT(dingtalk_userid) DO UPDATE SET nick = excluded.nick, "
                "clearance = excluded.clearance, title = excluded.title, "
                "mobile_masked = excluded.mobile_masked, email = excluded.email, "
                "synced_at = datetime('now')",
                (
                    str(user["dingtalk_userid"]), str(user.get("nick", "")),
                    int(user.get("clearance", 2)), user.get("title"),
                    user.get("mobile_masked"), user.get("email"),
                ),
            )
            created["users"] += 1
            row = conn.execute(
                "SELECT user_id FROM ekbs_user WHERE dingtalk_userid = ?",
                (str(user["dingtalk_userid"]),),
            ).fetchone()
            user_id = int(row["user_id"])
            primary = user.get("primary_dept_id")
            for dept_ref in user.get("dept_ids", []):
                dept_row = conn.execute(
                    "SELECT dept_id FROM ekbs_dept WHERE dingtalk_dept_id = ?",
                    (str(dept_ref),),
                ).fetchone()
                if dept_row is None:
                    continue
                conn.execute(
                    "INSERT INTO ekbs_user_dept(user_id, dept_id, is_primary) VALUES (?, ?, ?) "
                    "ON CONFLICT(user_id, dept_id) DO UPDATE SET is_primary = excluded.is_primary",
                    (user_id, int(dept_row["dept_id"]), 1 if str(dept_ref) == str(primary) else 0),
                )
                created["memberships"] += 1
            if primary is not None:
                conn.execute(
                    "UPDATE ekbs_user SET primary_dept_id = (SELECT dept_id FROM ekbs_dept "
                    "WHERE dingtalk_dept_id = ?) WHERE user_id = ?",
                    (str(primary), user_id),
                )

        closures = rebuild_dept_closure(conn)
        users_refreshed = refresh_user_dept_chain(conn)
        bump_policy_version(conn, reason="sync_org:fixture")
        conn.execute(
            "INSERT INTO ekbs_sync_log(scope, mode, ended_at, created, updated, detail) "
            "VALUES ('dept+user', 'fixture', datetime('now'), ?, 0, ?)",
            (
                created["depts"] + created["users"],
                json.dumps({"closure_rows": closures, "users_refreshed": users_refreshed}),
            ),
        )
        conn.commit()
    log.info("fixture_imported", **created, closure_rows=closures)
    return {**created, "closure_rows": closures, "users_refreshed": users_refreshed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="组织与用户同步（钉钉 / JSON fixture）")
    parser.add_argument("--mode", default="full", choices=["full", "delta", "reconcile"])
    parser.add_argument("--fixture", help="JSON 文件路径（无钉钉凭据时的导入路径）")
    parser.add_argument(
        "--rebuild-derived", action="store_true", help="仅重建闭包与 dept_chain"
    )
    parser.add_argument(
        "--mark-absent", action="store_true",
        help="把本轮未拉到的用户也置为离职（默认关闭：钉钉可见范围会让用户合法地不出现）",
    )
    parser.add_argument(
        "--leave-days", type=int, default=7, help="delta 模式回看多少天的离职记录"
    )
    args = parser.parse_args(argv)

    settings = load_settings()
    configure_logging(settings, "tools")
    db = Database(settings)
    db.require_ready()

    if args.fixture:
        payload = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
        print(import_fixture(db, payload))
        return 0

    if args.rebuild_derived:
        with db.sync_connect() as conn:
            closures = rebuild_dept_closure(conn)
            users = refresh_user_dept_chain(conn)
            bump_policy_version(conn, reason="sync_org:rebuild_derived")
            conn.commit()
        print({"closure_rows": closures, "users_refreshed": users})
        return 0

    # 走真实钉钉同步（04 §4）；无凭据时给出可用的替代路径
    import asyncio

    from ekbs.cache.store import CacheStore
    from ekbs.config import Secrets
    from ekbs.dingtalk.sync import build_sync_service
    from ekbs.errors import ConfigError

    cache = CacheStore(db, settings)
    try:
        service = build_sync_service(db, settings, Secrets(), cache=cache)
    except ConfigError as exc:
        print(f"[需要凭据] {exc}", file=sys.stderr)
        print(
            "  当前可用的替代路径：\n"
            "    python -m ekbs.tools.sync_org --fixture fixtures/org.json    # 导入 JSON\n"
            "    python -m ekbs.tools.sync_org --rebuild-derived              # 仅重建派生字段",
            file=sys.stderr,
        )
        return 2

    if args.mode == "reconcile":
        print(json.dumps(asyncio.run(service.reconcile()), ensure_ascii=False, indent=2))
        return 0
    if args.mode == "delta":
        report = asyncio.run(service.sync_leave_records(days=args.leave_days))
    else:
        report = asyncio.run(service.sync_full(mark_absent=args.mark_absent))
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
