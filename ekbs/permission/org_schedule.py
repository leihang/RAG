"""管理台保存的钉钉组织定时更新。

间隔存在 ``ekbs_policy_version`` 的附加列上，不新增表。
调度进程按这个间隔调用和「从钉钉更新」相同的全量同步：部门加人员。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

MAX_INTERVAL_HOURS = 720
RUNNING_STALE = timedelta(minutes=30)


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("T", " ")[:19]
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def schedule_due(row: Any, *, now: datetime | None = None) -> bool:
    """到点且没有另一趟还在跑时，才允许再拉一次。"""
    moment = now or datetime.utcnow()
    if not int(row["org_sync_enabled"] or 0):
        return False
    hours = int(row["org_sync_interval_hours"] or 0)
    if hours < 1:
        return False
    running = _parse(row["org_sync_running_at"])
    if running is not None and moment - running < RUNNING_STALE:
        return False
    armed = _parse(row["org_sync_armed_at"])
    if armed is None:
        return False
    return moment >= armed + timedelta(hours=hours)


def _row(conn: Any) -> Any:
    return conn.execute(
        "SELECT org_sync_enabled, org_sync_interval_hours, org_sync_armed_at, "
        "org_sync_running_at, datetime('now') AS now_utc FROM ekbs_policy_version WHERE id = 1"
    ).fetchone()


def read_org_schedule(db: Any) -> dict[str, Any]:
    with db.sync_connect() as conn:
        row = _row(conn)
    if row is None:
        return {
            "enabled": False,
            "interval_hours": 24,
            "armed_at": None,
            "running": False,
            "next_run_at": None,
        }
    now = _parse(row["now_utc"]) or datetime.utcnow()
    armed = _parse(row["org_sync_armed_at"])
    hours = int(row["org_sync_interval_hours"] or 24)
    running_at = _parse(row["org_sync_running_at"])
    enabled = bool(int(row["org_sync_enabled"] or 0))
    next_run = None
    if enabled and armed is not None:
        next_run = (armed + timedelta(hours=max(hours, 1))).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "enabled": enabled,
        "interval_hours": hours,
        "armed_at": row["org_sync_armed_at"],
        "running": running_at is not None and now - running_at < RUNNING_STALE,
        "next_run_at": next_run,
    }


def save_org_schedule(db: Any, *, enabled: bool, interval_hours: int) -> dict[str, Any]:
    hours = int(interval_hours)
    if hours < 1 or hours > MAX_INTERVAL_HOURS:
        raise ValueError(f"间隔须在 1 到 {MAX_INTERVAL_HOURS} 小时之间")
    with db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_policy_version SET org_sync_enabled = ?, org_sync_interval_hours = ?, "
            "org_sync_armed_at = datetime('now'), org_sync_running_at = NULL WHERE id = 1",
            (1 if enabled else 0, hours),
        )
        conn.commit()
    return read_org_schedule(db)


def claim_org_sync(db: Any) -> bool:
    """抢到这一轮才返回真。另一进程正在跑，或还没到间隔，返回假。"""
    with db.sync_connect() as conn:
        row = _row(conn)
        if row is None or not schedule_due(row, now=_parse(row["now_utc"])):
            return False
        cursor = conn.execute(
            "UPDATE ekbs_policy_version SET org_sync_running_at = datetime('now') "
            "WHERE id = 1 AND org_sync_running_at IS ?",
            (row["org_sync_running_at"],),
        )
        conn.commit()
        return int(cursor.rowcount) == 1


def finish_org_sync(db: Any) -> None:
    """无论成败都把下一次计时从现在起算，避免失败后每几分钟打一次钉钉。"""
    with db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_policy_version SET org_sync_running_at = NULL, "
            "org_sync_armed_at = datetime('now') WHERE id = 1"
        )
        conn.commit()


def touch_org_sync_clock(db: Any) -> None:
    """手动更新成功后，定时的下一轮从这次算起。"""
    with db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_policy_version SET org_sync_armed_at = datetime('now') "
            "WHERE id = 1 AND org_sync_enabled = 1"
        )
        conn.commit()
