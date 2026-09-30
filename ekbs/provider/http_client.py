"""云端 HTTP 客户端基础设施：错误映射 + 带抖动的指数退避重试。

依据 `docs/spec/40-cloud-models.md` §5（已核实的确切错误码与官方退避建议）：

* **可重试**：HTTP ``429`` / ``500`` / ``503``，以及 ``Throttling*``、``RequestTimeOut``、
  ``InternalError*``、``SystemError``、``ModelServiceFailed``、``ServiceUnavailable``、
  ``ModelUnavailable``。
  官方明确建议「不限制发送速率，仅在收到 429 或 5xx 时触发**带随机抖动**的指数退避」，
  因为固定间隔会让失败请求同时重发、再次触发限流。
* **不可重试**：``InvalidParameter`` / ``InvalidApiKey`` / ``Arrearage`` / ``DataInspectionFailed`` /
  ``model_not_supported`` 等 —— 重试只是浪费配额，必须立刻失败并把原因带出去。
* **两条协议的错误体不同**：DashScope 原生在**顶层** ``code``/``message``；
  OpenAI 兼容是嵌套 ``{"error": {...}}``。本模块统一归一化成 :class:`CloudError`。

退避参数取官方建议：base 1s、指数 ×2、上限 60s、最多 5~6 次（本项目按配置的 ``max_retries``）。
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

import httpx

from ekbs.errors import EmbeddingUnavailable, ModelUnavailable, RerankUnavailable
from ekbs.logging import get_logger

log = get_logger(component="cloud")

#: 可重试的 HTTP 状态码
RETRYABLE_STATUS = {429, 500, 503}

#: 可重试的错误码前缀/全名（已核实的确切字符串，见 docs/spec/40 §5.5）
RETRYABLE_CODES = (
    "Throttling",
    "Throttling.RateQuota",
    "Throttling.AllocationQuota",
    "Throttling.BurstRate",
    "Throttling.Concurrency",
    "Throttling.ServiceOverloaded",
    "Throttling.ResourceExhausted",
    "RequestTimeOut",
    "InternalError",
    "SystemError",
    "ModelServiceFailed",
    "ServiceUnavailable",
    "ModelUnavailable",
)

#: 明确的不可重试错误码（命中后直接失败，并把原因写清楚）
FATAL_CODES = {
    "InvalidApiKey": "API Key 非法（含地域不匹配）",
    "invalid_api_key": "API Key 非法（含地域不匹配）",
    "Arrearage": "账号欠费",
    "isv.OUT_OF_SERVICE": "账号余额不足，服务被暂停",
    "DataInspectionFailed": "内容审核拦截（输入或输出命中敏感内容）",
    "data_inspection_failed": "内容审核拦截（输入或输出命中敏感内容）",
    "model_not_supported": "该模型不支持 OpenAI 兼容模式，请改用 DashScope 原生接口",
    "ModelNotFound": "模型不存在",
    "model_not_found": "模型不存在",
    "AllocationQuota.FreeTierOnly": "免费额度已用尽",
}


@dataclass
class CloudError(Exception):
    """归一化后的云端错误（两条协议统一成这一种）。"""

    provider: str
    endpoint: str
    status_code: int
    code: str = ""
    message: str = ""
    retryable: bool = False
    request_id: str = ""

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        parts = [f"{self.provider} {self.endpoint} HTTP {self.status_code}"]
        if self.code:
            parts.append(f"code={self.code}")
        if self.message:
            parts.append(f"message={self.message[:200]}")
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        return " | ".join(parts)


def parse_error(provider: str, endpoint: str, response: httpx.Response) -> CloudError:
    """把两种协议的响应体解析成统一的 :class:`CloudError`。"""
    status = response.status_code
    code = ""
    message = ""
    request_id = ""
    try:
        body = response.json()
    except Exception:
        body = {}
    if isinstance(body, dict):
        if isinstance(body.get("error"), dict):  # OpenAI 兼容协议
            error = body["error"]
            code = str(error.get("code") or error.get("type") or "")
            message = str(error.get("message") or "")
        else:  # DashScope 原生协议
            code = str(body.get("code") or "")
            message = str(body.get("message") or "")
        request_id = str(body.get("request_id") or body.get("id") or "")
    if not message:
        message = (response.text or "")[:300]

    retryable = status in RETRYABLE_STATUS or code.startswith(RETRYABLE_CODES)
    if code in FATAL_CODES:
        retryable = False
        message = f"{message}（{FATAL_CODES[code]}）"
    return CloudError(
        provider=provider,
        endpoint=endpoint,
        status_code=status,
        code=code,
        message=message,
        retryable=retryable,
        request_id=request_id,
    )


def to_provider_error(error: CloudError, kind: str) -> ModelUnavailable:
    """按能力类型转成项目内的异常（降级矩阵据此决策，见 `00` §2.3）。"""
    detail = str(error)
    if kind == "embed":
        return EmbeddingUnavailable(f"嵌入不可用：{detail}")
    if kind == "rerank":
        return RerankUnavailable(f"rerank 不可用：{detail}")
    return ModelUnavailable(f"生成不可用：{detail}")


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    provider: str,
    endpoint: str,
    kind: str,
    max_retries: int = 2,
    json_body: dict | None = None,
    headers: dict[str, str] | None = None,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
) -> httpx.Response:
    """发请求并在可重试错误上做「指数退避 + 随机抖动」。

    成功（2xx）直接返回响应；不可重试错误立即抛 :class:`CloudError`；
    重试耗尽后抛最后一次的 :class:`CloudError`。
    """
    attempt = 0
    last_error: CloudError | None = None
    while attempt <= max_retries:
        try:
            response = await client.request(method, url, json=json_body, headers=headers)
        except httpx.HTTPError as exc:
            # 网络层错误（连接失败/超时）：可重试
            last_error = CloudError(
                provider=provider, endpoint=endpoint, status_code=0,
                code="ConnectionError", message=str(exc), retryable=True,
            )
        else:
            if response.status_code < 400:
                return response
            last_error = parse_error(provider, endpoint, response)
            if not last_error.retryable:
                log.error("cloud_fatal", error=str(last_error))
                raise last_error

        attempt += 1
        if attempt > max_retries:
            break
        # 指数退避 + 抖动（官方建议：固定间隔会让所有失败请求同时重发，再次触发限流）
        delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
        delay *= random.uniform(0.5, 1.5)
        log.warning(
            "cloud_retry", error=str(last_error), attempt=attempt, delay_s=round(delay, 3)
        )
        await asyncio.sleep(delay)

    assert last_error is not None
    raise last_error


def resolve_base_url(base_url: str | None, default: str = "https://dashscope.aliyuncs.com") -> str:
    """规范化 base_url（去掉尾部斜杠）。

    `docs/spec/40` §1.2 给出两种形态：
    ``https://dashscope.aliyuncs.com``（共享域名）与
    ``https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com``（工作空间专属）；
    两者路径一致，因此默认用共享域名，需要时在配置里覆盖。
    """
    return (base_url or default).rstrip("/")
