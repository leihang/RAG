"""健康端点 —— 语义严格区分（`04` §7.3；`deploy/scripts/smoke.sh` 按此断言）。

| 端点 | 查什么 | 是否出网 | 失败返回 |
|---|---|---|---|
| ``/livez`` | 什么都不查，只证明进程活着 | 否 | — |
| ``/healthz`` | SQLite 可读 + data_dir 可写 | 否 | 503 ``degraded`` |
| ``/readyz`` | Schema 自检 + 分片/指纹可加载 | 否 | 503 ``not_ready`` |
| ``/health/deps`` | **真实探测**云端 embed/llm/rerank + 钉钉 Stream + 分片数 | **是** | 503 |

⚠️ ``/health/deps`` **绝不能作为 readiness 探针**：它会真实出网并消耗 token，
把它挂到 k8s/launchd 探针上会造成「模型抖动 → 服务被判死」的连锁故障。

``smoke.sh`` 对 ``/healthz`` 断言 ``status == "ok"``、对 ``/readyz`` 断言 ``status == "ready"``，
对 ``/health/deps`` 断言 ``faiss_shards > 0`` 且 ``embed/llm/rerank/dingtalk_stream/fingerprint_match``
均为 ``true`` —— 本文件保持这些字段名与类型不变。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ekbs.index.fts import ensure_userdict as _ensure_userdict

from ekbs.ingest.chunker import active_token_counter
from ekbs.logging import get_logger

router = APIRouter(tags=["health"])
log = get_logger(component="health")

#: 依赖探测结果的短缓存（避免反复烧 token）；键为 "all"
_PROBE_TTL_SECONDS = 60


@router.get("/livez")
async def livez() -> dict[str, Any]:
    return {"status": "ok"}


@router.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    runtime = request.app.state.runtime
    checks = {
        "db": await runtime.db.ping(),
        "data_dir_writable": runtime.data_dir_writable(),
    }
    healthy = all(checks.values())
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", "checks": checks},
        status_code=200 if healthy else 503,
    )


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    runtime = request.app.state.runtime
    checks = {
        "schema": runtime.schema.ok,
        "shards_registered": runtime.shards.count_active(),
        "embed_fingerprint_match": runtime.fingerprint_match(),
        "providers_configured": runtime.providers.embed is not None
        and runtime.providers.llm is not None,
    }
    ready = bool(checks["schema"] and checks["embed_fingerprint_match"] and checks["providers_configured"])
    return JSONResponse(
        {"status": "ready" if ready else "not_ready", "checks": checks},
        status_code=200 if ready else 503,
    )


@router.get("/health/deps")
async def health_deps(request: Request) -> JSONResponse:
    """真实探测（**会出网**）。**网络探针**缓存 60s 避免探针式调用烧 token。

    ⚠️ 本地计数器（分片数 / 向量数 / 磁盘 / 指纹）**每次现算、不参与缓存**。
    整包一起缓存过（原实现），后果是：软删一份文档后立刻查 `/health/deps`，
    仍然显示删除前的向量数 —— 排查"删除到底摘没摘向量"时被它带偏过一次。
    网络探针才是贵的，本地读几个计数器是免费的，没理由一起缓存。
    """
    runtime = request.app.state.runtime
    cache = request.app.state.probe_cache
    now = time.time()
    cached = cache.get("all")
    probes: dict[str, Any] | None = None
    if cached and now - cached[0] < _PROBE_TTL_SECONDS:
        probes = cached[1]
    if probes is None:
        embed_ok, llm_ok, rerank_ok = await _probe_models(runtime)
        dingtalk_ok = await _probe_adapter(runtime)
        probes = {
            "embed": embed_ok,
            "llm": llm_ok,
            "rerank": rerank_ok,
            "dingtalk_stream": dingtalk_ok,
        }
        cache["all"] = (now, probes)

    payload = {
        # —— 本地计数：每次现算（删/入库后必须立刻反映）
        "faiss_shards": runtime.shards.count_active(),
        "vectors": runtime.shards.total_vectors(),
        "fingerprint_match": runtime.fingerprint_match(),
        "disk_used_pct": runtime.disk_used_pct(),
        "embed_ident": runtime.providers.embed.ref.ident,
        "llm_ident": runtime.providers.llm.ref.ident,
        # 本地 token 计数口径（与 TC-INDEX-004 校准直接相关）：
        # 期望是 `qwen:<path>`；若是 `tiktoken:cl100k_base` 说明词表没就位，
        # 尺寸判定/质量指标会偏（实测 -35%），这里显式暴露以免"悄悄降级"。
        "token_counter": active_token_counter(),
        # FTS 预分词用的 jieba 用户词典（编号/术语加成整词）：缺失时显示 none，
        # 避免"以为加了词典、其实没生效"
        "fts_userdict": fts_userdict_status(),
        # 校准状态：系数与样本数（`02` TC-INDEX-004）。factor 缺失 = 还没校准过或换了模型，
        # 此时 token 指标只是"本地估算"，别当成云端当量。
        "token_calibration": _token_calibration(runtime),
        # —— 网络探针：缓存 60s（会出网、会计费）
        **probes,
    }
    if payload["faiss_shards"] == 0:
        # 无 active 分片是「未建分片」而非「不健康」，给 readiness 之外的明确信号
        payload["note"] = "尚无 active 分片（01 §6.3：先用 ekbs.tools.new_shard 建分片）"
    return JSONResponse(payload, status_code=200 if _deps_healthy(payload) else 503)


def _token_calibration(runtime: Any) -> dict[str, Any] | None:
    """当前嵌入指纹下的校准系数（没校准/换模型后返回 None）。"""
    from ekbs.ingest import token_calib

    calibration = token_calib.load(runtime.settings.data_dir, runtime.providers.embed.ref.fingerprint)
    return calibration.as_dict() if calibration else None


def _deps_healthy(payload: dict[str, Any]) -> bool:
    return bool(
        payload.get("embed")
        and payload.get("llm")
        and payload.get("fingerprint_match")
    )


async def _probe_models(runtime: Any) -> tuple[bool, bool, bool]:
    async def probe_embed() -> bool:
        try:
            await runtime.providers.embed.embed_one("健康检查")
            return True
        except Exception as exc:
            log.warning("probe_embed_failed", error=str(exc))
            return False

    async def probe_llm() -> bool:
        try:
            await runtime.providers.llm.generate(
                [{"role": "user", "content": "ping"}]
            )
            return True
        except Exception as exc:
            log.warning("probe_llm_failed", error=str(exc))
            return False

    async def probe_rerank() -> bool:
        if runtime.providers.rerank is None:
            return True  # 未启用视为「无此项」，不计入失败（00 §2.3 允许关闭 rerank）
        try:
            from ekbs.provider.base import RerankDoc

            await runtime.providers.rerank.rerank(
                "ping", [RerankDoc(chunk_id=1, text="ping")], 1
            )
            return True
        except Exception as exc:
            log.warning("probe_rerank_failed", error=str(exc))
            return False

    return tuple(await asyncio.gather(probe_embed(), probe_llm(), probe_rerank()))  # type: ignore[return-value]


async def _probe_adapter(runtime: Any) -> bool:
    """探测钉钉适配器的 ``/readyz``（回环调用，不是出网）。"""
    if not runtime.settings.dingtalk.get("stream_enabled", True):
        return False
    url = f"http://{runtime.settings.adapter.host}:{runtime.settings.adapter.port}/readyz"
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(url)
        if response.status_code != 200:
            return False
        return str(response.json().get("status")) == "ready"
    except Exception:
        return False


def fts_userdict_status() -> str:
    """jieba 用户词典状态（路径 + 词条数；缺失为 ``none``）。"""
    try:
        return _ensure_userdict()
    except Exception as exc:  # noqa: BLE001
        return f"error:{exc}"
