"""进程内指标登记表（`04` §7.7）—— 无 Prometheus 依赖。

设计要点（逐条照文档）：

* 计数与仪表量放在**本进程的字典**里，由 ``collect_metrics`` 作业每 60s 读一次快照写 ``ekbs_metric``；
* 用一把锁保护：适配器是单进程但**多协程**，asyncio 下 dict 的读改写不是原子的；
* 快照语义与 Prometheus 一致：**计数器读后清零**（每次采集是一个增量区间），
  **仪表量保留**（它们是「当前值」，清零会让告警失去意义）；
* 直方图用有界样本估 p95，快照时产出 ``<name>_p95``（仪表量）与 ``<name>_count``（计数器）。

指标名沿用 v2.0 命名（`04` §7.7 的表格），将来真接 Prometheus 时可由同一张登记表渲染
``/metrics``，不必改名。
"""

from __future__ import annotations

import math
import threading
from collections import defaultdict, deque
from typing import Final

_LOCK = threading.Lock()
_COUNTERS: dict[str, float] = defaultdict(float)
_GAUGES: dict[str, float] = {}
_SAMPLES: dict[str, deque[float]] = {}
_LABELS: dict[str, dict[str, str]] = {}

#: 直方图保留的样本数上限（够估 p95，且不会无限增长）
SAMPLE_WINDOW: Final = 1000


def _key(name: str, labels: dict[str, str]) -> str:
    return name + "|" + ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


def inc(name: str, value: float = 1.0, **labels: str) -> None:
    key = _key(name, labels)
    with _LOCK:
        _COUNTERS[key] += value
        _LABELS[key] = labels


def set_gauge(name: str, value: float, **labels: str) -> None:
    key = _key(name, labels)
    with _LOCK:
        _GAUGES[key] = value
        _LABELS[key] = labels


def observe(name: str, value: float, **labels: str) -> None:
    """直方图打点（保留有界样本，快照时算 p95）。"""
    key = _key(name, labels)
    with _LOCK:
        bucket = _SAMPLES.get(key)
        if bucket is None:
            bucket = deque(maxlen=SAMPLE_WINDOW)
            _SAMPLES[key] = bucket
        bucket.append(float(value))
        _LABELS[key] = labels


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[rank]


def snapshot() -> list[tuple[str, float, dict[str, str]]]:
    """供 ``collect_metrics`` 读取：计数器读后清零，仪表量与直方图保留。"""
    with _LOCK:
        out: list[tuple[str, float, dict[str, str]]] = []
        for key, value in _COUNTERS.items():
            out.append((key.split("|", 1)[0], value, _LABELS.get(key, {})))
        for key, value in _GAUGES.items():
            out.append((key.split("|", 1)[0], value, _LABELS.get(key, {})))
        for key, bucket in _SAMPLES.items():
            name = key.split("|", 1)[0]
            labels = _LABELS.get(key, {})
            if bucket:
                out.append((f"{name}_p95", _percentile(list(bucket), 0.95), labels))
                out.append((f"{name}_count", float(len(bucket)), labels))
        _COUNTERS.clear()
        return out


def reset() -> None:
    """清空全部进程内指标（测试与优雅重启用）。"""
    with _LOCK:
        _COUNTERS.clear()
        _GAUGES.clear()
        _SAMPLES.clear()
        _LABELS.clear()


def current(name: str, **labels: str) -> float:
    """读当前值（counter / gauge / 直方图样本数），供自检与测试。"""
    key = _key(name, labels)
    with _LOCK:
        if key in _GAUGES:
            return float(_GAUGES[key])
        if key in _COUNTERS:
            return float(_COUNTERS[key])
        if key in _SAMPLES:
            return float(len(_SAMPLES[key]))
    return 0.0


# ---------- 指标名常量（与 `04` §7.7 阈值表一一对应）----------
STREAM_CONNECTED: Final = "ekbs_dingtalk_stream_connected"
RECONNECT_TOTAL: Final = "ekbs_dingtalk_reconnect_total"
MESSAGE_RECEIVED: Final = "ekbs_dingtalk_message_received_total"
MESSAGE_PROCESSED: Final = "ekbs_dingtalk_message_processed_total"
MESSAGE_FAILED: Final = "ekbs_dingtalk_message_failed_total"
MESSAGE_LATENCY: Final = "ekbs_dingtalk_message_latency_seconds"
FIRST_TOKEN: Final = "ekbs_dingtalk_first_token_seconds"
CARD_SEND: Final = "ekbs_dingtalk_card_send_total"
CARD_STREAMING: Final = "ekbs_dingtalk_card_streaming_total"
RATELIMIT_HIT: Final = "ekbs_dingtalk_ratelimit_hit_total"
AUTH_LOGIN: Final = "ekbs_dingtalk_auth_login_total"
AUTH_REPLAY: Final = "ekbs_dingtalk_auth_replay_total"
IDEMPOTENT_HIT: Final = "ekbs_dingtalk_idempotent_hit_total"
TOKEN_REFRESH_FAILED: Final = "ekbs_dingtalk_token_refresh_failed_total"
TOKEN_EXPIRE: Final = "ekbs_dingtalk_token_expire_seconds"
ORG_SYNC_DURATION: Final = "ekbs_dingtalk_org_sync_duration_seconds"
ORG_SYNC_FAILED: Final = "ekbs_dingtalk_org_sync_failed_total"
PERM_REVOKE_LAG: Final = "ekbs_dingtalk_perm_revoke_lag_seconds"

#: 应用自身的指标（文档表格之外，但同一张登记表渲染）
QA_TOTAL: Final = "ekbs_qa_total"
RETRIEVAL_LATENCY: Final = "ekbs_retrieval_latency_seconds"
C5_DENIED: Final = "ekbs_c5_denied_total"
