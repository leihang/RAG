"""``python -m ekbs.stream`` —— 钉钉 Stream 适配器（launchd ``com.ekbs.dingtalk``）。

职责（`04` §1.5）：持有与钉钉的 **Stream 长连接**，把用户消息转成对 ``ekbs-api`` 的回环调用，
再把答案渲染成卡片推回钉钉。它**不做鉴权判定、不算权限指纹** ——
身份与权限一律由 ``ekbs-api`` 服务端计算（这修掉了「调用方声明权限」的信任路径）。

现状（骨架阶段）：

* ``/livez`` / ``/readyz`` 已按 `04` §7.3 实现 —— ``smoke.sh`` 会检查 ``adapter /readyz``；
* Stream 客户端接入（``dingtalk-stream`` SDK）需要企业应用凭据（AppKey/AppSecret/RobotCode），
  拿到之前 ``readyz`` 返回 ``not_ready``，原因写在响应里，**不假装已连接**。

启动：

.. code-block:: bash

    EKBS_CONFIG=config/application.dev.yaml .venv/bin/python -m ekbs.stream
"""

from __future__ import annotations

import argparse
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ekbs.config import Secrets, Settings, load_settings
from ekbs.logging import configure_logging, get_logger

log = get_logger(component="dingtalk")


class StreamState:
    """Stream 连接状态（`04` §7.3：``started_at is not None`` 才算「状态已知」）。

    ⚠️ **不谎报就绪**（`方案/04` §5.3 的 D3 缺陷）：SDK 的 ``start_forever`` 在连不上时
    只 sleep 重试、**永不抛异常**，因此「进程起来了」不等于「Stream 已连上」。
    本实现只在**真的收到过回调**之后才把 ``connected`` 置为 True —— 保守但不会误报；
    代价是启动后无人提问的静默期内 ``/readyz`` 会是 ``not_ready``（宁可假阴，不可假阳）。
    """

    def __init__(self) -> None:
        self.started_at: float | None = None
        self.connected: bool = False
        self.last_error: str = ""
        self.last_message_at: float | None = None

    def mark_started(self) -> None:
        """Stream 任务已拉起（**不等于**已连接）。"""
        import time

        if self.started_at is None:
            self.started_at = time.time()

    def mark_connected(self) -> None:
        """真的收到过回调才置为已连接（见类 docstring 的 D3 说明）。"""
        import time

        from ekbs.observability.metrics import STREAM_CONNECTED, set_gauge

        self.started_at = self.started_at or time.time()
        self.last_message_at = time.time()
        self.connected = True
        self.last_error = ""
        set_gauge(STREAM_CONNECTED, 1)

    def mark_disconnected(self, error: str = "") -> None:
        from ekbs.observability.metrics import STREAM_CONNECTED, set_gauge

        self.connected = False
        self.last_error = error
        set_gauge(STREAM_CONNECTED, 0)


def build_sdk_handler(inner: Any, state: StreamState) -> Any:
    """把 :class:`~ekbs.dingtalk.handler.StreamMessageHandler` 包成 SDK 的回调处理器。

    SDK 契约（`docs/spec/50` §2.1/§2.2）：继承 ``ChatbotHandler`` 并实现
    ``async def process(self, callback) -> (code, message)``；SDK 会把返回的二元组包成
    ``AckMessage`` 经 WebSocket 原路回发。收到回调即证明连接可用 → 顺手标记 connected。
    """
    from dingtalk_stream import ChatbotHandler  # 懒导入：无凭据时不加载 SDK

    class _EkbsChatbotHandler(ChatbotHandler):
        async def process(self, callback: Any) -> tuple[int, str]:
            state.mark_connected()
            return await inner.process(callback)

    return _EkbsChatbotHandler()


def create_stream_task(app: FastAPI, settings: Settings, secrets: Secrets, state: StreamState) -> Any:
    """拉起真正的 Stream 长连接（凭据齐备时才有意义）。"""

    async def _run() -> None:
        from dingtalk_stream import ChatbotMessage, Credential, DingTalkStreamClient

        from ekbs.dingtalk.handler import make_handler
        from ekbs.db import Database
        from ekbs.logging import get_logger

        log = get_logger(component="dingtalk.stream")
        database = Database(settings)
        inner = make_handler(database, settings, secrets)
        credential = Credential(
            secrets.get("DINGTALK_CLIENT_ID"), secrets.get("DINGTALK_CLIENT_SECRET")
        )
        client = DingTalkStreamClient(credential)
        client.register_callback_handler(
            ChatbotMessage.TOPIC, build_sdk_handler(inner, state)
        )
        state.mark_started()
        log.info("stream_starting", topic=ChatbotMessage.TOPIC)
        try:
            # start_forever 内部自带重连（官方 SDK 契约），异常才会冒到这里
            await client.start_forever()
        except Exception as exc:  # pragma: no cover - 需要真实网络
            state.mark_disconnected(f"stream 异常退出：{exc}")
            log.error("stream_exited", error=str(exc))

    return app.state.stream_task_factory(_run)


def create_adapter_app(settings: Settings, secrets: Secrets) -> FastAPI:
    state = StreamState()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        enabled = bool(settings.dingtalk.get("stream_enabled", True))
        missing = [
            key
            for key in ("DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET", "DINGTALK_ROBOT_CODE")
            if not secrets.is_configured(key)
        ]
        if not enabled:
            state.mark_disconnected("stream_enabled=false（01 §7 配置）")
            log.info("stream_disabled")
        elif missing:
            state.mark_disconnected("缺少凭据：" + ", ".join(missing))
            log.warning("stream_credentials_missing", missing=missing)
        else:
            import asyncio

            task = asyncio.create_task(
                create_stream_task(_app, settings, secrets, state)()
            )
            state.mark_started()
            log.info("stream_task_launched", note="连接状态以收到首个回调为准（不谎报就绪）")
        yield

    app = FastAPI(title="EKBS 钉钉适配器", version="0.1.0", lifespan=lifespan)
    app.state.stream_task_factory = lambda coro: coro

    @app.get("/livez")
    async def livez() -> dict[str, Any]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        # 保守判定：只有真的收到过回调才算 ready（见 StreamState 的 D3 说明）
        ready = state.connected and state.started_at is not None
        return JSONResponse(
            {
                "status": "ready" if ready else "not_ready",
                "stream_connected": state.connected,
                "started_at": state.started_at,
                "last_message_at": state.last_message_at,
                "reason": "" if ready else (state.last_error or "尚未收到任何消息回调"),
            },
            status_code=200 if ready else 503,
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "checks": {"process": True}}

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EKBS 钉钉 Stream 适配器")
    parser.parse_args(argv)
    settings = load_settings()
    configure_logging(settings, "dingtalk")
    secrets = Secrets()
    app = create_adapter_app(settings, secrets)
    uvicorn.run(
        app,
        host=settings.adapter.host,
        port=settings.adapter.port,
        log_level="info",
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
