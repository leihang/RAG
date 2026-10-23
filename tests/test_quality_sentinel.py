"""每日质量哨兵（00 v4.0 D21、08 §5.1）。

核心断言：
* 健康流量 → support_rate=1.0、degraded=0、指标齐写；
* 答案编造（claim 对不上证据）+ 历史基线高 → support_drop 劣化 + degraded=1 + 告警规则命中；
* 拒答率 >15% → refusal_rate_high；
* 无流量 → 跳过当日（不写指标），绝不是「质量为 0」。
"""

from __future__ import annotations

import uuid

import pytest

from ekbs.observability import metrics as m
from ekbs.observability.sentinel import QualitySentinel, run_sentinel
from tests.conftest import user_id_of


def _seed_session_and_messages(runtime, messages: list[dict]) -> None:
    """把合成的问答记录写进审计表（哨兵的输入）。

    ``messages`` 每项：{"answer": str|None, "refused": 0|1, "cite_chunk": bool}
    """
    alice_id = user_id_of(runtime.db, "demo_alice")
    with runtime.db.sync_connect() as conn:
        hr_chunk = conn.execute(
            "SELECT c.chunk_id, c.doc_id FROM ekbs_chunk c "
            "JOIN ekbs_document d ON d.doc_id = c.doc_id "
            "WHERE d.title = '年假管理制度' AND c.chunk_type = 'child' LIMIT 1"
        ).fetchone()
        for item in messages:
            session_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO ekbs_qa_session(session_id, user_id, channel, perm_fingerprint,"
                " clearance_at) VALUES (?, ?, 'web', 'fp-test', 2)",
                (session_id, alice_id),
            )
            cursor = conn.execute(
                "INSERT INTO ekbs_qa_message(session_id, turn, role, question, answer,"
                " refused) VALUES (?, 1, 'assistant', '年假有多少天', ?, ?)",
                (session_id, item.get("answer"), int(item.get("refused", 0))),
            )
            if item.get("cite_chunk") and hr_chunk is not None:
                conn.execute(
                    "INSERT INTO ekbs_qa_citation(message_id, cite_index, chunk_id, doc_id)"
                    " VALUES (?, 1, ?, ?)",
                    (int(cursor.lastrowid), int(hr_chunk["chunk_id"]), int(hr_chunk["doc_id"])),
                )
        conn.commit()


def _quality_metrics(runtime, name: str) -> list[float]:
    with runtime.db.sync_connect() as conn:
        return [
            float(r["value"])
            for r in conn.execute(
                "SELECT value FROM ekbs_metric WHERE name = ? ORDER BY metric_id", (name,)
            ).fetchall()
        ]


#: 不含特异数字但带引用的答案 → 规则型检查判 supported
SUPPORTED_ANSWER = "员工年假天数按司龄确定，入职越久年假越多，具体以年假管理制度为准 [1]"
#: 含 3 个以上**特异**数字（≥3 位）且全部对不上证据 → unsupported
FABRICATED_ANSWER = "入职满 100 年享受 999 天，满 300 年享受 888 天，满 500 年享受 777 天 [1]"


async def test_sentinel_healthy_traffic(seeded) -> None:
    runtime, _ = seeded
    _seed_session_and_messages(
        runtime,
        [{"answer": SUPPORTED_ANSWER, "cite_chunk": True} for _ in range(5)],
    )
    report = run_sentinel(runtime.db)
    assert report.sampled == 5 and report.answered == 5
    assert report.support_rate == 1.0
    assert report.refusal_rate == 0.0
    assert report.degraded == []
    assert _quality_metrics(runtime, m.QUALITY_SUPPORT_RATE) == [1.0]
    assert _quality_metrics(runtime, m.QUALITY_DEGRADED) == [0.0]
    assert _quality_metrics(runtime, m.QUALITY_SENTINEL_SAMPLES) == [5.0]


async def test_sentinel_detects_support_drop(seeded) -> None:
    """编造答案 + 高基线 → support_drop；degraded 指标 = 1 且告警规则命中（P1）。"""
    runtime, _ = seeded
    _seed_session_and_messages(
        runtime,
        [{"answer": FABRICATED_ANSWER, "cite_chunk": True} for _ in range(5)],
    )
    # 历史 7 日基线 0.95（今天之前）
    with runtime.db.sync_connect() as conn:
        for days_ago in ("-1 days", "-2 days", "-3 days"):
            conn.execute(
                "INSERT INTO ekbs_metric(name, value, created_at) VALUES (?, 0.95,"
                f" datetime('now', '{days_ago}'))",
                (m.QUALITY_SUPPORT_RATE,),
            )
        conn.commit()

    report = run_sentinel(runtime.db)
    assert report.support_rate == 0.0, "编造的特异数字必须被规则型检查拦下"
    assert report.baseline == pytest.approx(0.95)
    assert "support_drop" in report.degraded
    assert _quality_metrics(runtime, m.QUALITY_DEGRADED) == [1.0]

    # 告警接线端到端验证：gauge ≥1 → P1 命中
    from ekbs.observability.alerts import evaluate_all

    hits = evaluate_all(runtime.db)
    assert any(h.rule.name == m.QUALITY_DEGRADED for h in hits), (
        "degraded=1 必须触发 P1 告警规则（D21）"
    )


async def test_sentinel_detects_high_refusal_rate(seeded) -> None:
    runtime, _ = seeded
    _seed_session_and_messages(
        runtime,
        [{"answer": SUPPORTED_ANSWER, "cite_chunk": True}] * 3
        + [{"answer": None, "refused": 1}] * 2,
    )
    report = run_sentinel(runtime.db)
    assert report.refusal_rate == pytest.approx(0.4)
    assert "refusal_rate_high" in report.degraded
    assert _quality_metrics(runtime, m.QUALITY_REFUSAL_RATE) == [0.4]


async def test_sentinel_skips_when_no_traffic(seeded) -> None:
    """无流量 = 无法判定：跳过且不写任何质量指标（08 §5.1 纪律）。"""
    runtime, _ = seeded
    report = run_sentinel(runtime.db)
    assert report.sampled == 0
    assert report.degraded == []
    for name in (m.QUALITY_SUPPORT_RATE, m.QUALITY_REFUSAL_RATE, m.QUALITY_DEGRADED):
        assert _quality_metrics(runtime, name) == []


async def test_sentinel_excludes_probe_channels(seeded) -> None:
    """探针/金丝雀消息不得计入质量口径（08 §5.3）。"""
    runtime, _ = seeded
    alice_id = user_id_of(runtime.db, "demo_alice")
    with runtime.db.sync_connect() as conn:
        session_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO ekbs_qa_session(session_id, user_id, channel, perm_fingerprint,"
            " clearance_at) VALUES (?, ?, 'synthetic_probe', 'fp-probe', 2)",
            (session_id, alice_id),
        )
        conn.execute(
            "INSERT INTO ekbs_qa_message(session_id, turn, role, answer, refused)"
            " VALUES (?, 1, 'assistant', '探针答案 [1]', 0)",
            (session_id,),
        )
        conn.commit()
    sentinel = QualitySentinel(runtime.db)
    assert sentinel._sample() == [], "探针渠道必须被排除（_sample 白盒断言）"
