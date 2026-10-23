"""指标登记表与阈值告警（`04` §7.7）。

两条最要紧的语义：
* 快照时**计数器清零、仪表量保留**（与 Prometheus 一致，否则告警失去意义）；
* 阈值**只有一处定义**（`alerts.ALERT_RULES`），且分母为 0 时不告警。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from ekbs.observability import alerts as a
from ekbs.observability import metrics as m


@pytest.fixture(autouse=True)
def _reset():
    m.reset()
    yield
    m.reset()


def _write_metric(runtime, name: str, values: list[float], *, labels: dict | None = None,
                  seconds_ago: list[int] | None = None) -> None:
    """往 ekbs_metric 写历史采样（模拟采集作业已经写过的数据）。"""
    seconds_ago = seconds_ago or [len(values) - index for index in range(len(values))]
    with runtime.db.sync_connect() as conn:
        for value, age in zip(values, seconds_ago):
            conn.execute(
                "INSERT INTO ekbs_metric(name, value, labels, created_at) "
                "VALUES (?, ?, ?, datetime('now', ?))",
                (name, value, json.dumps(labels) if labels else None, f"-{age} seconds"),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# 登记表语义
# ---------------------------------------------------------------------------


def test_counters_are_cleared_and_gauges_retained_on_snapshot() -> None:
    m.inc("demo_total", 3, scope="x")
    m.set_gauge("demo_gauge", 7)
    first = dict((name, value) for name, value, _ in m.snapshot())
    assert first["demo_total"] == 3 and first["demo_gauge"] == 7
    # 计数器读后清零；仪表量保留
    second = dict((name, value) for name, value, _ in m.snapshot())
    assert "demo_total" not in second
    assert second["demo_gauge"] == 7


def test_observe_emits_p95_and_count() -> None:
    for value in [1, 2, 3, 4, 100]:
        m.observe("demo_latency_seconds", value)
    snap = {name: value for name, value, _ in m.snapshot()}
    assert snap["demo_latency_seconds_count"] == 5
    assert snap["demo_latency_seconds_p95"] == 100


# ---------------------------------------------------------------------------
# 阈值表
# ---------------------------------------------------------------------------


def test_threshold_table_matches_doc_levels() -> None:
    """`04` §7.7 的表 + `08` §5.1 的质量哨兵两条：19 条，P1×3 / P2×11 / P3×5。"""
    levels = [rule.severity for rule in a.ALERT_RULES]
    assert len(a.ALERT_RULES) == 19
    assert levels.count("P1") == 3
    assert levels.count("P2") == 11
    assert levels.count("P3") == 5


def test_p1_rules_are_the_security_critical_ones() -> None:
    p1 = {rule.name for rule in a.ALERT_RULES if rule.severity == "P1"}
    assert p1 == {m.STREAM_CONNECTED, m.PERM_REVOKE_LAG, m.QUALITY_DEGRADED}


def test_sum_window_fires(runtime) -> None:
    rule = next(r for r in a.ALERT_RULES if r.name == m.RECONNECT_TOTAL)
    _write_metric(runtime, m.RECONNECT_TOTAL, [2, 2, 2])  # 6 次 > 5
    hit = a.evaluate_rule(runtime.db, rule)
    assert hit is not None and hit.value == 6 and hit.severity == "P2"


def test_sum_window_does_not_fire_below_threshold(runtime) -> None:
    rule = next(r for r in a.ALERT_RULES if r.name == m.RECONNECT_TOTAL)
    _write_metric(runtime, m.RECONNECT_TOTAL, [1, 1])
    assert a.evaluate_rule(runtime.db, rule) is None


def test_rate_window_fires_on_failure_ratio(runtime) -> None:
    rule = next(r for r in a.ALERT_RULES if r.name == m.MESSAGE_FAILED)
    _write_metric(runtime, m.MESSAGE_PROCESSED, [100])
    _write_metric(runtime, m.MESSAGE_FAILED, [5])  # 5%
    hit = a.evaluate_rule(runtime.db, rule)
    assert hit is not None and hit.value == pytest.approx(0.05)


def test_rate_window_with_zero_denominator_does_not_alert(runtime) -> None:
    """分母为 0 是「没有流量」，不是「失败率 100%」—— 否则夜间会疯狂误报。"""
    rule = next(r for r in a.ALERT_RULES if r.name == m.MESSAGE_FAILED)
    _write_metric(runtime, m.MESSAGE_FAILED, [3])
    assert a.evaluate_rule(runtime.db, rule) is None


def test_rate_window_respects_label_filters(runtime) -> None:
    """卡片失败率只统计 result=failure 的那一路。"""
    rule = next(r for r in a.ALERT_RULES if r.name == m.CARD_SEND)
    _write_metric(runtime, m.CARD_SEND, [100], labels={"result": "success"})
    _write_metric(runtime, m.CARD_SEND, [1], labels={"result": "failure"})
    assert a.evaluate_rule(runtime.db, rule) is None  # 1/101 ≈ 1% < 3%

    _write_metric(runtime, m.CARD_SEND, [10], labels={"result": "failure"})
    hit = a.evaluate_rule(runtime.db, rule)
    assert hit is not None and hit.value == pytest.approx(11 / 111, rel=1e-3)


def test_gauge_sustained_requires_all_samples_violating(runtime) -> None:
    """「Stream 未连接持续 60s」：中间连上过一次就不该告警。"""
    rule = next(r for r in a.ALERT_RULES if r.name == m.STREAM_CONNECTED)
    _write_metric(runtime, m.STREAM_CONNECTED, [0, 1, 0])
    assert a.evaluate_rule(runtime.db, rule) is None
    with runtime.db.sync_connect() as conn:
        conn.execute("DELETE FROM ekbs_metric WHERE name = ?", (m.STREAM_CONNECTED,))
        conn.commit()
    _write_metric(runtime, m.STREAM_CONNECTED, [0, 0, 0])
    hit = a.evaluate_rule(runtime.db, rule)
    assert hit is not None and hit.severity == "P1"


def test_latest_gauge_comparison(runtime) -> None:
    """token 剩余有效期 < 300s → P3。"""
    rule = next(r for r in a.ALERT_RULES if r.name == m.TOKEN_EXPIRE)
    _write_metric(runtime, m.TOKEN_EXPIRE, [4000, 200])
    hit = a.evaluate_rule(runtime.db, rule)
    assert hit is not None and hit.value == 200


def test_p95_window_fires(runtime) -> None:
    rule = next(r for r in a.ALERT_RULES if r.name == m.FIRST_TOKEN)
    _write_metric(runtime, f"{m.FIRST_TOKEN}_p95", [7.5])
    hit = a.evaluate_rule(runtime.db, rule)
    assert hit is not None and hit.value == pytest.approx(7.5)


def test_growth_window_fires_on_spike(runtime) -> None:
    """重复消息拦截突增 10 倍 → P3。"""
    rule = next(r for r in a.ALERT_RULES if r.name == m.IDEMPOTENT_HIT)
    _write_metric(runtime, m.IDEMPOTENT_HIT, [1], seconds_ago=[500])
    _write_metric(runtime, m.IDEMPOTENT_HIT, [30], seconds_ago=[10])
    hit = a.evaluate_rule(runtime.db, rule)
    assert hit is not None and hit.value == pytest.approx(30.0)


def test_per_label_max_groups_by_card(runtime) -> None:
    """「单卡片流式更新 > 200 次」必须按卡片分组，不能把多张卡片加在一起。"""
    rule = next(r for r in a.ALERT_RULES if r.name == m.CARD_STREAMING)
    # 同一张卡片累计 250 次 > 200 → 告警（按标签分组求和，而不是把不同卡片混在一起）
    for _ in range(5):
        _write_metric(runtime, m.CARD_STREAMING, [50], labels={"card_id": "c-1"})
    hit = a.evaluate_rule(runtime.db, rule)
    assert hit is not None and hit.value == pytest.approx(250.0)

    with runtime.db.sync_connect() as conn:
        conn.execute("DELETE FROM ekbs_metric WHERE name = ?", (m.CARD_STREAMING,))
        conn.commit()
    for card in ("c-1", "c-2", "c-3", "c-4"):
        _write_metric(runtime, m.CARD_STREAMING, [60], labels={"card_id": card})
    assert a.evaluate_rule(runtime.db, rule) is None  # 每张 60 次，都不超


def test_business_hours_only_rule(runtime) -> None:
    """业务时段（周一~周五 09:00-18:00）之外不判「5 分钟没消息」。"""
    rule = next(r for r in a.ALERT_RULES if r.name == m.MESSAGE_RECEIVED)
    saturday = datetime(2026, 9, 26, 3, 0, tzinfo=timezone.utc)  # 周六 11:00 北京
    assert a.in_business_hours(saturday) is False
    assert a.evaluate_rule(runtime.db, rule, now=saturday) is None
    monday_noon = datetime(2026, 9, 28, 4, 0, tzinfo=timezone.utc)  # 周一 12:00 北京
    assert a.in_business_hours(monday_noon) is True
    assert a.evaluate_rule(runtime.db, rule, now=monday_noon) is not None


def test_record_alerts_writes_security_events_for_p1_p2_only(runtime) -> None:
    p1 = next(r for r in a.ALERT_RULES if r.severity == "P1")
    p3 = next(r for r in a.ALERT_RULES if r.severity == "P3")
    hits = [a.AlertHit(p1, 0.0, 3), a.AlertHit(p3, 999.0, 1)]
    written = a.record_alerts(runtime.db, hits)
    assert written == 1
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT severity, detail FROM ekbs_security_event WHERE event_type='metric_alert'"
        ).fetchall()
    assert len(rows) == 1 and rows[0]["severity"] == "high"
    assert p1.name in str(rows[0]["detail"])


def test_scheduler_job_writes_metrics_and_evaluates_alerts(runtime) -> None:
    """采集作业必须把进程内指标写进 ekbs_metric 并顺手判告警（04 §7.7）。"""
    from ekbs.scheduler import job_metrics_collect

    m.inc(m.MESSAGE_RECEIVED, 4, msgtype="text")
    job_metrics_collect(runtime)
    with runtime.db.sync_connect() as conn:
        names = {
            str(r["name"])
            for r in conn.execute(
                "SELECT DISTINCT name FROM ekbs_metric WHERE created_at > datetime('now','-1 minute')"
            ).fetchall()
        }
    assert {"faiss_shards", "disk_used_pct", m.MESSAGE_RECEIVED} <= names


def test_scheduler_job_fires_alert_end_to_end(runtime) -> None:
    from ekbs.scheduler import job_metrics_collect

    # 制造一次超阈值：1 小时内重连 6 次（阈值 > 5）
    _write_metric(runtime, m.RECONNECT_TOTAL, [6], seconds_ago=[60])
    job_metrics_collect(runtime)
    # ⚠️ 不能只取"最新一条"：**业务时段**还会额外触发
    # 「5 分钟内没有收到任何消息」那条规则（实测在 09:51 跑就命中了），
    # 它排在后面前面就把重连告警挤掉 → 用例按**当前钟点**随机红。
    # 断言改成"本次采集落下的告警里**包含**重连告警"，与钟点无关。
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT detail FROM ekbs_security_event WHERE event_type='metric_alert' "
            "AND created_at > datetime('now','-1 minute')"
        ).fetchall()
    details = [str(r["detail"]) for r in rows]
    assert details, "本次采集没有落下任何 metric_alert"
    assert any(m.RECONNECT_TOTAL in d for d in details), (
        "本次采集的告警里没有重连告警，实际落库的是：\n  " + "\n  ".join(details)
    )
