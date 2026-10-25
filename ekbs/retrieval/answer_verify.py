"""回答侧「资料是否足够」核验（生成后置门禁，默认关闭）。

背景：公开基准负例口径下，仅靠 rerank 阈值拒答只有 7.6%；离线实验里
「claim 分解 + 逐条找原文」能把负例拒答提到 ~35–50%，代价是可答误拒 2–3%。
本模块把该核验做成**可配置、默认关闭**的后置门禁，供生产灰度。

设计约束：
* 只对 rerank 分数在 ``[min_score, max_score]`` 的边界题调用，高置信直接答、低分已拒；
* 显式模型名 ``deepseek-flash`` + ``reasoning_effort``，不用旧别名（会随版本漂移）；
* 密钥只从环境变量读；缺 key/超时/解析失败按 ``fail_open`` 放行，门禁本身不制造不可用；
* 只输出 ``supported`` 布尔 + 原因，不拼接用户可读答案。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

import httpx

from ekbs.config import AnswerVerifyCfg
from ekbs.logging import get_logger

log = get_logger(component="answer_verify")

SYSTEM_PROMPT = (
    "你是企业知识库问答的答案核验判官。给你问题、资料片段和候选回答，请做 claim 分解核验。\n"
    "步骤：\n"
    "1) 把候选回答拆成若干条事实性 claim（数字/日期/主体/条款/结论各算一条）；\n"
    "2) 对每条 claim，从资料中找出能逐字支持它的原文；找不到就标 supported=false；\n"
    "3) 只要有一条关键 claim 没有原文依据，整体 supported=false；\n"
    "4) 答非所问/资料不足同样 supported=false；\n"
    "5) 只输出 JSON：{\"supported\": true/false, \"claims\": [{\"claim\": \"...\", "
    "\"supported\": true/false, \"quote\": \"原文\"}], \"reason\": \"一句话\"}。"
)

_RETRYABLE = (429, 500, 502, 503, 504)


def _strip_noise(text: str) -> str:
    """去掉思考段（<think>…）与 markdown 围栏；思考模型可能把推理写进 content。"""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    return re.sub(r"```(?:json)?", "", text)


def parse_verdict(text: str) -> dict[str, Any] | None:
    """从模型输出里解析 ``{"supported": bool, ...}``；尽量容错。"""
    cleaned = _strip_noise(text).strip()
    for candidate in (cleaned,):
        try:
            data = json.loads(candidate)
            if isinstance(data, dict) and isinstance(data.get("supported"), bool):
                return data
        except json.JSONDecodeError:
            pass
    for block in reversed(re.findall(r"\{.*?\}", cleaned, re.S)):
        try:
            data = json.loads(block)
            if isinstance(data, dict) and isinstance(data.get("supported"), bool):
                return data
        except json.JSONDecodeError:
            continue
    m = re.search(r'"?supported"?\s*[:：]\s*(true|false)', cleaned, re.I)
    if m:
        return {"supported": m.group(1).lower() == "true"}
    return None


def _contexts_text(contexts: Sequence[str], limit: int, per: int) -> str:
    parts: list[str] = []
    for i, text in enumerate(contexts[:limit], 1):
        piece = str(text or "").strip()[:per]
        if piece:
            parts.append(f"[资料{i}] {piece}")
    return "\n\n".join(parts) or "（无资料）"


@dataclass
class VerifyResult:
    """核验结果：``supported`` 为 None 表示判官不可用/输出无法解析（由 fail_open 决定）。"""

    supported: bool | None
    reason: str = ""
    claims: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    latency_ms: int = 0
    model: str = ""


class AnswerVerifier:
    """DeepSeek OpenAI 兼容端点的答案核验客户端（同步创建、异步调用）。"""

    def __init__(
        self,
        cfg: AnswerVerifyCfg,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.cfg = cfg
        self._transport = transport

    def _payload(self, question: str, answer: str, contexts: Sequence[str]) -> dict[str, Any]:
        return {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"{_contexts_text(contexts, self.cfg.max_contexts, self.cfg.max_context_chars)}"
                        f"\n\n问题：{question}\n\n候选回答：{answer}"
                    ),
                },
            ],
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            # 思考强度：none=关、low/high/max=开；显式模型名才不会被别名漂移影响
            "reasoning_effort": self.cfg.reasoning_effort,
        }

    async def verify(self, question: str, answer: str, contexts: Sequence[str]) -> VerifyResult:
        api_key = os.environ.get(self.cfg.key_env)
        if not api_key:
            return VerifyResult(supported=None, error="missing_api_key", model=self.cfg.model)
        payload = self._payload(question, answer, contexts)
        url = f"{self.cfg.base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}"}
        started = time.perf_counter()
        last_error = ""
        async with httpx.AsyncClient(timeout=self.cfg.timeout_seconds, transport=self._transport) as client:
            for attempt in range(max(1, self.cfg.max_retries + 1)):
                try:
                    resp = await client.post(url, json=payload, headers=headers)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    await asyncio.sleep(2**attempt)
                    continue
                latency = int((time.perf_counter() - started) * 1000)
                if resp.status_code in _RETRYABLE:
                    last_error = f"HTTP {resp.status_code}"
                    await asyncio.sleep(2**attempt)
                    continue
                if resp.status_code != 200:
                    return VerifyResult(
                        supported=None,
                        error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                        latency_ms=latency,
                        model=self.cfg.model,
                    )
                body = resp.json()
                message = ((body.get("choices") or [{}])[0].get("message") or {})
                data = parse_verdict(message.get("content") or message.get("reasoning_content") or "")
                if data is None:
                    return VerifyResult(
                        supported=None,
                        error="bad_json",
                        latency_ms=latency,
                        model=str(body.get("model") or self.cfg.model),
                    )
                return VerifyResult(
                    supported=bool(data["supported"]),
                    reason=str(data.get("reason") or "")[:300],
                    claims=list(data.get("claims") or []),
                    latency_ms=latency,
                    model=str(body.get("model") or self.cfg.model),
                )
        return VerifyResult(
            supported=None,
            error=last_error or "retry_exhausted",
            latency_ms=int((time.perf_counter() - started) * 1000),
            model=self.cfg.model,
        )
