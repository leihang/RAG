"""告警阈值与判定（`04` §7.7）—— **阈值只有一处定义**。

文档明确：单机不部署 Alertmanager，判定写在 ``collect_metrics`` 作业里，
采集后逐条比对阈值，命中即写 ``ekbs_security_event``（P1/P2）并在管理台顶部显示红色横幅。
好处是「不会出现文档写一套阈值、告警规则文件里写另一套」这种典型的运维漂移。

因此 :data:`ALERT_RULES` 就是那张表，一条规则一行、阈值与级别都在这里，代码别处不再写死判断。

判定种类（覆盖表里全部规则）：

| kind | 含义 | 阈值比较 |
|---|---|---|
| ``sum_window`` | 窗口内计数**增量之和** | ``value > / >= threshold``（可 ``business_hours_only``、可 ``per_label_max``） |
| ``rate_window`` | 两个计数之比（失败率） | ``value > threshold``（可给标签过滤） |
| ``gauge`` | 仪表量当前值；``sustained=True`` 时要求窗口内**每个采样**都越界 | ``value > / < / == threshold`` |
| ``p95_window`` | 直方图 ``<name>_p95`` 在窗口内持续越界 | ``value > threshold`` |
| ``growth_window`` | 当前窗口之和 ÷ 上一窗口之和 | ``value > threshold``（突增倍数） |

窗口的实现方式：读 ``ekbs_metric`` 的历史行（`01` §5.5 的那张表），
因此不需要在进程内再存一份时间序列 —— 采集本身就是持久化。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any, Literal

from ekbs.db import Database
from ekbs.logging import get_logger
from ekbs.observability import metrics as m

log = get_logger(component="alerts")

Comparison = Literal[">", ">=", "<", "<=", "=="]
Kind = Literal["sum_window", "rate_window", "gauge", "p95_window", "growth_window"]

#: 业务时段（用于「业务时段 5 分钟内 = 0」这类规则；`04` §7.7）
BUSINESS_HOURS = (time(9, 0), time(18, 0))
BUSINESS_DAYS = (0, 1, 2, 3, 4)  # 周一到周五
SHANGHAI = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class AlertRule:
    name: str
    kind: Kind
    threshold: float
    window_seconds: int
    severity: str
    description: str
    comparison: Comparison = ">"
    numerator: str = ""
    denominator: str = ""
    numerator_labels: dict[str, str] = field(default_factory=dict)
    denominator_labels: dict[str, str] = field(default_factory=dict)
    sustained: bool = False
    business_hours_only: bool = False
    per_label_max: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "threshold": self.threshold,
            "window_seconds": self.window_seconds,
            "severity": self.severity,
            "comparison": self.comparison,
            "description": self.description,
            "sustained": self.sustained,
            "business_hours_only": self.business_hours_only,
        }


#: `04` §7.7 阈值表（**唯一权威定义**；改这里就等于改文档那张表）
ALERT_RULES: tuple[AlertRule, ...] = (
    AlertRule(
        m.STREAM_CONNECTED, "gauge", 0, 60, "P1",
        "Stream 未连接持续 60s", comparison="==", sustained=True,
    ),
    AlertRule(
        m.RECONNECT_TOTAL, "sum_window", 5, 3600, "P2",
        "1 小时内重连 > 5 次",
    ),
    AlertRule(
        m.MESSAGE_RECEIVED, "sum_window", 0, 300, "P2",
        "业务时段 5 分钟内没有收到任何消息", comparison="==", business_hours_only=True,
    ),
    AlertRule(
        m.MESSAGE_FAILED, "rate_window", 0.02, 300, "P2",
        "消息处理失败率 > 2%（持续 5 分钟窗口）",
        numerator=m.MESSAGE_FAILED, denominator=m.MESSAGE_PROCESSED,
    ),
    AlertRule(
        m.MESSAGE_LATENCY, "p95_window", 10.0, 300, "P2",
        "端到端处理耗时 P95 > 10s 持续 5 分钟",
    ),
    AlertRule(
        m.FIRST_TOKEN, "p95_window", 5.0, 600, "P2",
        "首字延迟 P95 > 5s 持续 10 分钟",
    ),
    AlertRule(
        m.CARD_SEND, "rate_window", 0.03, 300, "P2",
        "卡片发送失败率 > 3%",
        numerator=m.CARD_SEND, denominator=m.CARD_SEND,
        numerator_labels={"result": "failure"}, denominator_labels={},
    ),
    AlertRule(
        m.CARD_STREAMING, "sum_window", 200, 3600, "P3",
        "单卡片流式更新 > 200 次", per_label_max=True,
    ),
    AlertRule(
        m.RATELIMIT_HIT, "sum_window", 100, 3600, "P3",
        "1 小时内限流命中 > 100",
    ),
    AlertRule(
        m.TOKEN_REFRESH_FAILED, "sum_window", 1, 60, "P2",
        "钉钉 token 刷新失败", comparison=">=",
    ),
    AlertRule(
        m.TOKEN_EXPIRE, "gauge", 300, 60, "P3",
        "token 剩余有效期 < 300s", comparison="<",
    ),
    AlertRule(
        m.AUTH_LOGIN, "rate_window", 0.05, 300, "P2",
        "免登失败率 > 5%",
        numerator=m.AUTH_LOGIN, denominator=m.AUTH_LOGIN,
        numerator_labels={"result": "failure"}, denominator_labels={},
    ),
    AlertRule(
        m.AUTH_REPLAY, "sum_window", 10, 300, "P2",
        "防重放拦截 ≥ 10 次（安全）", comparison=">=",
    ),
    AlertRule(
        m.IDEMPOTENT_HIT, "growth_window", 10.0, 300, "P3",
        "重复消息拦截数突增 10 倍",
    ),
    AlertRule(
        m.ORG_SYNC_DURATION, "gauge", 1800, 300, "P3",
        "全量同步耗时 > 1800s",
    ),
    AlertRule(
        m.ORG_SYNC_FAILED, "sum_window", 1, 60, "P2",
        "组织同步失败", comparison=">=",
    ),
    AlertRule(
        m.PERM_REVOKE_LAG, "gauge", 300, 60, "P1",
        "权限回收滞后 > 300s（安全）",
    ),
    # ---- 质量哨兵（00 v4.0 D21）：窗口 25h，覆盖每日一次的产生节奏 ----
    AlertRule(
        m.QUALITY_DEGRADED, "gauge", 1, 90000, "P1",
        "质量哨兵劣化：支撑率相对 7 日基线降 >0.03 或拒答率 >15%",
        comparison=">=",
    ),
    AlertRule(
        m.QUALITY_REFUSAL_RATE, "gauge", 0.15, 90000, "P2",
        "质量哨兵：拒答率 >15%（知识覆盖度预警）",
        comparison=">",
    ),
)


@dataclass
class AlertHit:
    rule: AlertRule
    value: float
    samples: int

    @property
    def severity(self) -> str:
        return self.rule.severity

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.rule.name,
            "kind": self.rule.kind,
            "value": round(self.value, 4),
            "threshold": self.rule.threshold,
            "comparison": self.rule.comparison,
            "severity": self.severity,
            "window_seconds": self.rule.window_seconds,
            "samples": self.samples,
            "description": self.rule.description,
        }


# ---------------------------------------------------------------------------
# 窗口读取
# ---------------------------------------------------------------------------


def _labels_of(raw: str | None) -> dict[str, str]:
    import json

    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in (data or {}).items()} if isinstance(data, dict) else {}


def load_window(
    database: Database,
    name: str,
    window_seconds: int,
    *,
    offset_seconds: int = 0,
    labels: dict[str, str] | None = None,
) -> list[tuple[float, datetime]]:
    """读 ``ekbs_metric`` 里某个指标在窗口内的采样（按时间升序）。"""
    start = -int(window_seconds) - int(offset_seconds)
    end = -int(offset_seconds)
    sql = (
        "SELECT value, labels, created_at FROM ekbs_metric WHERE name = ? "
        "AND created_at > datetime('now', ?) AND created_at <= datetime('now', ?) "
        "ORDER BY created_at"
    )
    with database.sync_connect() as conn:
        rows = conn.execute(sql, (name, f"{start} seconds", f"{end} seconds")).fetchall()
    out: list[tuple[float, datetime]] = []
    for row in rows:
        if labels and not all(_labels_of(row["labels"]).get(k) == v for k, v in labels.items()):
            continue
        out.append((float(row["value"]), datetime.fromisoformat(str(row["created_at"]))))
    return out


def _sum_per_label(
    database: Database, name: str, window_seconds: int
) -> dict[tuple[tuple[str, str], ...], float]:
    """按标签组合分组求和（用于 ``per_label_max`` 规则）。"""
    sql = (
        "SELECT value, labels FROM ekbs_metric WHERE name = ? "
        "AND created_at > datetime('now', ?) ORDER BY created_at"
    )
    with database.sync_connect() as conn:
        rows = conn.execute(sql, (name, f"-{int(window_seconds)} seconds")).fetchall()
    grouped: dict[tuple[tuple[str, str], ...], float] = {}
    for row in rows:
        key = tuple(sorted(_labels_of(row["labels"]).items()))
        grouped[key] = grouped.get(key, 0.0) + float(row["value"])
    return grouped


def _compare(value: float, comparison: Comparison, threshold: float) -> bool:
    if comparison == ">":
        return value > threshold
    if comparison == ">=":
        return value >= threshold
    if comparison == "<":
        return value < threshold
    if comparison == "<=":
        return value <= threshold
    return value == threshold


def in_business_hours(now: datetime | None = None) -> bool:
    moment = (now or datetime.now(timezone.utc)).astimezone(SHANGHAI)
    return moment.weekday() in BUSINESS_DAYS and BUSINESS_HOURS[0] <= moment.time() < BUSINESS_HOURS[1]


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------


def _evaluate_rate_window(database: Database, rule: AlertRule) -> AlertHit | None:
    numerator = sum(
        value
        for value, _ in load_window(
            database, rule.numerator, rule.window_seconds, labels=rule.numerator_labels
        )
    )
    denominator = sum(
        value
        for value, _ in load_window(
            database, rule.denominator, rule.window_seconds, labels=rule.denominator_labels
        )
    )
    # 分母为 0 时**不告警**：那是「没有流量」，不是「失败率高」
    if denominator <= 0:
        return None
    value = numerator / denominator
    if _compare(value, rule.comparison, rule.threshold):
        return AlertHit(rule, value, 1)
    return None


def _evaluate_growth_window(database: Database, rule: AlertRule) -> AlertHit | None:
    current = sum(value for value, _ in load_window(database, rule.name, rule.window_seconds))
    previous = sum(
        value
        for value, _ in load_window(
            database, rule.name, rule.window_seconds, offset_seconds=rule.window_seconds
        )
    )
    if previous <= 0:
        return None
    value = current / previous
    if _compare(value, rule.comparison, rule.threshold):
        return AlertHit(rule, value, 2)
    return None


def _sample_value(database: Database, rule: AlertRule, metric_name: str, samples: list) -> float:
    if not samples:
        return 0.0
    if rule.per_label_max:
        # 「单卡片流式更新 > 200 次」要按标签（card_id）分组，取**每组之和的最大值**。
        # 早先的实现把所有值加在一起，等于把「多个卡片各 50 次」误报成「一张卡片 200 次」。
        grouped = _sum_per_label(database, metric_name, rule.window_seconds)
        return max(grouped.values(), default=0.0)
    if rule.kind == "sum_window":
        return sum(item[0] for item in samples)
    # p95/gauge：采集时已算好，取窗口内最近一次
    return samples[-1][0]


def _sustained_ok(rule: AlertRule, samples: list) -> bool:
    if rule.kind not in {"gauge", "p95_window"} or not rule.sustained:
        return True
    return all(_compare(item[0], rule.comparison, rule.threshold) for item in samples)


def evaluate_rule(database: Database, rule: AlertRule, *, now: datetime | None = None) -> AlertHit | None:
    if rule.business_hours_only and not in_business_hours(now):
        return None

    if rule.kind == "rate_window":
        return _evaluate_rate_window(database, rule)
    if rule.kind == "growth_window":
        return _evaluate_growth_window(database, rule)

    metric_name = f"{rule.name}_p95" if rule.kind == "p95_window" else rule.name
    samples = load_window(database, metric_name, rule.window_seconds)
    if not samples and rule.kind != "sum_window":
        # 仪表量/直方图没有采样 = 「无法判定」，不能当成 0
        return None
    # 而 sum_window 没有采样就是**和为零**：对「业务时段 5 分钟内没有收到消息」
    # 这类规则，零正是要告警的条件（早先在这里提前 return，漏报过）。
    value = _sample_value(database, rule, metric_name, samples)
    if not _sustained_ok(rule, samples):
        return None
    if _compare(value, rule.comparison, rule.threshold):
        return AlertHit(rule, value, len(samples))
    return None


def evaluate_all(
    database: Database, rules: tuple[AlertRule, ...] = ALERT_RULES, *, now: datetime | None = None
) -> list[AlertHit]:
    hits: list[AlertHit] = []
    for rule in rules:
        try:
            hit = evaluate_rule(database, rule, now=now)
        except Exception as exc:  # 单条规则出错不该影响其它规则
            log.error("alert_rule_failed", metric=rule.name, error=str(exc))
            continue
        if hit is not None:
            hits.append(hit)
    return hits


def record_alerts(database: Database, hits: list[AlertHit]) -> int:
    """命中即写 ``ekbs_security_event``（P1/P2），P3 只记日志（`04` §7.7）。"""
    written = 0
    with database.sync_connect() as conn:
        for hit in hits:
            if hit.severity not in {"P1", "P2"}:
                log.warning("alert_p3", **hit.as_dict())
                continue
            import json

            conn.execute(
                "INSERT INTO ekbs_security_event(event_type, user_id, severity, detail) "
                "VALUES ('metric_alert', NULL, ?, ?)",
                (
                    "high" if hit.severity == "P1" else "medium",
                    json.dumps(hit.as_dict(), ensure_ascii=False),
                ),
            )
            written += 1
        conn.commit()
    if hits:
        log.warning("alerts_fired", count=len(hits), p1=sum(1 for h in hits if h.severity == "P1"))
    return written
