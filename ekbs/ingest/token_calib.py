"""token 计数校准（`02` TC-INDEX-004 的落地）。

## 为什么需要"校准"而不是"换个词表就完了"

本地 token 计数只是云端 `usage.total_tokens` 的**代理**。换同源词表能把偏差从 −35% 收到
−11%，但**收不到 ≤5%**：偏差随文本类型摆动（中文散文 +9%、数字/表格行 −42%，
换 Qwen 词表后变成 −30% / −10%），**乘一个固定系数也不行**。唯一能把残差压进 5% 的办法是
拿**云端真值**去校正 —— 而云端只返回整批 `usage.total_tokens`、**不给逐条**，
所以校准只能做到**按文档**：每篇入库时记一条「云端真值 / 本地估算」，聚合成系数。

## 存什么、放哪

* `var/index/token_calibration.jsonl` —— 每篇文档一行原始记录（append-only，便于追溯/回归）；
* `var/index/token_calibration.json` —— 聚合后的**当前系数**，绑定 `embedding_fingerprint`。

**绑定指纹**是关键：换嵌入模型 → 指纹变 → 旧系数自动失效（`load()` 查不到就返回 None），
避免"用上个模型校准出来的系数去算这个模型的 token"。

## 系数怎么用

`factor = Σcloud / Σlocal`（>1 表示本地偏低）。`calibrated(local) = round(local * factor)`。
**注意**：把它套到分块阈值上会改变块的绝对大小，所以口径切换必须与"重定阈值 + 重灌"
一起做（详见 README 发现 84/86）；本模块只负责"记录与换算"，不偷偷改 `count_tokens`。
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from ekbs.logging import get_logger

log = get_logger(component="ingest.token_calib")

#: 少于这么多样本就不写系数：单篇文档的文本类型可能很偏，拿它去改全局口径会把别的文档带偏
MIN_SAMPLES = 3
RECORDS_NAME = "token_calibration.jsonl"
FACTOR_NAME = "token_calibration.json"


def records_path(data_dir: pathlib.Path | str) -> pathlib.Path:
    return pathlib.Path(data_dir) / "index" / RECORDS_NAME


def factor_path(data_dir: pathlib.Path | str) -> pathlib.Path:
    return pathlib.Path(data_dir) / "index" / FACTOR_NAME


@dataclass
class Calibration:
    """某个嵌入模型指纹下的校准系数。"""

    fingerprint: str
    factor: float
    samples: int
    cloud_tokens: int
    local_tokens: int
    #: 逐篇比例的中位数（与 factor 对照看）：factor 是按量加权，**会被最大的那篇主导**
    #: （实测：一篇 18.8 万 token 的 xlsx 把系数拉到 1.0002，而另一篇真实比例是 1.073）
    median_ratio: float = 1.0
    #: **计数口径标识**（`active_token_counter()`，如 `qwen:sdk:<path>`）。
    #: 为什么也要绑：换词表口径（SDK 词表 / HF 词表 / tiktoken 回退）会改变 local_tokens，
    #: 只绑嵌入指纹的话旧系数会**静默沿用**，校准就名不副实了。
    counter: str = ""
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def deviation_pct(self) -> float:
        """校准前本地相对云端的偏差（负数=本地偏低）。"""
        if not self.cloud_tokens:
            return 0.0
        return (self.local_tokens - self.cloud_tokens) / self.cloud_tokens * 100

    def calibrated(self, local_tokens: int) -> int:
        return int(round(local_tokens * self.factor))

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["deviation_pct_before"] = round(self.deviation_pct, 2)
        return payload


def record(
    data_dir: pathlib.Path | str,
    *,
    doc_id: int,
    fingerprint: str,
    cloud_tokens: int,
    local_tokens: int,
    counter: str = "",
) -> dict[str, Any] | None:
    """记一条文档级校准记录（append-only）。``cloud_tokens<=0`` 或 ``local_tokens<=0`` 时跳过。"""
    if cloud_tokens <= 0 or local_tokens <= 0:
        return None
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "doc_id": int(doc_id),
        "fingerprint": fingerprint,
        "counter": counter,
        "cloud_tokens": int(cloud_tokens),
        "local_tokens": int(local_tokens),
        "ratio": round(cloud_tokens / local_tokens, 4),
    }
    path = records_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    log.info(
        "token_calibration_record",
        doc_id=int(doc_id),
        cloud_tokens=int(cloud_tokens),
        local_tokens=int(local_tokens),
        ratio=entry["ratio"],
    )
    return entry


def summarize(
    data_dir: pathlib.Path | str,
    fingerprint: str | None = None,
    *,
    counter: str | None = None,
    min_samples: int = MIN_SAMPLES,
) -> Calibration | None:
    """从记录聚合出系数（**只统计同一嵌入指纹 + 同一计数口径**；样本不足返回 None）。"""
    path = records_path(data_dir)
    if not path.is_file():
        return None
    cloud = local = samples = 0
    ratios: list[float] = []
    matched_fingerprint = fingerprint or ""
    matched_counter = counter or ""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if fingerprint and entry.get("fingerprint") != fingerprint:
            continue
        if counter and str(entry.get("counter") or "") != counter:
            continue
        if not fingerprint:
            matched_fingerprint = str(entry.get("fingerprint") or "")
        if not counter:
            matched_counter = str(entry.get("counter") or "")
        cloud += int(entry.get("cloud_tokens") or 0)
        local += int(entry.get("local_tokens") or 0)
        ratio = entry.get("ratio")
        if isinstance(ratio, (int, float)) and ratio:
            ratios.append(float(ratio))
        samples += 1
    if samples < min_samples or local <= 0 or cloud <= 0:
        return None
    import statistics

    return Calibration(
        fingerprint=matched_fingerprint,
        factor=round(cloud / local, 4),
        samples=samples,
        cloud_tokens=cloud,
        local_tokens=local,
        median_ratio=round(statistics.median(ratios), 4) if ratios else 1.0,
        counter=matched_counter,
    )


def write_factor(data_dir: pathlib.Path | str, calibration: Calibration) -> pathlib.Path:
    path = factor_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(calibration.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def load(
    data_dir: pathlib.Path | str,
    fingerprint: str | None = None,
    counter: str | None = None,
) -> Calibration | None:
    """读当前系数；**指纹或计数口径不符**一律返回 None —— 旧系数不得复用。"""
    path = factor_path(data_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    stored = str(payload.get("fingerprint") or "")
    if fingerprint and stored and stored != fingerprint:
        return None
    stored_counter = str(payload.get("counter") or "")
    # 与 summarize **同一口径**：要求某个计数口径时，"没有口径字段"的旧系数一律失效
    # （无法证明它是在当前口径下算出来的；系数目前只用于展示与工具报告，宁严勿混）
    if counter and stored_counter != counter:
        return None
    return Calibration(
        fingerprint=stored,
        factor=float(payload.get("factor") or 1.0),
        samples=int(payload.get("samples") or 0),
        cloud_tokens=int(payload.get("cloud_tokens") or 0),
        local_tokens=int(payload.get("local_tokens") or 0),
        median_ratio=float(payload.get("median_ratio") or payload.get("factor") or 1.0),
        updated_at=str(payload.get("updated_at") or ""),
        counter=stored_counter,
    )


def calibrated_tokens(
    local_tokens: int,
    data_dir: pathlib.Path | str,
    fingerprint: str | None = None,
    counter: str | None = None,
) -> int:
    """把本地计数换算成「云端当量」（无系数时原样返回 —— 绝不假装校准过）。"""
    calibration = load(data_dir, fingerprint, counter)
    return calibration.calibrated(local_tokens) if calibration else int(local_tokens)
