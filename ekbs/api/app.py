"""FastAPI 应用装配（`ekbs.main:app` 是 launchd 的入口，见 `01` §8.1）。

路由挂载策略：``smoke.sh`` 同时尝试 ``/api/v1/...`` 与 ``/v1/...``（`01` §14 与 `04` §2.5
的路径前缀口径冲突），因此同一批 router 挂两次，待权威口径定稿后删掉一个即可。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ekbs.api import (
    admin,
    assets,
    auth_routes,
    chunks,
    docs,
    health,
    parse_tasks,
    perm_admin,
    playground,
    qa,
    ui,
)
from ekbs.api.auth import Authenticator
from ekbs.api.docs_zh import install_docs
from ekbs.audit import AuditService
from ekbs.config import Settings, load_settings
from ekbs.errors import ConfigError, EmbeddingFingerprintMismatch, PermissionDenied, SchemaError
from ekbs.logging import get_logger
from ekbs.runtime import Runtime

#: 同时挂载的两套前缀（口径冲突的兼容处理，见模块 docstring）
ROUTE_PREFIXES = ("/api/v1", "/v1")


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = Runtime.create(resolved, service="api")
        app.state.runtime = runtime
        app.state.authenticator = Authenticator(runtime.db, runtime.settings, runtime.secrets)
        app.state.audit = AuditService(runtime.db)
        app.state.probe_cache = {}
        # 供请求路径直接取用（避免每处都 request.app.state.runtime.authenticator）
        runtime.audit = app.state.audit  # type: ignore[attr-defined]
        runtime.authenticator = app.state.authenticator  # type: ignore[attr-defined]
        try:
            yield
        finally:
            await runtime.aclose()

    app = FastAPI(
        title="EKBS API",
        version="0.1.0",
        description="企业知识中枢（部门级 RAG 试点）—— 一期骨架",
        lifespan=lifespan,
        # 关掉框架默认的英文文档页，改由 ekbs/api/docs_zh.py 提供中文版（含界面文案中文化）
        docs_url=None,
        redoc_url=None,
    )

    app.include_router(health.router)
    app.include_router(ui.router)   # 临时测试台 /ui（非 05 规划的管理台）
    for prefix in ROUTE_PREFIXES:
        app.include_router(qa.router, prefix=prefix)
        app.include_router(auth_routes.router, prefix=prefix)
        app.include_router(assets.router, prefix=prefix)
        app.include_router(admin.router, prefix=prefix)
        app.include_router(docs.router, prefix=prefix)
        app.include_router(parse_tasks.router, prefix=prefix)
        app.include_router(playground.router, prefix=prefix)
        app.include_router(chunks.router, prefix=prefix)
        app.include_router(perm_admin.router, prefix=prefix)

    # 必须在 include_router 之后：中文化需要遍历 app.routes 取每个接口的 docstring
    install_docs(app)

    @app.exception_handler(PermissionDenied)
    async def _permission_handler(_request: Request, exc: PermissionDenied) -> JSONResponse:
        return JSONResponse({"detail": "unauthorized"}, status_code=401)

    @app.exception_handler(SchemaError)
    async def _schema_handler(_request: Request, exc: SchemaError) -> JSONResponse:
        return JSONResponse({"detail": f"schema_error: {exc}"}, status_code=503)

    @app.exception_handler(EmbeddingFingerprintMismatch)
    async def _fingerprint_handler(
        _request: Request, exc: EmbeddingFingerprintMismatch
    ) -> JSONResponse:
        return JSONResponse({"detail": f"embedding_fingerprint_mismatch: {exc}"}, status_code=503)

    @app.exception_handler(ConfigError)
    async def _config_handler(_request: Request, exc: ConfigError) -> JSONResponse:
        return JSONResponse({"detail": f"config_error: {exc}"}, status_code=500)

    return app


def runtime_of(request: Request) -> Any:
    """取运行时（测试与路由共用的取用口）。"""
    return request.app.state.runtime


def get_logger_for(component: str) -> Any:
    return get_logger(component=component)
