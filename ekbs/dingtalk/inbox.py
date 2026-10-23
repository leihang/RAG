"""D16 消息落库 —— 把钉钉消息链路从 at-most-once 升级为 at-least-once（`00` v4.0 D16/C6、`08` §3）。

为什么 ``ekbs_msg_dedup``（``IdempotencyGuard``）不够：它只记录「受理过」这一个事实。
适配器在 **acquire 之后、回答之前** 崩溃时，去重行残留 —— 钉钉重投被判「重复」直接吞掉，
用户永远等不到回答（这正是 v3 评估的 P0-4：消息 at-most-once）。

本模块用 ``ekbs_msg_inbox`` 把「受理」升级为**持久化状态机**：

* ``received``   已落库、未开始处理（ACK 之前必须到达此状态，C6）；
* ``processing`` 正在处理；``updated_at`` 超过租约秒数未刷新 → 视为崩溃，可被回收重驱动；
* ``done``       已答复（含拒答/拦截/限流等一切用户已收到反馈的终态）；
* ``failed``     可重试失败，按 ``next_retry_at`` 指数退避；重试耗尽转 ``ekbs_msg_deadletter``。

崩溃恢复由 ``pending_recovery()`` + 适配器启动/周期扫描完成（`08` §3.1 的 ③④）。
清理（done 行保留 7 天）归 ``com.ekbs.scheduler`` 的既有清理作业族。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from ekbs.db import Database
from ekbs.logging import get_logger

log = get_logger(component="dingtalk.inbox")

#: 处理租约：超过该秒数仍在 processing 的行视为崩溃残留，允许回收重驱动。
#: 必须大于最坏情况的一次问答耗时（adapter.qa_stream_timeout_seconds 默认 180s）。
LEASE_SECONDS = 600
#: 自动重试上限（不含首次）；耗尽转死信
MAX_RETRIES = 5
#: 指数退避基数（秒）：30 → 60 → 120 → 240 → 480
BACKOFF_BASE_SECONDS = 30

AcceptVerdict = Literal["new", "duplicate", "reclaimed"]


@dataclass(frozen=True)
class InboxRow:
    msg_id: str
    channel: str
    sender_staff_id: str
    conversation_id: str
    conversation_type: str
    text: str
    status: str
    retry_count: int

    def to_callback_payload(self) -> dict[str, Any]:
        """还原成 ``StreamMessageHandler.handle()`` 能消费的回调字典（恢复重驱动用）。"""
        return {
            "msgId": self.msg_id,
            "senderStaffId": self.sender_staff_id,
            "conversationId": self.conversation_id,
            "conversationType": self.conversation_type,
            "msgtype": "text",
            "text": {"content": self.text},
        }


class MessageInbox:
    """``ekbs_msg_inbox`` 的状态机封装（全部同步方法；适配器进程内经 ``asyncio.to_thread`` 调用）。"""

    def __init__(self, db: Database, *, lease_seconds: int = LEASE_SECONDS,
                 max_retries: int = MAX_RETRIES) -> None:
        self.db = db
        self.lease_seconds = int(lease_seconds)
        self.max_retries = int(max_retries)

    # ------------------------------------------------------------------
    # 受理（ACK 前的持久化，C6）
    # ------------------------------------------------------------------

    def accept(
        self,
        *,
        msg_id: str,
        channel: str = "dingtalk_robot",
        sender_staff_id: str = "",
        conversation_id: str = "",
        conversation_type: str = "1",
        text: str = "",
    ) -> AcceptVerdict:
        """落库并判定受理结果。

        * ``new``       —— 首次受理；
        * ``reclaimed`` —— 上一次处理崩溃/失败到期，本次**重新受理**（at-least-once 的关键）；
        * ``duplicate`` —— 已完成、或另一处理者正在租约内处理、或死信已终态 —— 不得重复回答。
        """
        with self.db.sync_connect() as conn:
            cursor = conn.execute(
                "INSERT INTO ekbs_msg_inbox(msg_id, channel, sender_staff_id, conversation_id,"
                " conversation_type, text) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(msg_id) DO NOTHING",
                (msg_id, channel, sender_staff_id, conversation_id, conversation_type, text),
            )
            conn.commit()
            if int(cursor.rowcount or 0) == 1:
                return "new"

            row = conn.execute(
                "SELECT status, retry_count, next_retry_at,"
                "       (updated_at < datetime('now', '-60 seconds')) AS stale_received,"
                "       (updated_at < datetime('now', ?)) AS stale_processing "
                "FROM ekbs_msg_inbox WHERE msg_id = ?",
                (f"-{self.lease_seconds} seconds", msg_id),
            ).fetchone()
            if row is None:  # 极端竞态：刚插入即被清理 —— 保守按重复处理
                return "duplicate"
            status = str(row["status"])
            if status == "done":
                return "duplicate"
            if status == "failed":
                # next_retry_at 为空 = 重试已耗尽（已入死信表）→ 终态，不再受理
                if row["next_retry_at"] is None:
                    return "duplicate"
                due = conn.execute(
                    "SELECT ? <= datetime('now')", (str(row["next_retry_at"]),)
                ).fetchone()[0]
                if not due:
                    return "duplicate"  # 退避中，等下次重投/恢复扫描
                return self._reclaim(conn, msg_id)
            # received / processing：超过对应租约才允许接管，否则说明另一处理者在跑
            stale = row["stale_received"] if status == "received" else row["stale_processing"]
            if not int(stale):
                return "duplicate"
            return self._reclaim(conn, msg_id)

    def _reclaim(self, conn: Any, msg_id: str) -> AcceptVerdict:
        """接管一条中断的消息。接管次数计入重试预算并设上限 —— 否则一条
        「必然让处理崩溃」的毒消息会造成「崩溃 → 接管 → 再崩溃」的无限循环。"""
        row = conn.execute(
            "SELECT retry_count FROM ekbs_msg_inbox WHERE msg_id = ?", (msg_id,)
        ).fetchone()
        retry_count = (int(row["retry_count"]) if row else 0) + 1
        if retry_count > self.max_retries:
            self._to_deadletter(conn, msg_id, "reclaim_limit_exceeded", retry_count)
            conn.commit()
            return "duplicate"
        conn.execute(
            "UPDATE ekbs_msg_inbox SET status = 'received', retry_count = ?,"
            " updated_at = datetime('now') WHERE msg_id = ?",
            (retry_count, msg_id),
        )
        conn.commit()
        log.warning("msg_inbox_reclaimed", msg_id=msg_id, retry_count=retry_count)
        return "reclaimed"

    # ------------------------------------------------------------------
    # 状态流转
    # ------------------------------------------------------------------

    def mark_processing(self, msg_id: str) -> None:
        with self.db.sync_connect() as conn:
            conn.execute(
                "UPDATE ekbs_msg_inbox SET status = 'processing', updated_at = datetime('now')"
                " WHERE msg_id = ?",
                (msg_id,),
            )
            conn.commit()

    def mark_done(self, msg_id: str) -> None:
        with self.db.sync_connect() as conn:
            conn.execute(
                "UPDATE ekbs_msg_inbox SET status = 'done', next_retry_at = NULL,"
                " updated_at = datetime('now') WHERE msg_id = ?",
                (msg_id,),
            )
            conn.commit()

    def mark_failed(self, msg_id: str, error: str) -> bool:
        """登记失败并计算退避；**重试耗尽时转死信**。返回 ``True`` 表示还会自动重试。"""
        with self.db.sync_connect() as conn:
            row = conn.execute(
                "SELECT retry_count FROM ekbs_msg_inbox WHERE msg_id = ?", (msg_id,)
            ).fetchone()
            if row is None:
                return False
            retry_count = int(row["retry_count"])
            if retry_count >= self.max_retries:
                self._to_deadletter(conn, msg_id, error, retry_count)
                conn.commit()
                return False
            delay = BACKOFF_BASE_SECONDS * (2 ** max(retry_count - 1, 0))
            conn.execute(
                "UPDATE ekbs_msg_inbox SET status = 'failed', last_error = ?,"
                " next_retry_at = datetime('now', ?), updated_at = datetime('now')"
                " WHERE msg_id = ?",
                (error[:500], f"+{delay} seconds", msg_id),
            )
            conn.commit()
            return True

    def _to_deadletter(self, conn: Any, msg_id: str, reason: str, retry_count: int) -> None:
        row = conn.execute(
            "SELECT channel, sender_staff_id, conversation_id, conversation_type, text "
            "FROM ekbs_msg_inbox WHERE msg_id = ?",
            (msg_id,),
        ).fetchone()
        payload = json.dumps(dict(row) if row else {}, ensure_ascii=False)
        conn.execute(
            "INSERT INTO ekbs_msg_deadletter(msg_id, channel, payload, reason, retry_count)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT(msg_id) DO UPDATE SET"
            " reason = excluded.reason, retry_count = excluded.retry_count",
            (msg_id, str(row["channel"]) if row else "dingtalk_robot",
             payload, reason[:500], retry_count),
        )
        # inbox 行保留为 failed + next_retry_at=NULL（终态）：防止删除后重投被当成新消息
        conn.execute(
            "UPDATE ekbs_msg_inbox SET status = 'failed', next_retry_at = NULL, last_error = ?,"
            " updated_at = datetime('now') WHERE msg_id = ?",
            (f"deadletter: {reason[:480]}", msg_id),
        )
        log.error("msg_deadletter", msg_id=msg_id, reason=reason[:160], retries=retry_count)
        try:
            from ekbs.observability.metrics import inc

            inc("ekbs_dingtalk_msg_deadletter_total", 1)
        except Exception:  # noqa: BLE001 - 指标失败不得影响主链路
            pass

    # ------------------------------------------------------------------
    # 崩溃恢复（启动/周期扫描）
    # ------------------------------------------------------------------

    def pending_recovery(self, *, limit: int = 20) -> list[InboxRow]:
        """待重驱动的消息：卡在 received（>60s）、processing 超租约、failed 退避到期。

        ``received`` 行必须带 60s 年龄守卫：正常 ``handle()`` 里 accept→mark_processing
        之间有一个亚秒窗口，无守卫会被恢复扫描误判为崩溃残留（``accept`` 会再拦一道，
        但白噪一次重驱动）。
        """
        with self.db.sync_connect() as conn:
            rows = conn.execute(
                "SELECT msg_id, channel, sender_staff_id, conversation_id, conversation_type,"
                "       text, status, retry_count "
                "FROM ekbs_msg_inbox "
                "WHERE (status = 'received' AND updated_at < datetime('now', '-60 seconds')) "
                "   OR (status = 'processing' AND updated_at < datetime('now', ?)) "
                "   OR (status = 'failed' AND next_retry_at IS NOT NULL"
                "        AND next_retry_at <= datetime('now')) "
                "ORDER BY created_at ASC LIMIT ?",
                (f"-{self.lease_seconds} seconds", int(limit)),
            ).fetchall()
        return [
            InboxRow(
                msg_id=str(r["msg_id"]),
                channel=str(r["channel"]),
                sender_staff_id=str(r["sender_staff_id"]),
                conversation_id=str(r["conversation_id"]),
                conversation_type=str(r["conversation_type"]),
                text=str(r["text"]),
                status=str(r["status"]),
                retry_count=int(r["retry_count"]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # 运维查询（管理台/排错用）
    # ------------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        with self.db.sync_connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM ekbs_msg_inbox GROUP BY status"
            ).fetchall()
            dead = conn.execute("SELECT COUNT(*) FROM ekbs_msg_deadletter").fetchone()[0]
        result = {str(r["status"]): int(r["n"]) for r in rows}
        result["deadletter"] = int(dead)
        return result
