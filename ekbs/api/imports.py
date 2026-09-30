"""「导入」接口：服务器目录导入 + 浏览器上传（管理台 `/admin` 的导入页后端）。

设计要点
--------
* **目录导入只扫描 + 登记**，解析交给常驻 worker（``status='pending'`` 入队）。
  网页请求不该挂几十分钟，进度与重跑直接复用已有的「解析任务」页。
* **路径只能在 ``<data_dir>/raw`` 之内**（含中文多级子目录），``..`` 与指向外部的
  软链接一律拒绝 —— 否则这个接口等于把「读服务器任意文件」开放给任何有
  ``doc:document:create`` 的账号。判定在 :func:`ekbs.ingest.dir_import.resolve_within_raw`。
* 上传接口沿用 `/docs/upload/*` 的三段式（init → chunk → complete），本模块不再造一套；
  这里只补一个**上传准入前置说明**接口，让界面能提前告诉用户「哪些格式能传」，
  而不是让用户挑完文件才吃 422。
"""

from __future__ import annotations

import importlib
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ekbs.api.auth import INTERNAL_TOKEN_HEADER, INTERNAL_USER_HEADER
from ekbs.api.qa import _authenticate
from ekbs.errors import ConfigError, PermissionDenied
from ekbs.ingest.dir_import import (
    ENQUEUE_LIMIT,
    ImportPathError,
    enqueue_files,
    list_children,
    raw_root,
    scan_directory,
)
from ekbs.ingest.parser import SUFFIX_ENGINES
from ekbs.logging import get_logger

log = get_logger(component="api.import")

#: CLI 工具模块（模块名含 Python 关键字 ``import``，只能动态取）
_import_tool = importlib.import_module("ekbs.tools.import")

router = APIRouter(tags=["import"])


def _h(authorization: str | None, token: str | None, user_id: str | None) -> dict[str, str | None]:
    return {
        "authorization": authorization,
        INTERNAL_TOKEN_HEADER.lower(): token,
        INTERNAL_USER_HEADER.lower(): user_id,
    }


def _principal(request: Request, headers: dict[str, str | None], code: str) -> Any:
    runtime = request.app.state.runtime
    user = _authenticate(request, headers)
    try:
        return runtime.authz.require(user.user_id, code)
    except PermissionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 元信息：白名单与队列现状（界面首屏用）
# ---------------------------------------------------------------------------


@router.get("/import/meta")
async def import_meta(
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """导入页首屏要用的元信息：raw 根、目录导入支持的格式、上传准入格式、各知识库。"""
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "doc:document:create")
    runtime = request.app.state.runtime
    settings = runtime.settings
    configured = {
        str(e).lower().lstrip(".")
        for e in (settings.limits or {}).get("file_ext_whitelist", [])
    }
    parseable = {s.lstrip(".") for s in SUFFIX_ENGINES}
    with runtime.db.sync_connect() as conn:
        kbs = [
            {
                "kb_id": int(r["kb_id"]),
                "code": str(r["code"]),
                "name": str(r["name"]),
                # 界面要显示「XX库（N 篇）」，顺手带上省一次请求
                "document_count": int(r["document_count"] or 0),
            }
            for r in conn.execute(
                "SELECT k.kb_id, k.code, k.name, "
                "       (SELECT COUNT(*) FROM ekbs_document d WHERE d.kb_id = k.kb_id) AS document_count "
                "FROM ekbs_kb k ORDER BY k.kb_id"
            )
        ]
        queue = {
            str(r["status"]): int(r["n"])
            for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM ekbs_document GROUP BY status"
            )
        }
    return JSONResponse(
        {
            "raw_root": str(raw_root(settings)),
            # 目录导入走 worker，与 CLI 同口径：63 种全放行
            "dir_extensions": sorted(parseable),
            # 浏览器上传走 /docs/upload，受配置白名单约束（当前 11 种）
            "upload_extensions": sorted(configured & parseable),
            "upload_blocked": sorted(configured - parseable),
            "max_file_size_mb": float((settings.limits or {}).get("max_file_size_mb", 100) or 100),
            # 上传行为（方案 05 §6.5）：界面按这些默认值初始化，可临时改
            "upload": {
                "chunk_size_mb": int(getattr(settings.upload, "chunk_size_mb", 8) or 8),
                "concurrent": int(getattr(settings.upload, "concurrent", 3) or 3),
                "max_retry": int(getattr(settings.upload, "max_retry", 3) or 3),
            },
            "enqueue_limit": ENQUEUE_LIMIT,
            "knowledge_bases": kbs,
            "queue": queue,
        }
    )


# ---------------------------------------------------------------------------
# 目录导入
# ---------------------------------------------------------------------------


@router.get("/import/dirs")
async def import_dirs(
    request: Request,
    path: str = Query(default="", description="raw 下的相对路径，空串表示 raw 根"),
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """逐层浏览 raw 目录（只列子目录；文件走扫描预览）。"""
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "doc:document:create")
    runtime = request.app.state.runtime
    try:
        return JSONResponse(list_children(runtime.settings, path))
    except ImportPathError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class ScanIn(BaseModel):
    path: str = ""
    level: int = Field(default=2, ge=1, le=4)
    #: 可见部门。L2/L3 必填 —— 扫描阶段就要能把「部门没给」报成 blocker，
    #: 而不是等 worker 一条条失败（界面那时只显示"上传成功"）
    dept_ids: list[str] = Field(default_factory=list)
    recursive: bool = True
    limit: int | None = Field(default=None, ge=1, le=100000)


@router.post("/import/scan")
async def import_scan(
    payload: ScanIn,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """预览：这个目录会导入哪些文件、哪些是重复、有没有闸门挡着。**不写库**。"""
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "doc:document:create")
    runtime = request.app.state.runtime
    try:
        result = scan_directory(
            runtime.settings,
            runtime.db,
            rel_dir=payload.path,
            level=payload.level,
            dept_ids=list(payload.dept_ids),
            recursive=payload.recursive,
            limit=payload.limit,
        )
    except ImportPathError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(result.as_dict())


class RunIn(BaseModel):
    path: str = ""
    kb_code: str | None = None
    kb_id: int | None = None
    level: int = Field(default=2, ge=1, le=4)
    dept_ids: list[str] = Field(default_factory=list)
    recursive: bool = True
    limit: int | None = Field(default=None, ge=1, le=100000)
    #: 只导入扫描结果里明确列出的这些相对路径（界面勾选）；为空则按目录全量
    files: list[str] = Field(default_factory=list)


@router.post("/import/run")
async def import_run(
    payload: RunIn,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """把文件登记为待解析文档（``status='pending'``），由 worker 解析入库。

    返回逐条结果：已入队 / 跳过（重复）/ 失败（越界、不存在、格式不支持）。
    """
    principal = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "doc:document:create"
    )
    runtime = request.app.state.runtime
    settings = runtime.settings

    if payload.kb_id is None and not payload.kb_code:
        raise HTTPException(status_code=422, detail="需要给出 kb_id 或 kb_code")

    kb_id = payload.kb_id
    if kb_id is None:
        # ``ekbs.tools.import`` 的模块名是关键字，**不能**写 ``from ekbs.tools.import import``
        # （本仓库踩过一次：那条 import 语句是语法错误）。用 importlib 取。
        kb_id = _import_tool.ensure_kb(runtime, str(payload.kb_code), level=payload.level)
    else:
        with runtime.db.sync_connect() as conn:
            row = conn.execute("SELECT kb_id FROM ekbs_kb WHERE kb_id = ?", (int(kb_id),)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"知识库不存在：{kb_id}")

    # 勾选模式：只导选中的；否则按目录**全量**扫描（跳过重复/超限）。
    # 注意必须用 importable_paths()（全量），不能用响应里的 preview（可能被截断）。
    if payload.files:
        rel_paths = list(payload.files)
    else:
        try:
            scan = scan_directory(
                settings, runtime.db,
                rel_dir=payload.path, level=payload.level, dept_ids=list(payload.dept_ids),
                recursive=payload.recursive, limit=payload.limit,
            )
        except ImportPathError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        rel_paths = scan.importable_paths()
        if scan.blockers:
            raise HTTPException(status_code=409, detail="；".join(scan.blockers))

    try:
        outcome = enqueue_files(
            settings,
            runtime.db,
            rel_paths=rel_paths,
            kb_id=int(kb_id),
            level=payload.level,
            dept_ids=list(payload.dept_ids),
            created_by=principal.user_id,
        )
    except ConfigError as exc:
        # 闸门类错误整批失败，且要把「为什么」原样带回界面
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ImportPathError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    log.info(
        "import_run_done",
        kb_id=int(kb_id), level=payload.level,
        enqueued=len(outcome["enqueued"]),
        skipped=len(outcome["skipped"]),
        failed=len(outcome["failed"]),
        operator=principal.user_id,
    )
    return JSONResponse(
        {
            **outcome,
            "kb_id": int(kb_id),
            "level": payload.level,
            "queued": len(outcome["enqueued"]),
            "hint": "解析由 ekbs-worker 异步进行；进度见「解析任务」页（status: pending → parsing → published）",
        }
    )


@router.get("/import/queue")
async def import_queue(
    request: Request,
    limit: int = Query(default=20, ge=1, le=200),
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """导入后的即时反馈：各状态计数 + 最近登记的一批文档（不必跳页就能看到动静）。"""
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "doc:document:create")
    runtime = request.app.state.runtime
    with runtime.db.sync_connect() as conn:
        counts = {
            str(r["status"]): int(r["n"])
            for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM ekbs_document GROUP BY status"
            )
        }
        recent = [
            {
                "doc_id": int(r["doc_id"]),
                "title": str(r["title"]),
                "status": str(r["status"]),
                "level": int(r["level"]),
                "source_path": str(r["source_path"]),
                "updated_at": str(r["updated_at"]),
            }
            for r in conn.execute(
                "SELECT doc_id, title, status, level, source_path, updated_at "
                "FROM ekbs_document ORDER BY doc_id DESC LIMIT ?",
                (int(limit),),
            )
        ]
    return JSONResponse({"counts": counts, "recent": recent})
