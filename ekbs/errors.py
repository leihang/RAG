"""EKBS 异常层次。

约定（对应 `00` §2.3 降级矩阵）：异常类型本身就是降级决策的输入，因此按「能不能降级」
分类，而不是按模块分类：

* :class:`ConfigError` / :class:`EmbeddingFingerprintMismatch` —— **拒绝启动**；
* :class:`EmbeddingUnavailable` —— 查询期**明确拒答**（禁止降级为「只走 FTS5 假装语义检索」）；
* :class:`ModelUnavailable` —— rerank/LLM 可降级，由调用方按 `00` §2.3 处理；
* :class:`PermissionDenied` —— 越权路径，必须落 `ekbs_security_event`。
"""

from __future__ import annotations


class EkbsError(Exception):
    """所有 EKBS 异常的基类。"""


class ConfigError(EkbsError):
    """配置不合法（含：application.yaml 里出现密钥类键名）。"""


class SchemaError(EkbsError):
    """Schema 未初始化、表数不符或库损坏。"""


class EmbeddingFingerprintMismatch(EkbsError):
    """嵌入指纹不一致 —— 按 `00` §2.3「拒绝启动」，防混用不同模型的向量。"""


class ModelUnavailable(EkbsError):
    """云端模型调用失败（可重试或按降级矩阵处理）。"""


class EmbeddingUnavailable(ModelUnavailable):
    """嵌入不可用。查询期必须拒答；入库期排队重试（`00` §2.3）。"""


class RerankUnavailable(ModelUnavailable):
    """rerank 不可用 → 跳过精排，直接取 RRF Top-5（`00` §2.3）。"""


class PermissionDenied(EkbsError):
    """权限不足。C5 闸门丢弃 chunk 时用它，并由调用方决定是否记安全事件。"""


class Refused(EkbsError):
    """拒答：rerank 最高分低于阈值，或上下文无有效内容（`00` §2.1）。"""


class ComplianceGateClosed(EkbsError):
    """合规闸未开（`permission.corpus_egress_ack=false`）：禁止真实语料做云端嵌入（`00` §7）。"""
