"""组织与权限同步 —— `04` §4（数据映射 §4.2、闭包与 ``dept_chain`` §4.4、
权限变化一致性 §4.5、失败重试与对账 §4.6）。

为什么这件事在 RAG 里是**安全关键**：分片路由的输入是用户的部门链与密级。
用户离职或调岗后若 ``dept_chain`` 没刷新，他会继续看到原部门的知识 ——
这正是 `04` §4.5 要求「权限回收 ≤5 分钟」的原因。

四件事（都落在本模块）：

1. **全量同步** ``sync_full``：部门树 + 成员 → upsert ``ekbs_dept`` / ``ekbs_user`` /
   ``ekbs_user_dept`` → **重建闭包与 ``dept_chain``** → 全局 ``policy_version`` 自增（全量缓存失效）；
2. **离职同步** ``sync_leave_records``：用官方 ``empLeaveRecords``（`docs/spec/50` §5.5）
   把离职用户置 ``left``，并记录**权限回收滞后**（P1 告警指标）；
3. **对账** ``reconcile``：本地与钉钉的部门/用户数量差异（只报告、不写库）；
4. **审计与可观测**：每次运行写 ``ekbs_sync_log``，并打 ``ORG_SYNC_DURATION`` /
   ``ORG_SYNC_FAILED`` / ``PERM_REVOKE_LAG`` 三个指标（阈值见 `04` §7.7）。

⚠️ **保守原则**：默认**不**因为「这次没拉到某个用户」就把他标成离职 ——
钉钉的可见范围/权限点会让用户**合法地**不出现在列表里。要清理「库里多出来的人」，
显式传 ``mark_absent=True``，或者用官方的离职记录接口（推荐）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from ekbs.cache.store import CacheStore
from ekbs.config import Settings
from ekbs.db import Database
from ekbs.dingtalk.directory import Department, DingTalkDirectoryClient, DirectoryUser
from ekbs.logging import get_logger
from ekbs.observability.metrics import (
    ORG_SYNC_DURATION,
    ORG_SYNC_FAILED,
    PERM_REVOKE_LAG,
    inc,
    observe,
    set_gauge,
)
from ekbs.permission.router import bump_policy_version
from ekbs.tools.sync_org import rebuild_dept_closure, refresh_user_dept_chain

log = get_logger(component="dingtalk.sync")


@dataclass
class SyncReport:
    mode: str
    departments: int = 0
    users: int = 0
    memberships: int = 0
    created: int = 0
    updated: int = 0
    deleted: int = 0
    failed: int = 0
    policy_version: int = 0
    duration_seconds: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "departments": self.departments,
            "users": self.users,
            "memberships": self.memberships,
            "created": self.created,
            "updated": self.updated,
            "deleted": self.deleted,
            "failed": self.failed,
            "policy_version": self.policy_version,
            "duration_seconds": round(self.duration_seconds, 3),
            "detail": self.detail,
        }


class OrgSyncService:
    def __init__(
        self,
        *,
        db: Database,
        settings: Settings,
        directory: DingTalkDirectoryClient,
        cache: CacheStore | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.directory = directory
        self.cache = cache

    # ------------------------------------------------------------------
    # 全量同步
    # ------------------------------------------------------------------

    async def sync_full(self, *, mark_absent: bool = False, operator_id: int | None = None) -> SyncReport:
        started = time.perf_counter()
        report = SyncReport(mode="full")
        try:
            departments = await self.directory.fetch_department_tree()
            report.departments = len(departments)
            created_d, updated_d = self._upsert_departments(departments)

            users: dict[str, DirectoryUser] = {}
            memberships = 0
            for dept in departments:
                for user in await self.directory.list_department_users(dept.dept_id):
                    if not user.userid:
                        continue
                    existing = users.get(user.userid)
                    if existing is None:
                        users[user.userid] = user
                    else:
                        merged = set(existing.dept_ids) | set(user.dept_ids) | {dept.dept_id}
                        existing.dept_ids = sorted(merged)
            report.users = len(users)
            created_u, updated_u, memberships = self._upsert_users(users.values())

            report.created = created_d + created_u
            report.updated = updated_d + updated_u
            report.memberships = memberships
            if mark_absent:
                report.deleted = self._mark_absent(
                    set(users.keys()),
                    scope_dept_ids={str(d.dept_id) for d in departments},
                    operator_id=operator_id,
                )

            with self.db.sync_connect() as conn:
                closures = rebuild_dept_closure(conn)
                refreshed = refresh_user_dept_chain(conn)
                report.policy_version = bump_policy_version(conn, reason="org_sync:full")
                conn.commit()
            report.detail = {"closure_rows": closures, "users_refreshed": refreshed}
            self._invalidate_perm_caches()
            return report
        except Exception as exc:
            report.failed = 1
            inc(ORG_SYNC_FAILED, 1, scope="full")
            log.error("org_sync_failed", mode="full", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            report.duration_seconds = time.perf_counter() - started
            observe(ORG_SYNC_DURATION, report.duration_seconds)
            self._write_sync_log(report)

    # ------------------------------------------------------------------
    # 离职同步
    # ------------------------------------------------------------------

    async def sync_leave_records(self, *, days: int = 7, operator_id: int | None = None) -> SyncReport:
        started = time.perf_counter()
        report = SyncReport(mode="leave")
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=max(1, days))
        try:
            records = await self.directory.list_leave_records(
                start_time=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                end_time=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            report.detail["leave_records"] = len(records)
            for record in records:
                if self._mark_left(record.userid, reason=record.leave_reason, operator_id=operator_id):
                    report.deleted += 1
                    self._record_revoke_lag(record.leave_time)
            if report.deleted:
                with self.db.sync_connect() as conn:
                    report.policy_version = bump_policy_version(conn, reason="org_sync:leave")
                    conn.commit()
                self._invalidate_perm_caches()
            return report
        except Exception as exc:
            report.failed = 1
            inc(ORG_SYNC_FAILED, 1, scope="leave")
            log.error("org_sync_failed", mode="leave", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            report.duration_seconds = time.perf_counter() - started
            observe(ORG_SYNC_DURATION, report.duration_seconds)
            self._write_sync_log(report)

    # ------------------------------------------------------------------
    # 对账（只报告）
    # ------------------------------------------------------------------

    async def reconcile(self) -> dict[str, Any]:
        departments = await self.directory.fetch_department_tree()
        remote_users: set[str] = set()
        for dept in departments:
            remote_users.update(
                user.userid for user in await self.directory.list_department_users(dept.dept_id)
            )
        with self.db.sync_connect() as conn:
            local_depts = int(
                conn.execute(
                    "SELECT COUNT(*) FROM ekbs_dept WHERE status = 'active' AND deleted_at IS NULL"
                ).fetchone()[0]
            )
            local_users = {
                str(r["dingtalk_userid"])
                for r in conn.execute(
                    "SELECT dingtalk_userid FROM ekbs_user WHERE status = 'active' "
                    "AND deleted_at IS NULL"
                ).fetchall()
            }
        report = {
            "remote_departments": len(departments),
            "local_departments": local_depts,
            "remote_users": len(remote_users),
            "local_users": len(local_users),
            # 本地多出来的用户：可能是离职未同步，也可能是钉钉可见范围变了 —— 需人工判断
            "local_only_users": sorted(local_users - remote_users)[:50],
            "missing_locally": len(remote_users - local_users),
        }
        log.info("org_reconcile", **{k: v for k, v in report.items() if k != "local_only_users"})
        return report

    # ------------------------------------------------------------------
    # 落库
    # ------------------------------------------------------------------

    def _upsert_departments(self, departments: Iterable[Department]) -> tuple[int, int]:
        depts = list(departments)
        created = updated = 0
        with self.db.sync_connect() as conn:
            for dept in depts:
                conn.execute(
                    "INSERT INTO ekbs_dept(dingtalk_dept_id, name, parent_dingtalk_dept_id, "
                    "order_no, is_root, status, synced_at) "
                    "VALUES (?, ?, ?, ?, ?, 'active', datetime('now')) "
                    "ON CONFLICT(dingtalk_dept_id) DO UPDATE SET name = excluded.name, "
                    "parent_dingtalk_dept_id = excluded.parent_dingtalk_dept_id, "
                    "order_no = excluded.order_no, status = 'active', synced_at = datetime('now')",
                    (
                        str(dept.dept_id), dept.name,
                        str(dept.parent_id) if dept.parent_id is not None else None,
                        int(dept.order or 0), 1 if dept.parent_id is None else 0,
                    ),
                )
                updated += 1
            # 第二遍解析父子（父可能在后出现）
            for dept in depts:
                if dept.parent_id is None:
                    conn.execute(
                        "UPDATE ekbs_dept SET parent_dept_id = NULL WHERE dingtalk_dept_id = ?",
                        (str(dept.dept_id),),
                    )
                else:
                    conn.execute(
                        "UPDATE ekbs_dept SET parent_dept_id = "
                        "(SELECT dept_id FROM ekbs_dept WHERE dingtalk_dept_id = ?) "
                        "WHERE dingtalk_dept_id = ?",
                        (str(dept.parent_id), str(dept.dept_id)),
                    )
            conn.commit()
        return created, updated

    def _upsert_users(self, users: Iterable[DirectoryUser]) -> tuple[int, int, int]:
        created = updated = memberships = 0
        with self.db.sync_connect() as conn:
            for user in users:
                existing = conn.execute(
                    "SELECT user_id FROM ekbs_user WHERE dingtalk_userid = ?", (user.userid,)
                ).fetchone()
                conn.execute(
                    "INSERT INTO ekbs_user(dingtalk_userid, unionid, nick, job_number, title, "
                    "mobile_masked, email, status, hired_at, synced_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, datetime('now')) "
                    "ON CONFLICT(dingtalk_userid) DO UPDATE SET unionid = excluded.unionid, "
                    "nick = excluded.nick, job_number = excluded.job_number, title = excluded.title, "
                    "mobile_masked = excluded.mobile_masked, email = excluded.email, "
                    # 复职：active 就恢复；离职由离职记录接口负责
                    "status = 'active', deleted_at = NULL, synced_at = datetime('now')",
                    (
                        user.userid, user.unionid or None, user.name or user.userid,
                        user.job_number or None, user.title or None,
                        user.mobile or None, user.email or None, user.hired_date or None,
                    ),
                )
                if existing is None:
                    created += 1
                else:
                    updated += 1
                row = conn.execute(
                    "SELECT user_id FROM ekbs_user WHERE dingtalk_userid = ?", (user.userid,)
                ).fetchone()
                user_id = int(row["user_id"])
                primary = user.dept_ids[0] if user.dept_ids else None
                for dept_ref in user.dept_ids:
                    dept_row = conn.execute(
                        "SELECT dept_id FROM ekbs_dept WHERE dingtalk_dept_id = ?",
                        (str(dept_ref),),
                    ).fetchone()
                    if dept_row is None:
                        continue
                    conn.execute(
                        "INSERT INTO ekbs_user_dept(user_id, dept_id, is_primary) VALUES (?, ?, ?) "
                        "ON CONFLICT(user_id, dept_id) DO UPDATE SET is_primary = excluded.is_primary",
                        (user_id, int(dept_row["dept_id"]), 1 if dept_ref == primary else 0),
                    )
                    memberships += 1
                if primary is not None:
                    conn.execute(
                        "UPDATE ekbs_user SET primary_dept_id = (SELECT dept_id FROM ekbs_dept "
                        "WHERE dingtalk_dept_id = ?) WHERE user_id = ?",
                        (str(primary), user_id),
                    )
            conn.commit()
        return created, updated, memberships

    def _mark_absent(
        self, seen: set[str], *, scope_dept_ids: set[str], operator_id: int | None
    ) -> int:
        """把「**本次同步范围内**却没出现在名册里」的用户置为离职。

        ⚠️ 范围必须限定在本次拉到的部门集合内（这是 `04` §4 的隐含语义）：
        钉钉应用的**可见范围**会让一部分用户根本不出现在接口返回里 ——
        若拿「库里全部 active 用户」去比对，那些范围外的用户会被整批误判离职。
        （实测踩到过：一次 mark_absent 把种子数据里的演示用户全标成了离职。）

        ⚠️⚠️ **开启前必读**：如果同步范围包含**根部门（dept_id=1）**，
        那么本地所有用户都落在范围内，``mark_absent=True`` 就会把「这轮名册里没有的人」
        整批标成离职。仅当你确认应用可见范围 = 全公司时才这么用；
        更稳妥的做法是走官方**离职记录接口**（:meth:`sync_leave_records`）。
        """
        scope = {str(d) for d in scope_dept_ids}
        if not scope:
            return 0
        placeholders = ",".join("?" for _ in scope)
        with self.db.sync_connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT u.dingtalk_userid AS userid FROM ekbs_user u "
                "JOIN ekbs_user_dept ud ON ud.user_id = u.user_id "
                "JOIN ekbs_dept d ON d.dept_id = ud.dept_id "
                "WHERE u.status = 'active' AND u.deleted_at IS NULL "
                f"AND d.dingtalk_dept_id IN ({placeholders})",
                tuple(sorted(scope)),
            ).fetchall()
        absent = [str(r["userid"]) for r in rows if str(r["userid"]) not in seen]
        marked = 0
        for userid in absent:
            if self._mark_left(userid, reason="absent_in_full_sync", operator_id=operator_id):
                marked += 1
        if absent:
            log.warning("org_sync_mark_absent", count=len(absent), sample=absent[:5])
        return marked

    def _mark_left(self, userid: str, *, reason: str, operator_id: int | None) -> bool:
        """置为离职 + 用户级版本自增 + 写权限变更审计（04 §4.5）。"""
        if not userid:
            return False
        with self.db.sync_connect() as conn:
            row = conn.execute(
                "SELECT user_id, status FROM ekbs_user WHERE dingtalk_userid = ?", (userid,)
            ).fetchone()
            if row is None or str(row["status"]) != "active":
                return False
            user_id = int(row["user_id"])
            conn.execute(
                "UPDATE ekbs_user SET status = 'left', left_at = datetime('now'), "
                "perm_version = perm_version + 1, synced_at = datetime('now') WHERE user_id = ?",
                (user_id,),
            )
            conn.execute(
                "INSERT INTO ekbs_perm_change_log(operator_id, object_type, object_id, "
                "change_type, field, old_value, new_value, reason) "
                "VALUES (?, 'user', ?, 'leave', 'status', 'active', 'left', ?)",
                (operator_id, str(user_id), reason or "left"),
            )
            conn.commit()
        if self.cache is not None:
            # 该用户的权限上下文缓存必须立刻失效（不能等 300s TTL）
            self.cache.invalidate(f"ekbs:perm:user:{user_id}")
        log.warning("org_user_marked_left", userid=userid, reason=reason)
        return True

    def _record_revoke_lag(self, leave_time: str) -> None:
        """记录权限回收滞后（P1 告警：> 300s，`04` §7.7）。"""
        if not leave_time:
            return
        try:
            moment = datetime.fromisoformat(str(leave_time).replace("Z", "+00:00"))
        except ValueError:
            return
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        lag = max(0.0, (datetime.now(timezone.utc) - moment).total_seconds())
        set_gauge(PERM_REVOKE_LAG, lag)
        if lag > 300:
            log.error("perm_revoke_lag_exceeded", lag_seconds=round(lag), leave_time=leave_time)

    def _invalidate_perm_caches(self) -> None:
        """组织变更后清权限缓存（正确性靠 policy_version，这里只是回收空间）。"""
        if self.cache is None:
            return
        self.cache.invalidate("ekbs:perm:")
        with self.db.sync_connect() as conn:
            conn.execute("DELETE FROM ekbs_cache WHERE cache_key LIKE 'ekbs:perm:%'")
            conn.commit()

    def _write_sync_log(self, report: SyncReport) -> None:
        with self.db.sync_connect() as conn:
            conn.execute(
                "INSERT INTO ekbs_sync_log(scope, mode, ended_at, created, updated, deleted, "
                "failed, error, detail) VALUES (?, ?, datetime('now'), ?, ?, ?, ?, ?, ?)",
                (
                    "dept+user", report.mode, report.created, report.updated, report.deleted,
                    report.failed, None, json.dumps(report.as_dict(), ensure_ascii=False),
                ),
            )
            conn.commit()


def build_sync_service(
    db: Database,
    settings: Settings,
    secrets: Any,
    *,
    cache: CacheStore | None = None,
) -> OrgSyncService:
    """按配置装配同步服务（凭据缺失时抛 ConfigError，由调用方决定怎么提示）。"""
    from ekbs.dingtalk.token import DingTalkTokenClient

    if not (
        secrets.is_configured("DINGTALK_CLIENT_ID")
        and secrets.is_configured("DINGTALK_CLIENT_SECRET")
    ):
        from ekbs.errors import ConfigError

        raise ConfigError(
            "组织同步需要钉钉凭据（DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET，04 §2.6）；"
            "无凭据时可用 ekbs.tools.sync_org --fixture 导入 JSON"
        )
    openapi = (settings.dingtalk or {}).get("openapi") or {}
    token_client = DingTalkTokenClient.from_settings(settings, secrets, cache=cache)
    directory = DingTalkDirectoryClient(
        token_client=token_client,
        settings=settings,
        legacy_base_url=openapi.get("legacy_base_url"),
        base_url=openapi.get("base_url"),
    )
    return OrgSyncService(db=db, settings=settings, directory=directory, cache=cache)
