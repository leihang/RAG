"""限流（`04` §5.2 + `01` §5.5 的 ``ekbs_rate_limit``）。"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from ekbs.api.app import create_app
from ekbs.ratelimit import RateLimiter, RateLimitExceeded
from tests.conftest import TEST_INTERNAL_TOKEN, user_id_of


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def limiter(db, settings):
    return RateLimiter(db, settings, clock=FakeClock())


def test_per_user_limit_blocks_third_request(db, settings) -> None:
    settings.limits = {"global_qps": 0, "per_user_qps": 2, "per_conversation_qps": 0}
    clock = FakeClock()
    limiter = RateLimiter(db, settings, clock=clock)
    assert limiter.check(user_id=7).allowed
    assert limiter.check(user_id=7).allowed
    decision = limiter.check(user_id=7)
    assert decision.allowed is False
    assert decision.bucket == "user:7" and decision.current == 3 and decision.limit == 2


def test_window_rollover_resets_counter(db, settings) -> None:
    settings.limits = {"global_qps": 0, "per_user_qps": 1, "per_conversation_qps": 0}
    clock = FakeClock()
    limiter = RateLimiter(db, settings, clock=clock)
    assert limiter.check(user_id=7).allowed
    assert limiter.check(user_id=7).allowed is False
    clock.now += 1.0  # 进入下一秒
    assert limiter.check(user_id=7).allowed


def test_global_limit_covers_different_users(db, settings) -> None:
    settings.limits = {"global_qps": 2, "per_user_qps": 0, "per_conversation_qps": 0}
    limiter = RateLimiter(db, settings, clock=FakeClock())
    assert limiter.check(user_id=1).allowed
    assert limiter.check(user_id=2).allowed
    decision = limiter.check(user_id=3)
    assert decision.allowed is False and decision.bucket == "global:-"


def test_conversation_limit_is_separate_from_user(db, settings) -> None:
    settings.limits = {"global_qps": 0, "per_user_qps": 0, "per_conversation_qps": 1}
    limiter = RateLimiter(db, settings, clock=FakeClock())
    assert limiter.check(user_id=1, conversation_id="conv-A").allowed
    assert limiter.check(user_id=2, conversation_id="conv-A").allowed is False
    assert limiter.check(user_id=1, conversation_id="conv-B").allowed


def test_zero_limit_means_unlimited(db, settings) -> None:
    settings.limits = {"global_qps": 0, "per_user_qps": 0, "per_conversation_qps": 0}
    limiter = RateLimiter(db, settings, clock=FakeClock())
    for _ in range(50):
        assert limiter.check(user_id=7).allowed


def test_enforce_raises_with_decision(db, settings) -> None:
    settings.limits = {"global_qps": 0, "per_user_qps": 1, "per_conversation_qps": 0}
    limiter = RateLimiter(db, settings, clock=FakeClock())
    limiter.enforce(user_id=7)
    with pytest.raises(RateLimitExceeded) as excinfo:
        limiter.enforce(user_id=7)
    assert excinfo.value.decision.bucket == "user:7"
    assert excinfo.value.decision.retry_after_seconds == 1


def test_counters_land_in_sqlite_table(db, settings) -> None:
    """限流计数必须落 ``ekbs_rate_limit``（替代 Redis 滑动窗口，01 §5.5）。"""
    settings.limits = {"global_qps": 5, "per_user_qps": 5, "per_conversation_qps": 5}
    limiter = RateLimiter(db, settings, clock=FakeClock())
    limiter.check(user_id=7, conversation_id="c1")
    with db.sync_connect() as conn:
        rows = {
            str(r["bucket"]): int(r["count"])
            for r in conn.execute("SELECT bucket, count FROM ekbs_rate_limit").fetchall()
        }
    assert rows["global:-"] == 1
    assert rows["user:7"] == 1
    assert rows["conv:c1"] == 1


async def test_api_returns_429_when_user_limit_hit(seeded) -> None:
    """超限返回 429 + Retry-After（而不是把请求打到云端烧钱）。"""
    runtime, _ = seeded
    runtime.settings.limits = {"global_qps": 0, "per_user_qps": 1, "per_conversation_qps": 0}
    app = create_app(runtime.settings)
    alice_id = user_id_of(runtime.db, "demo_alice")
    headers = {
        "X-Internal-Token": TEST_INTERNAL_TOKEN,
        "X-Internal-User-Id": str(alice_id),
    }
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            first = await client.post(
                "/api/v1/qa/chat", json={"question": "年假有多少天"}, headers=headers
            )
            assert first.status_code == 200
            second = await client.post(
                "/api/v1/qa/chat", json={"question": "年假有多少天"}, headers=headers
            )
            assert second.status_code == 429
            assert second.headers.get("Retry-After") == "1"
            assert "user:" in second.json()["detail"]
