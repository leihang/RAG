"""限流 —— `04` §5.2 的 ``limits`` 配置 + `01` §5.5 的 ``ekbs_rate_limit`` 表。

栈里没有 Redis（D8），因此用 SQLite 表做**固定窗口**计数：

* 窗口 = 1 秒（配置里的 ``*_qps`` 就是每秒请求数）；桶为 ``(bucket, window_start)``，
  正是 ``ekbs_rate_limit`` 的主键；
* 三个维度（`04` §5.2）：``global:-`` / ``user:<user_id>`` / ``conv:<conversation_id>``；
* 计数用 ``INSERT ... ON CONFLICT DO UPDATE ... RETURNING`` 一条语句完成，
  避免「先查后写」的竞态（这要求 SQLite ≥3.35，见 `01` §5 的版本要求）。

**为什么放在服务端**：钉钉侧的投递速率不受我们控制，而云端嵌入/rerank/LLM 都是按量计费的
（`00` §6）。限流是成本闸门，也是「一个用户刷爆全局配额」的兜底。

⚠️ 语义边界（诚实交代）：SQLite 单写者 + 固定窗口，在 ``workers=1``（本栈强制）下是准的；
若将来上多副本，这个实现会因多写者竞争而偏松，届时需换 Redis 或集中式限流（`01` §15 的迁移项）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from ekbs.config import Settings
from ekbs.db import Database
from ekbs.logging import get_logger

log = get_logger(component="ratelimit")

BUCKET_GLOBAL = "global:-"


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    bucket: str
    limit: int
    current: int
    window_start: int

    @property
    def retry_after_seconds(self) -> int:
        return 1


class RateLimitExceeded(Exception):
    def __init__(self, decision: RateLimitDecision) -> None:
        super().__init__(f"rate limit exceeded: {decision.bucket} {decision.current}/{decision.limit}")
        self.decision = decision


class RateLimiter:
    def __init__(
        self, db: Database, settings: Settings, *, clock: Callable[[], float] | None = None
    ) -> None:
        self.db = db
        self.settings = settings
        self._clock = clock or time.time

    # ------------------------------------------------------------------

    def limits(self) -> dict[str, int]:
        limits = self.settings.limits or {}
        return {
            "global": int(limits.get("global_qps", 0) or 0),
            "user": int(limits.get("per_user_qps", 0) or 0),
            "conversation": int(limits.get("per_conversation_qps", 0) or 0),
        }

    def _bump(self, bucket: str, window_start: int) -> int:
        with self.db.sync_connect() as conn:
            row = conn.execute(
                "INSERT INTO ekbs_rate_limit(bucket, window_start, count) VALUES (?, ?, 1) "
                "ON CONFLICT(bucket, window_start) DO UPDATE SET count = count + 1 "
                "RETURNING count",
                (bucket, window_start),
            ).fetchone()
            conn.commit()
        return int(row["count"])

    # ------------------------------------------------------------------

    def check(
        self, *, user_id: int | None = None, conversation_id: str | None = None
    ) -> RateLimitDecision:
        """按 全局 → 会话 → 用户 的顺序判定；返回第一个被拒的维度。"""
        limits = self.limits()
        window_start = int(self._clock())
        checks: list[tuple[str, int]] = [(BUCKET_GLOBAL, limits["global"])]
        if conversation_id:
            checks.append((f"conv:{conversation_id}", limits["conversation"]))
        if user_id is not None:
            checks.append((f"user:{user_id}", limits["user"]))

        last = RateLimitDecision(True, BUCKET_GLOBAL, limits["global"], 0, window_start)
        for bucket, limit in checks:
            if limit <= 0:  # 0 或未配置 = 不限制该维度
                continue
            current = self._bump(bucket, window_start)
            decision = RateLimitDecision(current <= limit, bucket, limit, current, window_start)
            if not decision.allowed:
                from ekbs.observability.metrics import RATELIMIT_HIT, inc

                inc(RATELIMIT_HIT, 1, scope=bucket.split(":", 1)[0])
                log.warning(
                    "rate_limited", bucket=bucket, current=current, limit=limit, user_id=user_id
                )
                return decision
            last = decision
        return last

    def enforce(self, *, user_id: int | None = None, conversation_id: str | None = None) -> None:
        decision = self.check(user_id=user_id, conversation_id=conversation_id)
        if not decision.allowed:
            raise RateLimitExceeded(decision)
