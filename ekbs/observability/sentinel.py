"""每日质量哨兵（`00` v4.0 D21、`08` §5.1）—— 把质量恶化的发现周期从「月」压缩到「日」。

工作方式：每天从 ``ekbs_qa_message`` 抽样最近 24h 的 assistant 消息（含拒答），
用**规则型忠实度**（:func:`ekbs.retrieval.faithfulness.assess_faithfulness`）重估
答案支撑率，并统计拒答率；指标写 ``ekbs_metric``，劣化结论写
``ekbs_quality_degraded``（0/1），由 ``ALERT_RULES`` 对应规则告警（P1）。

为什么默认不用云端 LLM judge：judge 必须**异源**（`02` §3.6.2 三约束，否则
「用模型考模型」），未配置异源 judge 时「跳过当日」等于没有哨兵。规则型检查
永远可用、零成本、可复现 —— 它只能发现「明显胡编数字/该引不引」，覆盖率不如
LLM judge，但按「先有无、后精深」排序，它是哨兵的底线层；异源 LLM judge 作为
二期增强位预留（接入时同样写这三个指标，告警规则不变）。

抽样与数据纪律：
* 样本只取自 ``ekbs_qa_message`` / ``ekbs_qa_citation``（本系统自己的审计表）；
* 金丝雀/探针账号的消息**必须排除**（否则会污染运营与质量口径，`08` §5.3）——
  经 ``ekbs_qa_session.channel`` 与 ``ekbs_user`` 排除（当前无探针账号时为空集）；
* 抽样用 ``ORDER BY RANDOM()``：日常量级（数千/日）下足够均匀且实现最简单。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from ekbs.db import Database
from ekbs.logging import get_logger
from ekbs.observability import metrics as m
from ekbs.retrieval.faithfulness import assess_faithfulness, mostly_unsupported

log = get_logger(component="quality_sentinel")

#: 每日抽样条数（08 §5.1：≥50）
SAMPLE_SIZE = 50
#: 支撑率劣化阈值：相对 7 日基线降幅超过该值即 degraded（08 §5.1：>0.03）
SUPPORT_DROP_THRESHOLD = 0.03
#: 拒答率阈值（07 §7.8 的「无命中率 ≤15%」运营口径）
REFUSAL_RATE_THRESHOLD = 0.15
#: 抽样窗口（小时）
SAMPLE_WINDOW_HOURS = 24
#: 基线窗口（天）：取该窗口内哨兵支撑率的均值
BASELINE_DAYS = 7


@dataclass
class SentinelReport:
    sampled: int = 0
    answered: int = 0
    refused: int = 0
    supported: int = 0
    unsupported: int = 0
    support_rate: float | None = None
    refusal_rate: float | None = None
    baseline: float | None = None
    degraded: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "sampled": self.sampled,
            "answered": self.answered,
            "refused": self.refused,
            "supported": self.supported,
            "unsupported": self.unsupported,
            "support_rate": self.support_rate,
            "refusal_rate": self.refusal_rate,
            "baseline": self.baseline,
            "degraded": list(self.degraded),
        }


class QualitySentinel:
    """一次哨兵评测的全部步骤（同步实现，供 scheduler 作业直接调用）。"""

    def __init__(self, db: Database, *, sample_size: int = SAMPLE_SIZE) -> None:
        self.db = db
        self.sample_size = int(sample_size)

    # ------------------------------------------------------------------
    # 抽样与评测
    # ------------------------------------------------------------------

    def _sample(self) -> list[Any]:
        """最近 24h 的 assistant 消息随机抽样（排除探针/金丝雀渠道）。"""
        with self.db.sync_connect() as conn:
            return conn.execute(
                "SELECT m.message_id, m.answer, m.refused "
                "FROM ekbs_qa_message m "
                "JOIN ekbs_qa_session s ON s.session_id = m.session_id "
                "WHERE m.role = 'assistant' "
                "  AND m.created_at >= datetime('now', ?) "
                "  AND s.channel NOT IN ('synthetic_probe', 'canary') "
                "ORDER BY RANDOM() LIMIT ?",
                (f"-{SAMPLE_WINDOW_HOURS} hours", self.sample_size),
            ).fetchall()

    def _evidence_chunks(self, message_id: int) -> list[SimpleNamespace]:
        """该消息的引用分块原文（经 ekbs_qa_citation 回查，C4 保证原文在库）。"""
        with self.db.sync_connect() as conn:
            rows = conn.execute(
                "SELECT c.content, c.heading_path, d.title AS doc_title "
                "FROM ekbs_qa_citation ci "
                "JOIN ekbs_chunk c ON c.chunk_id = ci.chunk_id "
                "JOIN ekbs_document d ON d.doc_id = c.doc_id "
                "WHERE ci.message_id = ?",
                (message_id,),
            ).fetchall()
        return [
            SimpleNamespace(
                content=str(r["content"] or ""),
                heading_path=str(r["heading_path"] or ""),
                doc_title=str(r["doc_title"] or ""),
            )
            for r in rows
        ]

    def _baseline(self) -> float | None:
        """近 7 日哨兵支撑率均值（不含今天这次写入之前的历史）。"""
        with self.db.sync_connect() as conn:
            row = conn.execute(
                "SELECT AVG(value) AS avg_value, COUNT(*) AS n FROM ekbs_metric "
                "WHERE name = ? AND created_at >= datetime('now', ?) "
                "AND created_at < datetime('now', 'start of day')",
                (m.QUALITY_SUPPORT_RATE, f"-{BASELINE_DAYS} days"),
            ).fetchone()
        if row is None or not int(row["n"]):
            return None
        return float(row["avg_value"])

    # ------------------------------------------------------------------

    def run(self) -> SentinelReport:
        report = SentinelReport()
        rows = self._sample()
        report.sampled = len(rows)
        if not rows:
            # 没有流量 = 无法判定（不是「质量为 0」）：跳过当日并留痕（08 §5.1 纪律）
            log.warning("quality_sentinel_skipped", reason="no_samples_in_24h")
            return report

        for row in rows:
            if int(row["refused"]):
                report.refused += 1
                continue
            answer = str(row["answer"] or "").strip()
            if not answer:
                continue  # 空答案（异常记录）不计入两侧
            report.answered += 1
            chunks = self._evidence_chunks(int(row["message_id"]))
            faith = assess_faithfulness(answer, chunks)
            if faith.ok and not mostly_unsupported(faith):
                report.supported += 1
            else:
                report.unsupported += 1

        if report.answered:
            report.support_rate = round(report.supported / report.answered, 4)
        report.refusal_rate = round(report.refused / report.sampled, 4)
        report.baseline = self._baseline()

        # 劣化判定（08 §5.1 双条件）
        if (
            report.support_rate is not None
            and report.baseline is not None
            and report.baseline - report.support_rate > SUPPORT_DROP_THRESHOLD
        ):
            report.degraded.append("support_drop")
        if report.refusal_rate is not None and report.refusal_rate > REFUSAL_RATE_THRESHOLD:
            report.degraded.append("refusal_rate_high")

        self._write_metrics(report)
        (log.warning if report.degraded else log.info)(
            "quality_sentinel_done", **report.as_dict()
        )
        return report

    def _write_metrics(self, report: SentinelReport) -> None:
        rows: list[tuple[str, float, None]] = [
            (m.QUALITY_SENTINEL_SAMPLES, float(report.sampled), None),
            # degraded 恒写（0/1）：告警规则按 gauge ≥1 判定
            (m.QUALITY_DEGRADED, 1.0 if report.degraded else 0.0, None),
        ]
        # 支撑率/拒答率为 None（无有效样本侧）时不写 —— gauge 规则「无采样 = 无法判定」
        if report.support_rate is not None:
            rows.append((m.QUALITY_SUPPORT_RATE, float(report.support_rate), None))
        if report.refusal_rate is not None:
            rows.append((m.QUALITY_REFUSAL_RATE, float(report.refusal_rate), None))
        with self.db.sync_connect() as conn:
            conn.executemany(
                "INSERT INTO ekbs_metric(name, value, labels, created_at) "
                "VALUES (?, ?, ?, datetime('now'))",
                rows,
            )
            conn.commit()


def run_sentinel(db: Database, *, sample_size: int = SAMPLE_SIZE) -> SentinelReport:
    """便捷入口（scheduler 作业用）。"""
    return QualitySentinel(db, sample_size=sample_size).run()
