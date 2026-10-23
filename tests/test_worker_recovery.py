"""worker 启动时回收"卡死的解析中"文档。

真实故障（用户实测）：出一份含 92 张图的 PPT，解析很慢，运维重启 worker →
那份文档**永远卡在 `parsing`**：取消接口只接受 `pending`，重排接口拒绝 `parsing`，
界面一直"解析中"，日志里既没有 `doc_parsed` 也没有失败原因。

判定为什么精确：本进程刚起来，不可能有自己在跑的解析，所以此刻处于
`parsing`/`indexing` 的行一定是上一个进程留下的。
"""

from __future__ import annotations


from ekbs.worker import requeue_orphans


def _set_status(runtime, doc_id: int, status: str) -> None:
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_document SET status = ? WHERE doc_id = ?", (status, doc_id)
        )
        conn.commit()


def _status(runtime, doc_id: int) -> str:
    with runtime.db.sync_connect() as conn:
        return str(
            conn.execute(
                "SELECT status FROM ekbs_document WHERE doc_id = ?", (doc_id,)
            ).fetchone()["status"]
        )


def test_requeue_orphans_recovers_stuck_documents(runtime, seeded) -> None:
    runtime, summary = seeded
    doc_ids = [int(d["doc_id"]) for d in summary["documents"]]
    _set_status(runtime, doc_ids[0], "parsing")
    _set_status(runtime, doc_ids[1], "indexing")

    assert requeue_orphans(runtime) == 2
    assert _status(runtime, doc_ids[0]) == "pending"
    assert _status(runtime, doc_ids[1]) == "pending"


def test_requeue_orphans_leaves_healthy_states_alone(runtime, seeded) -> None:
    runtime, summary = seeded
    doc_ids = [int(d["doc_id"]) for d in summary["documents"]]
    _set_status(runtime, doc_ids[0], "published")
    _set_status(runtime, doc_ids[1], "failed")
    _set_status(runtime, doc_ids[2], "offline")

    assert requeue_orphans(runtime) == 0
    assert _status(runtime, doc_ids[0]) == "published"
    assert _status(runtime, doc_ids[1]) == "failed"
    assert _status(runtime, doc_ids[2]) == "offline"
