"""消息幂等守卫 —— `04` §5.4（表 ``ekbs_msg_dedup``，替代 v2.0 的 Redis SET NX）。

语义：「**首次受理放行，重复投递拒绝**」。

* 判据：``INSERT ... ON CONFLICT(msg_id) DO NOTHING`` 的 ``rowcount`` ——
  单语句原子，不需要额外加锁，也不需要「处理中/已完成」三态；
* 处理失败时**删除**去重行，让钉钉重投能被重新受理（否则一条失败消息会被永久吞掉）；
* ``msg_id`` 缺失时**保守放行并告警** —— 宁可重复回答，也不能静默丢弃用户消息。

**为什么不用 ``ekbs_cache``**（文档里的论证，值得照抄）：
缓存的语义是「可以随时被清空而不影响正确性」，但「这条消息已受理」是**业务事实**——
被误清会直接导致同一个问题被回答两次，并产生两条审计记录。

清理由 ``com.ekbs.scheduler`` 的 ``msg_dedup_cleanup`` 作业按 ``created_at`` 删除
（`01` §5.5；本实现保留 7 天，见 ``ekbs.scheduler.MSG_DEDUP_RETAIN_DAYS``）。
"""

from __future__ import annotations

import asyncio

from ekbs.db import Database
from ekbs.logging import get_logger

log = get_logger(component="idempotency")


class IdempotencyGuard:
    def __init__(self, db: Database, *, ttl_seconds: int = 3600) -> None:
        self.db = db
        self.ttl = int(ttl_seconds)

    # ------------------------------------------------------------------
    # 同步核心
    # ------------------------------------------------------------------

    def try_acquire(self, msg_id: str | None) -> bool:
        """返回 True 表示**首次受理**。"""
        if not msg_id:
            # 钉钉理论上总会带 msgId；缺失时宁可重复回答，也不能丢消息
            log.warning("message_without_msg_id_idempotency_skipped")
            return True
        with self.db.sync_connect() as conn:
            cursor = conn.execute(
                "INSERT INTO ekbs_msg_dedup(msg_id, created_at) VALUES (?, datetime('now')) "
                "ON CONFLICT(msg_id) DO NOTHING",
                (str(msg_id),),
            )
            conn.commit()
            first_time = int(cursor.rowcount or 0) == 1
        if not first_time:
            from ekbs.observability.metrics import IDEMPOTENT_HIT, inc

            inc(IDEMPOTENT_HIT, 1)
        return first_time

    def release(self, msg_id: str | None) -> int:
        """处理失败时释放去重行，使重投可被重新受理。返回删除行数。"""
        if not msg_id:
            return 0
        with self.db.sync_connect() as conn:
            cursor = conn.execute("DELETE FROM ekbs_msg_dedup WHERE msg_id = ?", (str(msg_id),))
            conn.commit()
            return int(cursor.rowcount or 0)

    def is_processed(self, msg_id: str) -> bool:
        with self.db.sync_connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM ekbs_msg_dedup WHERE msg_id = ?", (str(msg_id),)
            ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # 异步门面（适配器进程用）
    # ------------------------------------------------------------------

    async def acquire(self, msg_id: str | None) -> bool:
        return await asyncio.to_thread(self.try_acquire, msg_id)

    async def mark_done(self, msg_id: str | None) -> None:
        """无需写「已完成」状态。

        去重粒度是「是否已受理」，不是「处理结果」：用户重发同一条消息会拿到**新的 msgId**，
        真正的处理结果落在 ``ekbs_qa_message``。保留该方法只是给调用方一个稳定接口位，
        避免调用点写 ``if`` 分支（`04` §5.4 原话）。
        """
        return None

    async def mark_failed(self, msg_id: str | None) -> int:
        """处理失败 → 删除去重行（否则钉钉重投会被当成重复而丢弃）。"""
        removed = await asyncio.to_thread(self.release, msg_id)
        if removed:
            log.warning("idempotency_released", msg_id=str(msg_id))
        return removed
