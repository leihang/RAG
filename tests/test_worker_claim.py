"""worker 认领竞态与进程单例锁。

真实故障：常驻 worker 与 ``--once`` 同时 SELECT 到同一 pending，无条件 UPDATE 后
两个进程各自拉起 MinerU，24 GB 机器内存耗尽。另见 ``requeue_orphans`` 会把对方
正在 parsing 的文档打回 pending——单例锁必须在 requeue 之前。

双 worker 温床：停不干净仍起新进程，或删掉 ``worker.lock`` 导致 flock inode 分裂。
进程扫描是 flock 之外的第二道闸。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from ekbs.worker import (
    WorkerProcessScanError,
    _acquire_worker_lock,
    _claim_next_pending,
    _lock_holder_pid,
    _peer_worker_pids,
    _reject_if_peer_workers,
)


def _force_pending(runtime, doc_id: int) -> None:
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_document SET status = 'pending' WHERE doc_id = ?", (doc_id,)
        )


def _force_published(runtime, doc_id: int) -> None:
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_document SET status = 'published' WHERE doc_id = ?", (doc_id,)
        )


def _status(runtime, doc_id: int) -> str:
    with runtime.db.sync_connect() as conn:
        return str(
            conn.execute(
                "SELECT status FROM ekbs_document WHERE doc_id = ?", (doc_id,)
            ).fetchone()["status"]
        )


def test_concurrent_claim_only_one_wins(runtime, seeded) -> None:
    """两个并发认领同一条 pending：恰好一个成功，库状态为 parsing。"""
    runtime, summary = seeded
    doc_id = int(summary["documents"][0]["doc_id"])
    _force_pending(runtime, doc_id)
    for d in summary["documents"][1:]:
        _force_published(runtime, int(d["doc_id"]))

    barrier = threading.Barrier(2)

    def _claim() -> dict | None:
        barrier.wait(timeout=5)
        return _claim_next_pending(runtime)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(_claim), pool.submit(_claim)]
        results = [f.result(timeout=10) for f in futs]

    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert int(winners[0]["doc_id"]) == doc_id
    assert _status(runtime, doc_id) == "parsing"


def test_claim_returns_none_when_empty(runtime, seeded) -> None:
    runtime, summary = seeded
    for d in summary["documents"]:
        _force_published(runtime, int(d["doc_id"]))
    assert _claim_next_pending(runtime) is None


def test_worker_lock_second_acquire_fails(tmp_path) -> None:
    lock_path = tmp_path / "worker.lock"
    first = _acquire_worker_lock(lock_path)
    assert first is not None
    try:
        second = _acquire_worker_lock(lock_path)
        assert second is None
        assert _lock_holder_pid(lock_path) is not None
    finally:
        first.close()


def test_reject_peers_after_lock_unlink_split(tmp_path, monkeypatch) -> None:
    """删 lock 文件会让 flock 锁到新 inode；peer 扫描必须拒绝第二进程。"""
    lock_path = tmp_path / "worker.lock"
    first = _acquire_worker_lock(lock_path)
    assert first is not None
    try:
        lock_path.unlink()
        second = _acquire_worker_lock(lock_path)
        assert second is not None  # flock 本身已被 inode 分裂骗过
        second.close()
        monkeypatch.setattr(
            "ekbs.worker._peer_worker_pids", lambda **_kwargs: [12345]
        )
        assert _reject_if_peer_workers(lock_path=lock_path, settle_seconds=0) == [12345]
    finally:
        first.close()


def test_peer_worker_pids_matches_module_entry_only(monkeypatch) -> None:
    fake_ps = (
        "  111 /usr/bin/Python -m ekbs.worker\n"
        "  222 /usr/bin/Python -m pytest tests/test_worker_claim.py\n"
        "  333 /usr/bin/Python -c from ekbs.worker import main\n"
        "  444 /usr/bin/Python -m ekbs.scheduler\n"
    )
    monkeypatch.setattr(
        "ekbs.worker.subprocess.check_output",
        lambda *_a, **_k: fake_ps,
    )
    assert _peer_worker_pids(exclude_pid=0) == [111]
    assert _peer_worker_pids(exclude_pid=111) == []


def test_peer_worker_pids_fails_closed_when_ps_broken(monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise OSError("operation not permitted")

    monkeypatch.setattr("ekbs.worker.subprocess.check_output", _boom)
    with pytest.raises(WorkerProcessScanError):
        _peer_worker_pids()


def test_claim_pending_index_marks_indexing(runtime, seeded) -> None:
    """方案 B：approve 入队的 pending_index 文档，认领后置 indexing（跳过解析）。"""
    runtime, summary = seeded
    doc_id = int(summary["documents"][0]["doc_id"])
    for d in summary["documents"][1:]:
        _force_published(runtime, int(d["doc_id"]))
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_document SET status = 'pending_index' WHERE doc_id = ?", (doc_id,)
        )
    claimed = _claim_next_pending(runtime)
    assert claimed is not None
    assert int(claimed["doc_id"]) == doc_id
    assert claimed["_claimed_status"] == "indexing"
    assert _status(runtime, doc_id) == "indexing"
