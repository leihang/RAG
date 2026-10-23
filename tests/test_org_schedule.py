"""定时更新：没到间隔不跑；保存后从现在起算。"""

from datetime import datetime

from ekbs.permission.org_schedule import claim_org_sync, save_org_schedule, schedule_due


def test_schedule_due_respects_interval_and_switch() -> None:
    now = datetime(2026, 10, 24, 12, 0, 0)
    row = {
        "org_sync_enabled": 1,
        "org_sync_interval_hours": 6,
        "org_sync_armed_at": "2026-10-24 10:00:00",
        "org_sync_running_at": None,
    }
    assert schedule_due(row, now=now) is False
    assert schedule_due(row, now=datetime(2026, 10, 24, 16, 0, 0)) is True
    row["org_sync_enabled"] = 0
    assert schedule_due(row, now=datetime(2026, 10, 24, 16, 0, 0)) is False


def test_fresh_schedule_is_not_due_immediately(seeded) -> None:
    runtime, _ = seeded
    saved = save_org_schedule(runtime.db, enabled=True, interval_hours=6)
    assert saved["enabled"] is True
    assert saved["interval_hours"] == 6
    assert saved["next_run_at"]
    assert claim_org_sync(runtime.db) is False
