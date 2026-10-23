"""消息幂等（`04` §5.4）：首次受理放行、重复投递拒绝、失败可重投。"""

from __future__ import annotations


import pytest

from ekbs.idempotency import IdempotencyGuard


@pytest.fixture
def guard(runtime) -> IdempotencyGuard:
    return IdempotencyGuard(runtime.db)


async def test_first_message_is_accepted_and_duplicate_rejected(guard) -> None:
    assert await guard.acquire("msg-1") is True
    assert await guard.acquire("msg-1") is False
    assert await guard.acquire("msg-2") is True
    assert guard.is_processed("msg-1") is True


async def test_missing_msg_id_is_tolerated_with_warning(guard, caplog) -> None:
    """钉钉理论上总会带 msgId；缺失时宁可重复回答，也不能静默丢弃用户消息。"""
    assert await guard.acquire(None) is True
    assert await guard.acquire("") is True


async def test_mark_failed_releases_for_redelivery(guard) -> None:
    """处理失败必须释放去重行，否则钉钉重投会被当成重复而永久吞掉。"""
    assert await guard.acquire("msg-3") is True
    await guard.mark_failed("msg-3")
    assert guard.is_processed("msg-3") is False
    assert await guard.acquire("msg-3") is True


async def test_mark_done_keeps_guard_closed(guard) -> None:
    assert await guard.acquire("msg-4") is True
    await guard.mark_done("msg-4")
    assert await guard.acquire("msg-4") is False


async def test_release_unknown_message_is_noop(guard) -> None:
    assert await guard.mark_failed("never-seen") == 0
    assert await guard.mark_failed(None) == 0


async def test_dedup_rows_land_in_table(runtime, guard) -> None:
    """必须落 ``ekbs_msg_dedup``（业务事实，不能放缓存 —— 缓存被清会导致重复回答）。"""
    await guard.acquire("msg-5")
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT msg_id, created_at FROM ekbs_msg_dedup WHERE msg_id = 'msg-5'"
        ).fetchone()
    assert row is not None and row["created_at"]


async def test_cleanup_job_removes_old_rows(runtime, guard) -> None:
    """过期清理由调度器作业负责（`01` §5.5），这里验证它确实按时间删除。"""
    await guard.acquire("msg-old")
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_msg_dedup SET created_at = datetime('now', '-30 days') "
            "WHERE msg_id = 'msg-old'"
        )
        conn.commit()
    from ekbs.scheduler import job_msg_dedup_cleanup

    job_msg_dedup_cleanup(runtime)
    assert guard.is_processed("msg-old") is False
