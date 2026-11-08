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
    ensure_import_dir,
    list_children,
    list_descendant_dirs,
    list_raw_top_dirs,
    raw_root,
    resolve_within_raw,
    scan_directory,
    to_rel,
)
from ekbs.ingest.folder_policy import (
    FolderNotEmpty,
    policy_view,
    remove_import_dir,
    save_folder_policy,
)
from ekbs.permission.dept_tree import attach_dept_codes
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
                # 目录导入页要把「密级」默认成该库的 default_level。**必须在这里给**：
                # 否则界面得另调 `/kb/knowledge-bases`，而那个端点要 `kb:knowledge_base:read`
                # —— 只有 `doc:document:create` 的角色（如 doc_reviewer）会拿不到，
                # 于是密级悄悄回落成 L1，导入的文档比预期更"公开"。
                "default_level": int(r["default_level"] or 1),
            }
            for r in conn.execute(
                "SELECT k.kb_id, k.code, k.name, k.default_level, "
                "       (SELECT COUNT(*) FROM ekbs_document d "
                "        WHERE d.kb_id = k.kb_id AND d.status != 'deleted') AS document_count "
                "FROM ekbs_kb k ORDER BY k.kb_id"
            )
        ]
        from ekbs.personal import attach_personal_owners

        attach_personal_owners(conn, kbs)
        queue = {
            str(r["status"]): int(r["n"])
            for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM ekbs_document GROUP BY status"
            )
        }
    raw_top_dirs = list_raw_top_dirs(
        settings, kb_codes=[str(k["code"]) for k in kbs]
    )
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
            # UI 目标树与 raw 对齐：无对应 kb.code 的顶层目录标 orphan
            "raw_top_dirs": raw_top_dirs,
        }
    )


# ---------------------------------------------------------------------------
# 目录导入
# ---------------------------------------------------------------------------


@router.get("/import/dirs")
async def import_dirs(
    request: Request,
    path: str = Query(default="", description="raw 下的相对路径，空串表示 raw 根"),
    recursive: bool = Query(
        default=False,
        description="true=返回 path 下全部子孙目录（上传页落盘下拉）；false=只列一层（目录浏览）",
    ),
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """浏览 raw 目录。默认只列一层子目录；``recursive=true`` 时扁平列出全部子孙目录。"""
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "doc:document:create")
    runtime = request.app.state.runtime
    try:
        if recursive:
            rels = list_descendant_dirs(runtime.settings, path)
            target = resolve_within_raw(runtime.settings, path)
            base = path.strip("/")
            dirs = []
            for rel_under in rels:
                full = f"{base}/{rel_under}" if base else rel_under
                dirs.append({
                    "name": rel_under.split("/")[-1],
                    "rel_path": full,
                    "rel_under": rel_under,
                    "has_children": False,
                    "importable_files": 0,
                    **policy_view(runtime.settings, full),
                })
            return JSONResponse({
                "rel_path": to_rel(runtime.settings, target),
                "parent": None,
                "recursive": True,
                "dirs": dirs,
                "folder_policy": policy_view(runtime.settings, path),
                "root": str(raw_root(runtime.settings)),
            })
        listing = list_children(runtime.settings, path)
        for entry in listing["dirs"]:
            entry.update(policy_view(runtime.settings, str(entry["rel_path"])))
        listing["folder_policy"] = policy_view(runtime.settings, path)
        return JSONResponse(listing)
    except ImportPathError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class MkdirIn(BaseModel):
    path: str = Field(min_length=1, max_length=400, description="raw 下的相对路径，可多级")
    #: 给出密级就把这份权限记在文件夹上。不给则只建目录，不改已有权限。
    level: int | None = Field(default=None, ge=1, le=4)
    visible_dept_ids: list[str] = Field(default_factory=list)
    viewer_user_ids: list[int] = Field(default_factory=list)


@router.post("/import/dirs")
async def import_mkdir(
    payload: MkdirIn,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """在 raw 下新建目录。不要求里面已经有文件。

    带上密级时，把密级、可见部门和额外可查看的人写进这个文件夹。
    之后传到该文件夹的文件沿用这份设置。
    """
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "doc:document:create")
    runtime = request.app.state.runtime
    try:
        rel, created = ensure_import_dir(runtime.settings, payload.path)
        policy = None
        if payload.level is not None:
            policy = save_folder_policy(
                runtime.settings,
                runtime.db,
                rel,
                level=int(payload.level),
                visible_dept_ids=list(payload.visible_dept_ids),
                viewer_user_ids=list(payload.viewer_user_ids),
            )
    except ImportPathError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    log.info("import_mkdir", rel_path=rel, created=created, level=None if policy is None else policy["level"])
    return JSONResponse({"rel_path": rel, "created": created, "folder_policy": policy})


@router.delete("/import/dirs")
async def import_rmdir(
    request: Request,
    path: str = Query(min_length=1, description="raw 下要删除的相对路径"),
    with_documents: bool = Query(
        default=False,
        description="true=连同该目录下已入库的文档一起删（硬删）；false=只删空文件夹",
    ),
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """删除文件夹。

    * 默认 ``with_documents=false``：只删**空**文件夹（有内容返回 409）；
    * ``with_documents=true``：先把该目录下已入库的文档**硬删**（块/向量/parse/原件），
      再递归删掉目录 —— 解决"UI 删了文件夹、库里文档/目录还在"的问题。

    目录已经不在磁盘上时也返回成功，方便界面去掉残留名字。
    """
    principal = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "doc:document:create"
    )
    runtime = request.app.state.runtime
    documents_deleted = 0
    if with_documents:
        # 延迟导入：docs 路由与 imports 路由互相引用时不至于在 import 期成环
        from ekbs.api.docs import _perform_delete

        target = resolve_within_raw(runtime.settings, path)
        prefix = str(target).rstrip("/") + "/"
        with runtime.db.sync_connect() as conn:
            doc_ids = [
                int(row["doc_id"])
                for row in conn.execute(
                    "SELECT doc_id FROM ekbs_document WHERE source_path LIKE ?",
                    (prefix + "%",),
                )
            ]
        for doc_id in doc_ids:
            try:
                _perform_delete(
                    runtime, doc_id, user_id=principal.user_id,
                    reason="api:dir_delete", hard=True, purge_assets=True,
                )
                documents_deleted += 1
            except Exception as exc:  # noqa: BLE001 - 单篇失败不拖垮整目录删除
                log.warning("dir_delete_doc_failed", doc_id=doc_id, error=str(exc)[:160])
    try:
        rel = remove_import_dir(runtime.settings, path, recursive=with_documents)
    except FolderNotEmpty as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ImportPathError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    log.info("import_rmdir", rel_path=rel, with_documents=with_documents,
             documents_deleted=documents_deleted)
    return JSONResponse(
        {"rel_path": rel, "deleted": True, "documents_deleted": documents_deleted}
    )


@router.get("/import/depts")
async def import_dept_options(
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """上传/导入时选「可见部门」用的组织树。

    权限是 ``doc:document:create``，不是 ``perm:dept:read``：审核员能上传，
    但不能打开组织架构页。返回值里的 ``dingtalk_dept_id`` 就是要写入文档的可见部门 ID。
    """
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "doc:document:create")
    runtime = request.app.state.runtime
    with runtime.db.sync_connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT dept_id, dingtalk_dept_id, name, parent_dept_id "
                "FROM ekbs_dept WHERE deleted_at IS NULL"
            ).fetchall()
        ]
    items = attach_dept_codes(rows, runtime.settings.permission.dept_code_map or {})
    return JSONResponse({
        "items": items,
        "note": "可见部门 ID 用 dingtalk_dept_id；dept_code 是分片部门码，只在配置映射里有的部门才有值。",
    })


@router.get("/import/users")
async def import_user_options(
    request: Request,
    q: str | None = Query(default=None, description="按姓名或钉钉 ID 筛选"),
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """入库时多选「额外可查看的人」。权限与上传相同，不要求用户管理权限。"""
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "doc:document:create")
    runtime = request.app.state.runtime
    where = [
        "u.deleted_at IS NULL",
        "u.status = 'active'",
        "u.dingtalk_userid NOT LIKE 'demo_%'",
    ]
    params: list[Any] = []
    if q and q.strip():
        where.append("(u.nick LIKE ? OR u.dingtalk_userid LIKE ? OR CAST(u.user_id AS TEXT) = ?)")
        needle = f"%{q.strip()}%"
        params.extend([needle, needle, q.strip()])
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT u.user_id, u.nick, u.title, u.clearance, "
            "(SELECT ud.dept_id FROM ekbs_user_dept ud "
            " JOIN ekbs_dept d ON d.dept_id = ud.dept_id AND d.deleted_at IS NULL "
            " WHERE ud.user_id = u.user_id "
            " ORDER BY ud.is_primary DESC, ud.dept_id LIMIT 1) AS dept_id, "
            "(SELECT d.name FROM ekbs_user_dept ud "
            " JOIN ekbs_dept d ON d.dept_id = ud.dept_id AND d.deleted_at IS NULL "
            " WHERE ud.user_id = u.user_id "
            " ORDER BY ud.is_primary DESC, ud.dept_id LIMIT 1) AS dept_name "
            f"FROM ekbs_user u WHERE {' AND '.join(where)} "
            "ORDER BY u.nick LIMIT 2000",
            tuple(params),
        ).fetchall()
    return JSONResponse({
        "items": [
            {
                "user_id": int(row["user_id"]),
                "nick": str(row["nick"] or ""),
                "title": str(row["title"] or ""),
                "clearance": int(row["clearance"] or 1),
                "dept_id": int(row["dept_id"]) if row["dept_id"] is not None else None,
                "dept_name": str(row["dept_name"] or ""),
            }
            for row in rows
        ]
    })


class ScanIn(BaseModel):
    path: str = ""
    level: int = Field(default=2, ge=1, le=4)
    #: 可见部门。L2/L3 必填 —— 扫描阶段就要能把「部门没给」报成 blocker，
    #: 而不是等 worker 一条条失败（界面那时只显示"上传成功"）
    dept_ids: list[str] = Field(default_factory=list)
    recursive: bool = True
    limit: int | None = Field(default=None, ge=1, le=100000)


def _permission_for_dir(
    settings: Any,
    path: str,
    level: int,
    dept_ids: list[str],
    viewer_ids: list[int],
) -> tuple[int, list[str], list[int]]:
    """目录上已经写过权限时，扫描和导入都沿用文件夹，不再用表单上的另一套。

    目标是个人库时，表单带来的可见范围是 ``user:<用户>``。这时不套用
    当前目录上的部门，避免把别人的文件夹权限写进私有库。
    """
    from ekbs.personal import PERSONAL_LEVEL, sole_personal_owner

    try:
        if sole_personal_owner([str(item) for item in dept_ids]) is not None:
            kept = [str(item).strip() for item in dept_ids if str(item).strip()]
            return PERSONAL_LEVEL, kept, []
    except ConfigError:
        pass
    view = policy_view(settings, path or "")
    if view["level"] is None:
        return int(level), list(dept_ids), list(viewer_ids)
    return int(view["level"]), list(view["visible_dept_ids"]), list(view["viewer_user_ids"])


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
    level, dept_ids, _viewers = _permission_for_dir(
        runtime.settings, payload.path, payload.level, list(payload.dept_ids), [],
    )
    try:
        result = scan_directory(
            runtime.settings,
            runtime.db,
            rel_dir=payload.path,
            level=level,
            dept_ids=dept_ids,
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
    #: 额外可查看的人（用户编号，可多选）。这一批登记的每份文件都写上这些人。
    viewer_user_ids: list[int] = Field(default_factory=list)
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
    level, dept_ids, viewers = _permission_for_dir(
        settings, payload.path, payload.level, list(payload.dept_ids), list(payload.viewer_user_ids),
    )

    if payload.kb_id is None and not payload.kb_code:
        raise HTTPException(status_code=422, detail="需要给出 kb_id 或 kb_code")

    kb_id = payload.kb_id
    if kb_id is None:
        # ``ekbs.tools.import`` 的模块名是关键字，**不能**写 ``from ekbs.tools.import import``
        # （本仓库踩过一次：那条 import 语句是语法错误）。用 importlib 取。
        kb_id = _import_tool.ensure_kb(runtime, str(payload.kb_code), level=level)
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
                rel_dir=payload.path, level=level, dept_ids=dept_ids,
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
            level=level,
            dept_ids=dept_ids,
            created_by=principal.user_id,
            viewer_user_ids=viewers,
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
            "hint": "解析由 ekbs-worker 异步进行；进度见「解析任务」页（status: pending → parsing → review，确认后才 published）",
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
