"""文档**写**接口 —— 路径与权限点严格取自 `05` §8.2 的全量清单。

这是二期里**唯一会改动线上检索结果**的部分，因此三条纪律比读接口更硬：

1. **改密级/可见部门必须走 :class:`~ekbs.permission.changes.PermissionChangeService`** ——
   只改 ``ekbs_document`` 而不迁 ``ekbs_chunk.shard_id``，会让低密级用户继续从旧分片召回它
   （`03` §6.4；我在第 2 轮修过这个漏洞）。本模块**不允许**直接 UPDATE 这两个字段。
2. **下线/删除必须让取消生效是即时的**：改状态 + ``policy_version`` 自增（指纹变化使旧缓存失效）。
3. **身份与权限一律服务端判定**（`04` §1.5）：不接受请求体里声明操作人或可见范围。

| 端点 | 权限点（`05` §8.2） |
|---|---|
| ``POST /api/v1/docs/documents`` | ``doc:document:create`` |
| ``PUT /api/v1/docs/documents/{doc_id}`` | ``doc:document:update`` |
| ``DELETE /api/v1/docs/documents/{doc_id}`` | ``doc:document:delete`` |
| ``POST /api/v1/docs/documents/{doc_id}/publish`` | ``doc:document:publish`` |
| ``POST /api/v1/docs/documents/{doc_id}/offline``（兼容 ``/unpublish``） | ``doc:document:offline`` |
| ``POST /api/v1/docs/documents/batch`` | 按动作：publish / offline / update |
| ``POST /api/v1/docs/upload/init`` ``/chunk`` ``/complete`` | ``doc:document:create`` |
| ``POST /api/v1/system/index/rebuild`` | ``sys:index:rebuild`` |

⚠️ `05` 内部冲突：§8.1 汇总表写 ``/unpublish``，§8.2 全量清单写 ``/offline``。
本实现以 **§8.2（全量清单）** 为准，同时把 ``/unpublish`` 挂成别名（已记入 README 发现清单）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ekbs.api.auth import INTERNAL_TOKEN_HEADER, INTERNAL_USER_HEADER
from ekbs.api.qa import _authenticate
from ekbs.errors import ConfigError, PermissionDenied, SchemaError
from ekbs.personal import (
    PERSONAL_LEVEL,
    assert_personal_kb_storage,
    personal_user_id_from_code,
    scope_ref,
)
from ekbs.ingest.dir_import import ImportPathError, assert_path_within_raw
from ekbs.ingest.parser import SUFFIX_ENGINES, normalize_suffix
from ekbs.ingest.retrievable import RETRIEVABLE_PREDICATE
from ekbs.logging import get_logger
from ekbs.permission.changes import PermissionChangeService
from ekbs.permission.router import bump_policy_version
from ekbs.permission.upload_scope import check_document_scope
from ekbs.permission.viewers import assert_viewers_assignable, grant_document_viewers

router = APIRouter(tags=["docs"])
log = get_logger(component="api.docs")

UPLOAD_CACHE_PREFIX = "ekbs:upload:"
UPLOAD_TTL_SECONDS = 3600

#: 允许的扩展名/MIME 以配置为准（`01` §7 的 limits / security 段）
DEFAULT_EXTS = {"pdf", "docx", "doc", "pptx", "ppt", "xlsx", "xls", "txt", "md", "html", "png", "jpg", "jpeg"}


def _runtime(request: Request) -> Any:
    return request.app.state.runtime


def _principal(
    request: Request, headers: dict[str, str | None], code: str
) -> tuple[Any, Any]:
    """返回 ``(user, principal)``；401 未登录 / 403 权限不足 分开。"""
    runtime = _runtime(request)
    user = _authenticate(request, headers)
    try:
        principal = runtime.authz.require(user.user_id, code)
    except PermissionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return user, principal


def _headers(
    authorization: str | None, internal_token: str | None, internal_user_id: str | None
) -> dict[str, str | None]:
    return {
        "authorization": authorization,
        INTERNAL_TOKEN_HEADER.lower(): internal_token,
        INTERNAL_USER_HEADER.lower(): internal_user_id,
    }


def _load_document(runtime: Any, doc_id: int) -> Any:
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT * FROM ekbs_document WHERE doc_id = ?", (doc_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return row


# ---------------------------------------------------------------------------
# 元数据登记 / 编辑
# ---------------------------------------------------------------------------


class DocumentCreate(BaseModel):
    title: str = Field(min_length=1)
    kb_id: int | None = None
    kb_code: str | None = None
    level: int = Field(default=2, ge=1, le=4)
    visible_dept_ids: list[str] = Field(default_factory=list)
    #: 额外可查看的人（用户编号，可多选）。不改变可见部门。
    viewer_user_ids: list[int] = Field(default_factory=list)
    source_path: str = ""
    doc_version: str = "v1"
    effective_date: str | None = None
    expire_date: str | None = None
    checksum: str | None = None


@router.post("/docs/documents")
async def create_document(
    payload: DocumentCreate,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """登记文档元数据（``status='pending'``，随后由 ``ekbs-worker`` 解析入库）。"""
    user, _ = _principal(
        request, _headers(authorization, x_internal_token, x_internal_user_id),
        "doc:document:create",
    )
    runtime = _runtime(request)
    settings = runtime.settings
    # 闸门：L3/L4 与合规（与入库路径同口径）
    try:
        settings.assert_l3_ingest_allowed(payload.level)
        if payload.level == 4 and not settings.permission.l4_enabled:
            raise ConfigError("L4 不入库：permission.l4_enabled=false（00 D11）")
        settings.assert_compliance_gate(real_corpus=False)  # 登记阶段不含正文
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 上架范围（部门 × 密级）：登记同样受限 —— 否则可以先登记再让 worker 入库绕过。
    scope = check_document_scope(
        runtime.db,
        user_id=user.user_id,
        level=int(payload.level),
        visible_dept_ids=list(payload.visible_dept_ids),
    )
    if not scope.allowed:
        raise HTTPException(status_code=403, detail=f"无权登记该文档：{scope.detail}")

    kb_id = payload.kb_id
    if kb_id is None and payload.kb_code:
        with runtime.db.sync_connect() as conn:
            row = conn.execute("SELECT kb_id FROM ekbs_kb WHERE code = ?", (payload.kb_code,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"知识库不存在：{payload.kb_code}")
        kb_id = int(row["kb_id"])
    if kb_id is None:
        raise HTTPException(status_code=422, detail="必须给出 kb_id 或 kb_code")

    # source_path 红线：必须落在 data_dir/raw 内的真实文件。空路径 / 逃逸路径会让
    # worker 解析任意本机文件进知识库（与 dir_import 同等级别的越权）。
    raw_path = (payload.source_path or "").strip()
    if not raw_path:
        raise HTTPException(
            status_code=422,
            detail="必须提供 raw 目录内的 source_path，或改走 /docs/upload",
        )
    try:
        resolved = assert_path_within_raw(settings, raw_path)
    except ImportPathError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not resolved.is_file():
        raise HTTPException(status_code=422, detail=f"文件不存在：{resolved}")
    source_path = str(resolved)

    size_bytes = resolved.stat().st_size
    with runtime.db.sync_connect() as conn:
        try:
            assert_personal_kb_storage(
                conn, settings, kb_id=int(kb_id), incoming=size_bytes
            )
            viewers = assert_viewers_assignable(
                conn, kb_id=int(kb_id), viewer_user_ids=payload.viewer_user_ids
            )
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, visible_dept_ids, "
            "status, checksum, size_bytes, doc_version, effective_date, expire_date, created_by) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
            (
                kb_id, payload.title, source_path, payload.level,
                json.dumps(payload.visible_dept_ids, ensure_ascii=False), payload.checksum,
                size_bytes, payload.doc_version, payload.effective_date, payload.expire_date,
                user.user_id,
            ),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        grant_document_viewers(
            conn, doc_id=doc_id, viewer_user_ids=viewers,
            operator_id=user.user_id, reason="document:viewers",
        )
        conn.commit()
    log.info("document_registered", doc_id=doc_id, operator=user.user_id, level=payload.level)
    return JSONResponse({"doc_id": doc_id, "status": "pending"}, status_code=201)


class DocumentUpdate(BaseModel):
    title: str | None = None
    level: int | None = Field(default=None, ge=1, le=4)
    visible_dept_ids: list[str] | None = None
    effective_date: str | None = None
    expire_date: str | None = None
    doc_version: str | None = None


@router.put("/docs/documents/{doc_id}")
async def update_document(
    doc_id: int,
    payload: DocumentUpdate,
    request: Request,
    reason: str = Query(default="", description="变更原因（写入审计）"),
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """编辑元数据。**密级/可见部门变更走权限变更服务**（含分片迁移 + 审计 + 缓存失效）。"""
    user, _ = _principal(
        request, _headers(authorization, x_internal_token, x_internal_user_id),
        "doc:document:update",
    )
    runtime = _runtime(request)
    doc = _load_document(runtime, doc_id)
    changes: list[dict[str, Any]] = []
    service = PermissionChangeService(
        db=runtime.db, settings=runtime.settings, providers=runtime.providers,
        shards=runtime.shards, fts=runtime.fts, cache=runtime.cache,
    )
    try:
        if payload.level is not None and int(payload.level) != int(doc["level"]):
            result = await service.change_document_level(
                doc_id, int(payload.level), operator_id=user.user_id, reason=reason or "api:update"
            )
            changes.append(result.as_dict())
        if payload.visible_dept_ids is not None:
            current = json.loads(doc["visible_dept_ids"] or "[]")
            if sorted(payload.visible_dept_ids) != sorted(current):
                result = await service.change_document_depts(
                    doc_id, payload.visible_dept_ids,
                    operator_id=user.user_id, reason=reason or "api:update",
                )
                changes.append(result.as_dict())
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 其余字段是普通元数据，直接更新（不触发迁移，但仍要写审计）
    plain_fields = {
        "title": payload.title,
        "effective_date": payload.effective_date,
        "expire_date": payload.expire_date,
        "doc_version": payload.doc_version,
    }
    plain = {k: v for k, v in plain_fields.items() if v is not None}
    if plain:
        assignments = ", ".join(f"{key} = ?" for key in plain)
        with runtime.db.sync_connect() as conn:
            conn.execute(
                f"UPDATE ekbs_document SET {assignments}, updated_at = datetime('now') "
                "WHERE doc_id = ?",
                (*plain.values(), doc_id),
            )
            conn.execute(
                "INSERT INTO ekbs_perm_change_log(operator_id, object_type, object_id, "
                "change_type, field, old_value, new_value, reason) "
                "VALUES (?, 'document', ?, 'metadata', ?, ?, ?, ?)",
                (
                    user.user_id, str(doc_id), ",".join(plain.keys()),
                    "", json.dumps(plain, ensure_ascii=False), reason or "api:update",
                ),
            )
            version = bump_policy_version(conn, reason=f"api:update:doc={doc_id}")
            conn.commit()
        changes.append({"change_type": "metadata", "fields": sorted(plain), "policy_version": version})

    return JSONResponse({"doc_id": doc_id, "changes": changes})


# ---------------------------------------------------------------------------
# 发布 / 下线
# ---------------------------------------------------------------------------


class StatusChange(BaseModel):
    reason: str = ""


@router.post("/docs/documents/{doc_id}/publish")
async def publish_document(
    doc_id: int,
    request: Request,
    payload: StatusChange | None = None,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """发布：``status='published'``（**索引生效**，此后参与召回）。"""
    user, _ = _principal(
        request, _headers(authorization, x_internal_token, x_internal_user_id),
        "doc:document:publish",
    )
    runtime = _runtime(request)
    _load_document(runtime, doc_id)
    with runtime.db.sync_connect() as conn:
        chunks = int(
            conn.execute(
                # 用「可检索块」而不是 child：代码文件切出来的是符号块（parent），
                # 写死 child 会让纯代码文档永远卡在"尚无分块"而发布不了
                f"SELECT COUNT(*) FROM ekbs_chunk c WHERE c.doc_id = ? AND {RETRIEVABLE_PREDICATE}",
                (doc_id,),
            ).fetchone()[0]
        )
    if chunks == 0:
        raise HTTPException(
            status_code=409,
            detail="文档尚无分块（解析/索引未完成），不能发布；请先让 ekbs-worker 处理",
        )
    _set_status(runtime, doc_id, "published", operator_id=user.user_id,
                reason=(payload.reason if payload else "") or "api:publish")
    return JSONResponse({"doc_id": doc_id, "status": "published", "child_chunks": chunks})


@router.post("/docs/documents/{doc_id}/offline")
@router.post("/docs/documents/{doc_id}/unpublish")  # 05 §8.1 的旧写法，保留兼容
async def offline_document(
    doc_id: int,
    request: Request,
    payload: StatusChange | None = None,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """下线：``status='offline'`` + ``policy_version`` 自增。

    下线必须**立刻**生效：检索两侧都带 ``d.status='published'`` 过滤，
    因此改状态即停召回；自增是为了让指纹失效、清掉可能已缓存的相关结果。
    """
    user, _ = _principal(
        request, _headers(authorization, x_internal_token, x_internal_user_id),
        "doc:document:offline",
    )
    runtime = _runtime(request)
    _load_document(runtime, doc_id)
    _set_status(runtime, doc_id, "offline", operator_id=user.user_id,
                reason=(payload.reason if payload else "") or "api:offline")
    return JSONResponse({"doc_id": doc_id, "status": "offline"})


def _set_status(runtime: Any, doc_id: int, status: str, *, operator_id: int, reason: str) -> None:
    with runtime.db.sync_connect() as conn:
        old = conn.execute(
            "SELECT status FROM ekbs_document WHERE doc_id = ?", (doc_id,)
        ).fetchone()["status"]
        conn.execute(
            "UPDATE ekbs_document SET status = ?, updated_at = datetime('now') WHERE doc_id = ?",
            (status, doc_id),
        )
        conn.execute(
            "INSERT INTO ekbs_perm_change_log(operator_id, object_type, object_id, change_type, "
            "field, old_value, new_value, reason) "
            "VALUES (?, 'document', ?, 'status', 'status', ?, ?, ?)",
            (operator_id, str(doc_id), str(old), status, reason),
        )
        version = bump_policy_version(conn, reason=f"api:status:{doc_id}->{status}")
        conn.commit()
    runtime.shards  # 索引不因状态变化而改动（召回侧按 status 过滤）
    log.info("document_status_changed", doc_id=doc_id, status=status, policy_version=version)


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------


@router.delete("/docs/documents/{doc_id}")
async def delete_document(
    doc_id: int,
    request: Request,
    reason: str = Query(default=""),
    hard: bool = Query(default=False, description="true=物理删除行与资产；默认软删除进回收站"),
    purge_assets: bool = Query(
        default=True,
        description="仅 hard=true 时生效：是否同时删除磁盘原件与解析产物",
    ),
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """删除文档。默认**软删除**（摘索引、清解析图与图注；原件从 ``raw`` 迁入 ``trash``，保留 30 天）；
    ``hard=true`` 才物理删行；``purge_assets`` 控制硬删时是否连原件一并清。"""
    user, _ = _principal(
        request, _headers(authorization, x_internal_token, x_internal_user_id),
        "doc:document:delete",
    )
    return JSONResponse(
        _perform_delete(
            _runtime(request), doc_id, user_id=user.user_id, reason=reason or "api:delete",
            hard=hard, purge_assets=purge_assets,
        )
    )


@router.post("/docs/documents/{doc_id}/restore")
async def restore_document(
    doc_id: int,
    request: Request,
    reason: str = Query(default=""),
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """从回收站恢复：``deleted → offline``；原件从 ``trash/`` 迁回 ``raw/``。
    索引已在软删时摘掉，须 **reparse** 才能再发布。"""
    user, _ = _principal(
        request, _headers(authorization, x_internal_token, x_internal_user_id),
        "doc:document:update",
    )
    runtime = _runtime(request)
    row = _load_document(runtime, doc_id)
    if str(row["status"]) != "deleted":
        raise HTTPException(status_code=409, detail=f"仅 deleted 可恢复，当前 status={row['status']}")
    restored_path = _restore_raw_from_trash(runtime, row, doc_id)
    with runtime.db.sync_connect() as conn:
        if restored_path and restored_path != str(row["source_path"] or ""):
            conn.execute(
                "UPDATE ekbs_document SET source_path = ? WHERE doc_id = ?",
                (restored_path, doc_id),
            )
        conn.execute(
            "UPDATE ekbs_document SET status = 'offline', deleted_at = NULL, "
            "updated_at = datetime('now') WHERE doc_id = ?",
            (doc_id,),
        )
        conn.execute(
            "INSERT INTO ekbs_perm_change_log(operator_id, object_type, object_id, change_type, "
            "field, old_value, new_value, reason) "
            "VALUES (?, 'document', ?, 'restore', 'status', 'deleted', 'offline', ?)",
            (user.user_id, str(doc_id), reason or "api:restore"),
        )
        version = bump_policy_version(conn, reason=f"api:restore:doc={doc_id}")
        conn.commit()
    log.info("document_restored", doc_id=doc_id, operator=user.user_id, policy_version=version)
    return JSONResponse({
        "doc_id": doc_id,
        "status": "offline",
        "policy_version": version,
        "source_path": restored_path or row["source_path"],
        "hint": "已恢复为 offline（原件已迁回 raw）；需 reparse 后才能再次发布进索引",
    })


def _collect_chunk_image_paths(runtime: Any, doc_id: int) -> list[str]:
    """摘索引前先记下 figure 块的 ``image_path``（相对 data_dir），供清图注缓存。"""
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT image_path FROM ekbs_chunk "
            "WHERE doc_id = ? AND image_path IS NOT NULL AND TRIM(image_path) != ''",
            (doc_id,),
        ).fetchall()
    return [str(r["image_path"]) for r in rows]


def _figure_stems_from_image_paths(paths: list[str]) -> set[str]:
    """``parse/.../images/img-<sha8>.png`` → ``img-<sha8>``（与图注缓存文件名对齐）。"""
    stems: set[str] = set()
    for rel in paths:
        stem = Path(rel).stem
        if stem.startswith("img-"):
            stems.add(stem)
    return stems


def _figure_stems_under_parse_dir(parse_dir: Path) -> set[str]:
    """扫解析目录里的图片文件名（chunk 已删或 CLI 入库走 sha- 目录时的兜底）。"""
    stems: set[str] = set()
    images = parse_dir / "images"
    if not images.is_dir():
        return stems
    for item in images.iterdir():
        if item.is_file() and item.stem.startswith("img-"):
            stems.add(item.stem)
    return stems


def _purge_figure_caption_cache(runtime: Any, stems: set[str], *, doc_id: int) -> list[str]:
    """图注是文档内容的一部分：文档失效则缓存一并清，不跨文档留用。"""
    from ekbs.ingest.figure_caption import caption_cache_dir

    removed: list[str] = []
    cache_dir = caption_cache_dir(runtime.settings.data_dir)
    for stem in sorted(stems):
        path = cache_dir / f"{stem}.json"
        if path.is_file():
            path.unlink()
            removed.append(str(path))
    if removed:
        log.info(
            "document_caption_cache_removed",
            doc_id=doc_id,
            count=len(removed),
        )
    return removed


def _under_root(path: Path, root: Path) -> Path | None:
    """若 ``path`` 落在 ``root`` 内返回相对路径，否则 ``None``。"""
    try:
        return path.resolve().relative_to(root.resolve())
    except Exception:
        return None


def _cleanup_empty_parents(start: Path, *, stop_at: Path) -> None:
    cur = start
    stop = stop_at.resolve()
    while True:
        try:
            cur.resolve().relative_to(stop)
        except ValueError:
            break
        if cur.resolve() == stop or not cur.is_dir():
            break
        try:
            next(cur.iterdir())
            break
        except StopIteration:
            try:
                cur.rmdir()
            except OSError:
                break
            cur = cur.parent
        except OSError:
            break


def _unlink_if_under_allowed(
    runtime: Any,
    source: str,
    *,
    doc_id: int,
    allowed: tuple[str, ...] = ("raw", "trash"),
) -> str | None:
    """仅当文件落在允许的 data 子目录内才 unlink，返回被删路径。"""
    if not source:
        return None
    paths = runtime.settings.data_paths()
    try:
        resolved = Path(source).resolve()
    except Exception:
        log.warning("delete_asset_unresolvable", doc_id=doc_id, path=source)
        return None
    for name in allowed:
        root = paths[name]
        if _under_root(resolved, root) is None:
            continue
        if resolved.is_file():
            resolved.unlink()
            log.info("document_asset_removed", doc_id=doc_id, path=str(resolved), kind=name)
            if name == "trash":
                _cleanup_empty_parents(resolved.parent, stop_at=root.resolve())
            return str(resolved)
        return None
    log.warning("delete_asset_outside_allowed", doc_id=doc_id, path=source, allowed=list(allowed))
    return None


def _quarantine_raw_to_trash(runtime: Any, row: Any, doc_id: int) -> tuple[str | None, list[str]]:
    """软删：把 ``raw`` 下原件挪到 ``trash/<doc_id>/…``，返回 (新路径, 变动路径列表)。"""
    moved: list[str] = []
    source = str(row["source_path"] or "").strip()
    if not source:
        return None, moved
    paths = runtime.settings.data_paths()
    raw_root = paths["raw"]
    trash_root = paths["trash"]
    trash_root.mkdir(parents=True, exist_ok=True)
    try:
        resolved = Path(source).resolve()
    except Exception:
        log.warning("quarantine_unresolvable", doc_id=doc_id, path=source)
        return None, moved

    if _under_root(resolved, trash_root) is not None:
        return str(resolved), moved

    rel = _under_root(resolved, raw_root)
    if rel is None:
        log.warning("quarantine_outside_raw", doc_id=doc_id, path=source)
        return None, moved
    if not resolved.is_file():
        log.warning("quarantine_missing_file", doc_id=doc_id, path=source)
        return None, moved

    dest = trash_root / str(doc_id) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    shutil.move(str(resolved), str(dest))
    moved.append(str(dest))
    log.info(
        "document_raw_quarantined",
        doc_id=doc_id,
        from_path=str(resolved),
        to_path=str(dest),
    )
    return str(dest), moved


def _restore_raw_from_trash(runtime: Any, row: Any, doc_id: int) -> str | None:
    """恢复：把 ``trash/<doc_id>/…`` 迁回 ``raw/…``，返回新 ``source_path``。"""
    source = str(row["source_path"] or "").strip()
    if not source:
        return None
    paths = runtime.settings.data_paths()
    raw_root = paths["raw"]
    trash_root = paths["trash"]
    try:
        resolved = Path(source).resolve()
    except Exception:
        log.warning("restore_unresolvable", doc_id=doc_id, path=source)
        return None

    under_trash = _under_root(resolved, trash_root)
    if under_trash is None:
        if _under_root(resolved, raw_root) is not None and resolved.is_file():
            return str(resolved)
        return None
    if not resolved.is_file():
        return None

    parts = under_trash.parts
    if parts and parts[0] == str(doc_id):
        rel = Path(*parts[1:]) if len(parts) > 1 else Path(resolved.name)
    else:
        rel = under_trash
    dest = raw_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.resolve() != resolved:
        dest = dest.with_name(f"{dest.stem}.restored{dest.suffix}")
    shutil.move(str(resolved), str(dest))
    _cleanup_empty_parents(resolved.parent, stop_at=trash_root.resolve())
    log.info(
        "document_raw_restored",
        doc_id=doc_id,
        from_path=str(resolved),
        to_path=str(dest),
    )
    return str(dest)


def _purge_document_assets(
    runtime: Any,
    row: Any,
    doc_id: int,
    *,
    image_paths: list[str] | None = None,
    purge_raw: bool = True,
) -> list[str]:
    """删掉这份文档**在磁盘上的解析产物、图注缓存，以及（可选）原件**，返回被删路径。

    图片与图注是文档内容生成的，文档删/失效时必须一起清 —— 不为「同图再入库省
    一次 vision 费」跨文档留缓存。

    解析目录两种约定都清：``parse/<doc_id>/``（worker）与
    ``parse/sha-<checksum前16>/``（CLI import）。

    ⚠️ **越界保护**：``source_path`` 只有确认落在 ``data_dir/raw`` 或
    ``data_dir/trash`` **之内**才 unlink。
    """
    removed: list[str] = []
    paths = runtime.settings.data_paths()
    parse_root = paths["parse"]
    stems = _figure_stems_from_image_paths(image_paths or [])

    parse_dirs: list[Path] = [parse_root / str(doc_id)]
    checksum = str(row["checksum"] or "").strip()
    if checksum:
        parse_dirs.append(parse_root / f"sha-{checksum[:16]}")

    for parse_dir in parse_dirs:
        if not parse_dir.is_dir():
            continue
        stems |= _figure_stems_under_parse_dir(parse_dir)
        shutil.rmtree(parse_dir, ignore_errors=True)
        removed.append(str(parse_dir))
        log.info("document_assets_removed", doc_id=doc_id, path=str(parse_dir), kind="parse")

    removed.extend(_purge_figure_caption_cache(runtime, stems, doc_id=doc_id))

    if purge_raw:
        gone = _unlink_if_under_allowed(
            runtime, str(row["source_path"] or ""), doc_id=doc_id, allowed=("raw", "trash"),
        )
        if gone:
            removed.append(gone)
    return removed


def _drop_document_index(runtime: Any, doc_id: int) -> tuple[list[int], list[str]]:
    """从 FAISS / FTS / ekbs_chunk 摘掉该文档的全部索引，返回 (chunk_ids, shard_ids)。"""
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT chunk_id, shard_id FROM ekbs_chunk WHERE doc_id = ?", (doc_id,)
        ).fetchall()
    chunk_ids = [int(r["chunk_id"]) for r in rows]
    by_shard: dict[str, list[int]] = {}
    for row in rows:
        by_shard.setdefault(str(row["shard_id"]), []).append(int(row["chunk_id"]))
    shards = sorted(by_shard)
    if chunk_ids:
        runtime.fts.delete_chunk_ids(chunk_ids)
        for shard_id, ids_in_shard in by_shard.items():
            removed = runtime.shards.remove(shard_id, ids_in_shard)
            log.info("document_vectors_removed", doc_id=doc_id, shard_id=shard_id, removed=removed)
        if getattr(runtime, "figure_index", None) is not None:
            runtime.figure_index.remove(chunk_ids)
    with runtime.db.sync_connect() as conn:
        conn.execute("DELETE FROM ekbs_chunk WHERE doc_id = ?", (doc_id,))
        conn.commit()
    return chunk_ids, shards


def _perform_delete(
    runtime: Any,
    doc_id: int,
    *,
    user_id: int,
    reason: str,
    hard: bool = False,
    purge_assets: bool = True,
) -> dict[str, Any]:
    """删除一篇文档的**核心逻辑**（单篇路由与批量路由共用同一份）。

    * **软删除（默认）**：立刻摘 FAISS/FTS/chunk，文档行 ``status='deleted'`` +
      ``deleted_at``；**解析图与图注缓存立刻清**；``raw`` 原件迁入 ``trash/<doc_id>/``
      保留 30 天供恢复。
    * **硬删除（``hard=true``）**：摘索引 + 删文档行；``purge_assets`` 控制是否连
      原件（raw 或 trash）/解析产物/图注一并删。

    索引是 ``IndexIDMap2``（标签 = ``chunk_id``），因此摘向量是真删，不留孤儿。
    """
    doc_row = _load_document(runtime, doc_id)
    if str(doc_row["status"]) == "deleted" and not hard:
        raise HTTPException(status_code=409, detail="文档已在回收站；物理清除请传 hard=true")

    # 必须在删 chunk 之前记下 image_path，否则图注缓存找不到对应 stem
    image_paths = _collect_chunk_image_paths(runtime, doc_id)
    chunk_ids, shards = _drop_document_index(runtime, doc_id)
    assets_removed: list[str] = []

    if hard:
        if purge_assets:
            assets_removed = _purge_document_assets(
                runtime, doc_row, doc_id, image_paths=image_paths, purge_raw=True,
            )
        with runtime.db.sync_connect() as conn:
            conn.execute("DELETE FROM ekbs_document WHERE doc_id = ?", (doc_id,))
            conn.execute(
                "INSERT INTO ekbs_perm_change_log(operator_id, object_type, object_id, change_type, "
                "field, old_value, new_value, reason) "
                "VALUES (?, 'document', ?, 'delete', 'deleted', 'present', 'absent', ?)",
                (user_id, str(doc_id), reason or "api:hard_delete"),
            )
            version = bump_policy_version(conn, reason=f"api:hard_delete:doc={doc_id}")
            conn.commit()
        mode = "hard"
        hint = (
            "已物理删除文档行"
            + ("与磁盘资产" if purge_assets else "（磁盘原件保留）")
        )
    else:
        # 软删：原件迁入 trash/；图片/图注是文档内容，文档已失效则立刻清
        assets_removed = _purge_document_assets(
            runtime, doc_row, doc_id, image_paths=image_paths, purge_raw=False,
        )
        new_source, quarantined = _quarantine_raw_to_trash(runtime, doc_row, doc_id)
        assets_removed.extend(quarantined)
        with runtime.db.sync_connect() as conn:
            if new_source and new_source != str(doc_row["source_path"] or ""):
                conn.execute(
                    "UPDATE ekbs_document SET source_path = ? WHERE doc_id = ?",
                    (new_source, doc_id),
                )
            conn.execute(
                "UPDATE ekbs_document SET status = 'deleted', deleted_at = datetime('now'), "
                "updated_at = datetime('now') WHERE doc_id = ?",
                (doc_id,),
            )
            conn.execute(
                "INSERT INTO ekbs_perm_change_log(operator_id, object_type, object_id, change_type, "
                "field, old_value, new_value, reason) "
                "VALUES (?, 'document', ?, 'soft_delete', 'status', ?, 'deleted', ?)",
                (user_id, str(doc_id), str(doc_row["status"]), reason or "api:soft_delete"),
            )
            version = bump_policy_version(conn, reason=f"api:soft_delete:doc={doc_id}")
            conn.commit()
        mode = "soft"
        hint = (
            "已软删除：索引与解析图/图注已清；原件已迁入 trash/ 保留 30 天；"
            "可用 /restore 迁回 raw 并恢复为 offline 后 reparse"
        )

    log.warning(
        "document_deleted",
        doc_id=doc_id,
        mode=mode,
        chunks=len(chunk_ids),
        shards=shards,
        assets_removed=len(assets_removed),
        purge_assets=purge_assets if hard else True,
    )
    return {
        "doc_id": doc_id,
        "mode": mode,
        "status": "absent" if hard else "deleted",
        "deleted_chunks": len(chunk_ids),
        "affected_shards": shards,
        "policy_version": version,
        "assets_removed": assets_removed,
        "hint": hint,
    }


TRASH_RETENTION_DAYS = 30


def purge_expired_trash(runtime: Any, *, retention_days: int = TRASH_RETENTION_DAYS) -> dict[str, Any]:
    """物理清理回收站中超过保留期的文档（scheduler 日作业调用）。"""
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT doc_id FROM ekbs_document WHERE status = 'deleted' "
            "AND deleted_at IS NOT NULL "
            "AND deleted_at <= datetime('now', ?)",
            (f"-{int(retention_days)} days",),
        ).fetchall()
    purged: list[int] = []
    errors: list[dict[str, str]] = []
    for row in rows:
        doc_id = int(row["doc_id"])
        try:
            _perform_delete(
                runtime, doc_id, user_id=0, reason="scheduler:trash_purge",
                hard=True, purge_assets=True,
            )
            purged.append(doc_id)
        except Exception as exc:  # pragma: no cover - 单条失败不阻断整批
            log.error("trash_purge_failed", doc_id=doc_id, error=str(exc))
            errors.append({"doc_id": str(doc_id), "error": str(exc)})
    return {"purged": purged, "errors": errors, "retention_days": retention_days}


# ---------------------------------------------------------------------------
# 批量
# ---------------------------------------------------------------------------


class BatchRequest(BaseModel):
    doc_ids: list[int] = Field(min_length=1, max_length=200)
    action: str = Field(description="publish | offline | update | delete")
    level: int | None = Field(default=None, ge=1, le=4)
    visible_dept_ids: list[str] | None = None
    reason: str = ""
    #: 仅对 ``action='delete'``：是否物理删除（默认软删进回收站）
    hard: bool = False
    #: 仅 ``hard=true`` 时有意义：是否连磁盘原件与解析产物一起删（默认删）
    purge_assets: bool = True


@router.post("/docs/documents/batch")
async def batch_documents(
    payload: BatchRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """批量操作：按动作分别校验权限点（publish → ``doc:document:publish``，
    offline → ``doc:document:offline``，update → ``doc:document:update``，
    delete → ``doc:document:delete``）。"""
    headers = _headers(authorization, x_internal_token, x_internal_user_id)
    action = payload.action.strip().lower()
    permission = {
        "publish": "doc:document:publish",
        "offline": "doc:document:offline",
        "update": "doc:document:update",
        # 管理台的「删除 / 批量删除」走的都是这个批量接口（见 DocumentList.runConfirm），
        # 这里原先没有 delete ⇒ 确认框修好后仍会撞 422「不支持的 action」。
        # 与单篇删除共用 `_perform_delete`，两条路径不会漂移。
        "delete": "doc:document:delete",
    }.get(action)
    if permission is None:
        raise HTTPException(status_code=422, detail=f"不支持的 action：{payload.action}")
    user, _ = _principal(request, headers, permission)
    runtime = _runtime(request)

    service = PermissionChangeService(
        db=runtime.db, settings=runtime.settings, providers=runtime.providers,
        shards=runtime.shards, fts=runtime.fts, cache=runtime.cache,
    )
    results: list[dict[str, Any]] = []
    for doc_id in payload.doc_ids:
        try:
            if action == "publish":
                _set_status(runtime, doc_id, "published", operator_id=user.user_id,
                            reason=payload.reason or "api:batch_publish")
                results.append({"doc_id": doc_id, "ok": True, "status": "published"})
            elif action == "offline":
                _set_status(runtime, doc_id, "offline", operator_id=user.user_id,
                            reason=payload.reason or "api:batch_offline")
                results.append({"doc_id": doc_id, "ok": True, "status": "offline"})
            elif action == "delete":
                info = _perform_delete(
                    runtime, doc_id, user_id=user.user_id,
                    reason=payload.reason or "api:batch_delete",
                    hard=payload.hard,
                    purge_assets=payload.purge_assets,
                )
                results.append({"ok": True, **info})
            else:
                changes = []
                if payload.level is not None:
                    changes.append(
                        (await service.change_document_level(
                            doc_id, payload.level, operator_id=user.user_id,
                            reason=payload.reason or "api:batch_update",
                        )).as_dict()
                    )
                if payload.visible_dept_ids is not None:
                    changes.append(
                        (await service.change_document_depts(
                            doc_id, payload.visible_dept_ids, operator_id=user.user_id,
                            reason=payload.reason or "api:batch_update",
                        )).as_dict()
                    )
                results.append({"doc_id": doc_id, "ok": True, "changes": changes})
        except (ConfigError, SchemaError) as exc:
            # 单个文档失败不影响整批（与 ekbs.tools.import 的取舍一致）
            results.append({"doc_id": doc_id, "ok": False, "error": str(exc)})
        except Exception as exc:  # pragma: no cover - 兜底
            log.error("batch_item_failed", doc_id=doc_id, error=str(exc))
            results.append({"doc_id": doc_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    succeeded = sum(1 for item in results if item["ok"])
    return JSONResponse({"action": action, "succeeded": succeeded, "results": results})


# ---------------------------------------------------------------------------
# 上传（init / chunk / complete，含 SHA-256 秒传）
# ---------------------------------------------------------------------------


def safe_relative_name(filename: str) -> str:
    """把上传文件名规整成**raw 目录内的相对路径**，挡掉一切越界写法。

    上传文件夹时前端会带上相对路径（``制度/2026/预算表.xlsx``）——
    保留它才能把目录结构带进库；但 ``filename`` 是客户端给的，
    所以必须自己挡：绝对路径、``..`` 段、空段、以点开头的段一律剔除。
    """
    parts: list[str] = []
    for raw in str(filename or "").replace("\\", "/").split("/"):
        seg = raw.strip()
        if not seg or seg in {".", ".."} or seg.startswith("."):
            continue
        parts.append(seg)
    return "/".join(parts) or "未命名"


class UploadInit(BaseModel):
    filename: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    checksum: str | None = Field(default=None, description="客户端计算的 SHA-256，用于秒传")
    kb_id: int | None = None
    kb_code: str | None = None
    level: int = Field(default=2, ge=1, le=4)
    visible_dept_ids: list[str] = Field(default_factory=list)
    #: 额外可查看的人（用户编号，可多选）。同一批上传的每份文件都写上这些人。
    viewer_user_ids: list[int] = Field(default_factory=list)
    #: 可选标题。上传文件夹时前端把相对路径（去扩展名）作为标题传上来，
    #: 否则 `Path(filename).stem` 会把目录结构整段丢掉，用户看到的就是"文件夹被分离了"。
    title: str | None = Field(default=None, max_length=512)


class UploadCheckHash(BaseModel):
    checksum: str = Field(min_length=16, description="客户端 SHA-256 hex")
    filename: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)


def _find_instant_doc(runtime: Any, checksum: str) -> int | None:
    """秒传命中：同 checksum 且非 failed/非 deleted。"""
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT doc_id FROM ekbs_document WHERE checksum = ? "
            "AND status NOT IN ('failed', 'deleted') "
            "ORDER BY doc_id LIMIT 1",
            (checksum,),
        ).fetchone()
    return int(row["doc_id"]) if row is not None else None


@router.post("/docs/upload/check-hash")
async def upload_check_hash(
    payload: UploadCheckHash,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """秒传预检（方案 §6.5）：有同内容且有效文档则 ``instant=true``。"""
    _principal(
        request, _headers(authorization, x_internal_token, x_internal_user_id),
        "doc:document:create",
    )
    doc_id = _find_instant_doc(_runtime(request), payload.checksum.strip().lower())
    if doc_id is not None:
        return JSONResponse({"instant": True, "doc_id": doc_id, "message": "秒传：内容已存在"})
    return JSONResponse({"instant": False})


@router.get("/docs/upload/{upload_id}/status")
async def upload_status(
    upload_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """断点续传状态：已收分片位图 + 分片大小提示。"""
    _principal(
        request, _headers(authorization, x_internal_token, x_internal_user_id),
        "doc:document:create",
    )
    if not all(c in "0123456789abcdef" for c in upload_id.lower()) or len(upload_id) < 16:
        raise HTTPException(status_code=422, detail="非法 upload_id")
    runtime = _runtime(request)
    state = await runtime.cache.get(f"{UPLOAD_CACHE_PREFIX}{upload_id}")
    if not isinstance(state, dict):
        raise HTTPException(status_code=404, detail="upload_id 不存在或已过期")
    chunk_mb = int(getattr(runtime.settings.upload, "chunk_size_mb", 8) or 8)
    parts = sorted(int(p) for p in (state.get("parts") or []))
    return JSONResponse(
        {
            "upload_id": upload_id,
            "uploaded_chunks": parts,
            "chunk_size_hint": chunk_mb * 1024 * 1024,
            "size_bytes": int(state.get("size_bytes") or 0),
        }
    )


@router.post("/docs/upload/init")
async def upload_init(
    payload: UploadInit,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """上传初始化：校验扩展名与大小闸门，并按 SHA-256 做**秒传**判定（`05` §8.2）。"""
    user, _p = _principal(
        request, _headers(authorization, x_internal_token, x_internal_user_id),
        "doc:document:create",
    )
    runtime = _runtime(request)
    settings = runtime.settings
    # 用统一的归一化取后缀：尾部空格 / 全角句点 / 末尾多余点都会让裸 suffix 取错，
    # 结果用户明明传的是 .xlsx 却被判"不支持的类型"（实测踩到）。
    extension = normalize_suffix(payload.filename)
    # **准入 = 配置允许 ∩ 解析器真能解析**
    # 只按配置白名单放行会出现"上传成功、入库却是乱码"：配置里有 .ppt/.html 这类
    # 解析器根本没实现的格式，放进来后 worker 走 text 分支把二进制当 UTF-8 读。
    # 取交集后：配置仍照 `01` §7 写（不偏离方案），但实际只放行能解析的格式；
    # 等解析分支补上，交集自动变大，配置一个字都不用改。
    configured = {str(e).lower().lstrip(".") for e in (settings.limits or {}).get("file_ext_whitelist", DEFAULT_EXTS)}
    parseable = {suffix.lstrip(".") for suffix in SUFFIX_ENGINES}
    whitelist = configured & parseable
    if extension not in whitelist:
        if extension in configured:
            raise HTTPException(
                status_code=422,
                detail=f"暂不支持解析该格式：.{extension}（配置允许，但解析器尚未实现，"
                       f"可用格式：{'、'.join(sorted(whitelist))}）",
            )
        raise HTTPException(status_code=422, detail=f"不支持的文件类型：.{extension}")
    max_mb = float((settings.limits or {}).get("max_file_size_mb", 500) or 500)
    if payload.size_bytes > max_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"文件超过 {max_mb:g} MB 上限")

    # 文件只使用直接所在文件夹的权限，不套用上级文件夹。
    # 个人库不读这份设置。放在分片归属判定之前，避免表单上的另一套把文件夹盖掉。
    kb_code_for_policy = str(payload.kb_code or "").strip()
    if not kb_code_for_policy and payload.kb_id is not None:
        with runtime.db.sync_connect() as conn:
            kb_row = conn.execute(
                "SELECT code FROM ekbs_kb WHERE kb_id = ?", (int(payload.kb_id),),
            ).fetchone()
        kb_code_for_policy = str(kb_row["code"]) if kb_row else ""
    from ekbs.ingest.folder_policy import policy_for_upload

    personal_owner = personal_user_id_from_code(kb_code_for_policy)
    if personal_owner is not None:
        # 个人库固定投给主人：不读文件夹权限，也不采用表单上的密级和部门。
        payload.level = PERSONAL_LEVEL
        payload.visible_dept_ids = [scope_ref(personal_owner)]
        payload.viewer_user_ids = []
    else:
        try:
            applied_policy = policy_for_upload(settings, kb_code_for_policy, payload.filename)
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if applied_policy is not None:
            payload.level = int(applied_policy["level"])
            payload.visible_dept_ids = list(applied_policy["visible_dept_ids"])
            payload.viewer_user_ids = list(applied_policy["viewer_user_ids"])

    # **分片归属判定前置**：L2/L3 不给部门、L4 未开，都会在**解析阶段**才被拒
    # （worker 异步失败，界面上只显示"上传成功"）。放在这里就能立刻 422，且复用
    # 入库路径的**同一份**判定函数（不另写规则）。
    # 顺序：扩展名 → 大小 → 归属。文件本身不合格（413/415）比元数据不合格更该先报。
    try:
        from ekbs.index.shard import resolve_shard_for_document

        resolve_shard_for_document(
            level=int(payload.level),
            visible_dept_ids=list(payload.visible_dept_ids),
            settings=settings,
        )
    except ConfigError as exc:
        raise HTTPException(status_code=422, detail=f"密级/部门不满足入库条件：{exc}") from exc

    # 上架范围（部门 × 密级）：只能发布到自己的管理范围、且不超过自己的密级上限
    # （super_admin 例外）。修复评估发现的「有上传权限点就能传任何部门/任何密级」。
    scope = check_document_scope(
        runtime.db,
        user_id=user.user_id,
        level=int(payload.level),
        visible_dept_ids=list(payload.visible_dept_ids),
    )
    if not scope.allowed:
        raise HTTPException(status_code=403, detail=f"无权发布该文档：{scope.detail}")

    with runtime.db.sync_connect() as conn:
        try:
            viewers = assert_viewers_assignable(
                conn,
                kb_id=payload.kb_id,
                kb_code=payload.kb_code,
                viewer_user_ids=payload.viewer_user_ids,
            )
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    if payload.checksum:
        doc_id = _find_instant_doc(runtime, str(payload.checksum).strip().lower())
        if doc_id is not None:
            return JSONResponse(
                {"instant": True, "doc_id": doc_id, "message": "秒传：内容已存在"}
            )

    with runtime.db.sync_connect() as conn:
        try:
            assert_personal_kb_storage(
                conn,
                settings,
                kb_id=payload.kb_id,
                kb_code=payload.kb_code,
                incoming=int(payload.size_bytes),
            )
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    upload_id = uuid.uuid4().hex
    await runtime.cache.set(
        f"{UPLOAD_CACHE_PREFIX}{upload_id}",
        {
            "filename": payload.filename,
            "title": payload.title,
            "size_bytes": payload.size_bytes,
            "checksum": payload.checksum or "",
            "kb_id": payload.kb_id,
            "kb_code": payload.kb_code,
            "level": payload.level,
            "visible_dept_ids": list(payload.visible_dept_ids),
            "viewer_user_ids": viewers,
            "parts": [],
        },
        ttl=UPLOAD_TTL_SECONDS,
    )
    (runtime.settings.data_paths()["tmp"] / upload_id).mkdir(parents=True, exist_ok=True)
    # 分片大小由配置给（方案 §6.5 的 upload.chunk_size_mb，默认 8 MB）。
    # 原先硬编码 5 MB，与配置项对不上，也不是方案口径。
    chunk_mb = int(getattr(settings.upload, "chunk_size_mb", 8) or 8)
    return JSONResponse(
        {
            "instant": False,
            "upload_id": upload_id,
            "chunk_size_hint": chunk_mb * 1024 * 1024,
            "uploaded_chunks": [],
        }
    )


@router.post("/docs/upload/{upload_id}/chunk")
async def upload_chunk(
    upload_id: str,
    request: Request,
    index: int | None = Form(default=None),
    chunk_index: int | None = Form(default=None),
    file: UploadFile | None = File(default=None),
    chunk: UploadFile | None = File(default=None),
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """上传一个分片。兼容方案字段 ``chunk_index``+``chunk`` 与现有 ``index``+``file``。"""
    _principal(
        request, _headers(authorization, x_internal_token, x_internal_user_id),
        "doc:document:create",
    )
    if not all(c in "0123456789abcdef" for c in upload_id.lower()) or len(upload_id) < 16:
        raise HTTPException(status_code=422, detail="非法 upload_id")
    runtime = _runtime(request)
    state = await runtime.cache.get(f"{UPLOAD_CACHE_PREFIX}{upload_id}")
    if not isinstance(state, dict):
        raise HTTPException(status_code=404, detail="upload_id 不存在或已过期")
    part_index = chunk_index if chunk_index is not None else index
    if part_index is None:
        raise HTTPException(status_code=422, detail="必须提供 index 或 chunk_index")
    upload = chunk or file
    if upload is None:
        raise HTTPException(status_code=422, detail="必须提供 file 或 chunk")
    target_dir = runtime.settings.data_paths()["tmp"] / upload_id
    target_dir.mkdir(parents=True, exist_ok=True)
    part_path = target_dir / f"part-{int(part_index):05d}"
    part_path.write_bytes(await upload.read())
    parts = set(state.get("parts") or [])
    parts.add(int(part_index))
    state["parts"] = sorted(parts)
    await runtime.cache.set(f"{UPLOAD_CACHE_PREFIX}{upload_id}", state, ttl=UPLOAD_TTL_SECONDS)
    return JSONResponse(
        {
            "upload_id": upload_id,
            "received": len(parts),
            "index": int(part_index),
            "uploaded_chunks": sorted(parts),
        }
    )


@router.post("/docs/upload/{upload_id}/complete")
async def upload_complete(
    upload_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """合并分片 → 落到 ``data_dir/raw/<checksum>/<文件名>`` → 建 ``pending`` 文档 → 等 worker 解析。"""
    user, _ = _principal(
        request, _headers(authorization, x_internal_token, x_internal_user_id),
        "doc:document:create",
    )
    runtime = _runtime(request)
    state = await runtime.cache.get(f"{UPLOAD_CACHE_PREFIX}{upload_id}")
    if not isinstance(state, dict):
        raise HTTPException(status_code=404, detail="upload_id 不存在或已过期")
    tmp_dir = runtime.settings.data_paths()["tmp"] / upload_id
    parts = sorted(tmp_dir.glob("part-*"))
    if not parts:
        raise HTTPException(status_code=409, detail="没有收到任何分片")
    merged = tmp_dir / "merged.bin"
    # **流式**合并与哈希：不要 `merged.read_bytes()` —— 那会把整个文件读进内存。
    # 上限一旦放宽（比如对齐 scheme §6.5 的 500 MB），一次性读入就等于凭空多出
    # 一个与文件等大的内存峰值，正好撞上「16 GB 机器不让解析峰值叠加」这条约束
    # （见 ekbs/worker.py 顶部）。这里边写边算，内存占用与文件大小无关。
    hasher = hashlib.sha256()
    with merged.open("wb") as sink:
        for part in parts:
            with part.open("rb") as src:
                while block := src.read(1 << 20):
                    sink.write(block)
                    hasher.update(block)

    digest = hasher.hexdigest()
    merged_bytes = merged.stat().st_size
    declared = int(state.get("size_bytes") or 0)
    # 闸门①：**分片完整性**（片数/顺序之外的最后一道，且**不依赖客户端校验和**）。
    #
    # 为什么必须有这道：浏览器算 SHA-256 用的是 `crypto.subtle`，它只在 https 或
    # localhost 下存在。内网走 http://<ip>:8000/admin 时 `sha256Hex()` 返回 null，
    # `checksum` 于是是空字符串 —— 下面那道校验和比对被 `if state.get(...)` 直接跳过。
    # 也就是说**在内网部署这个最常见的形态下，之前没有任何服务端校验**：
    # 少传一片、片区半截、片被改，都会照常入库。对 .md/.txt 这类纯文本，
    # 截断后仍能正常解析 ⇒ 静默丢内容；而 `ekbs_document.size_bytes` 记的是
    # **声明值**，库里连长度都对不上却没人比。
    #
    # 判断依据：切片的并集恰好覆盖整个文件（前端 `file.slice` 无重叠无缝隙），
    # 所以"合并后字节数 == init 声明的 size_bytes"是必要条件。
    # 保留 tmp 目录不删：`upload_chunk` 按 index 覆盖写，客户端补传缺的那片后
    # 可以再调一次 complete 恢复，不必整份重传。
    if declared and merged_bytes != declared:
        log.warning(
            "upload_size_mismatch",
            upload_id=upload_id, merged_bytes=merged_bytes, declared_bytes=declared,
            parts=len(parts),
        )
        raise HTTPException(
            status_code=422,
            detail=f"分片不完整：合并后 {merged_bytes} 字节，上传时声明 {declared} 字节",
        )
    if state.get("checksum") and str(state["checksum"]) != digest:
        raise HTTPException(status_code=422, detail="校验和不匹配（文件在传输中被改动或分片缺失）")

    kb_id = state.get("kb_id")
    if kb_id is None and state.get("kb_code"):
        with runtime.db.sync_connect() as conn:
            row = conn.execute("SELECT kb_id FROM ekbs_kb WHERE code = ?", (state["kb_code"],)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"知识库不存在：{state['kb_code']}")
        kb_id = int(row["kb_id"])
    if kb_id is None:
        raise HTTPException(status_code=422, detail="上传初始化时需给出 kb_id 或 kb_code")

    settings = runtime.settings
    try:
        settings.assert_l3_ingest_allowed(int(state.get("level", 2)))
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # **先校验再落盘**：原先把 shutil.move 放在校验之前，于是任何一次失败的上传
    # （最常见的正是上面那个 422）都会在 raw 里留下一个**没有文档记录的孤儿文件** ——
    # 用户重试一次就多一份垃圾，而且库里查不到、看起来像"上传没生效"。
    # 落盘布局：``raw/<知识库 code>/<你上传时的相对路径>``。
    # 早先用 ``raw/<sha256 前16位>/`` 做目录名 —— 功能上没错（同内容同目录），
    # 但在文件管理器里就是一堆 `95fbf1ef16573acf` 这样的壳子，人根本认不出是什么。
    # 去重本来就在库里按 ``checksum`` 做（秒传），不依赖目录名。
    raw_root = runtime.settings.data_paths()["raw"]
    kb_code = state.get("kb_code")
    if not kb_code:
        with runtime.db.sync_connect() as conn:
            row = conn.execute("SELECT code FROM ekbs_kb WHERE kb_id = ?", (int(kb_id),)).fetchone()
        kb_code = str(row["code"]) if row else "default"
    with runtime.db.sync_connect() as conn:
        try:
            assert_personal_kb_storage(
                conn,
                settings,
                kb_id=int(kb_id),
                kb_code=str(kb_code) if kb_code else None,
                incoming=merged_bytes,
            )
            viewers = assert_viewers_assignable(
                conn,
                kb_id=int(kb_id),
                kb_code=str(kb_code) if kb_code else None,
                viewer_user_ids=state.get("viewer_user_ids") or [],
            )
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    rel_name = safe_relative_name(str(state["filename"]))
    final_path = raw_root / safe_relative_name(str(kb_code)) / rel_name
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if final_path.exists():
        # 同名但内容不同（改了再传）：不覆盖原件，加内容后缀区分
        suffix = "".join(final_path.suffixes) or ""
        stem = final_path.name[: len(final_path.name) - len(suffix)] if suffix else final_path.name
        final_path = final_path.with_name(f"{stem}-{digest[:8]}{suffix}")
    shutil.move(str(merged), final_path)
    shutil.rmtree(tmp_dir, ignore_errors=True)

    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, visible_dept_ids, "
            "status, checksum, size_bytes, doc_version, created_by) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, 'v1', ?)",
            (
                int(kb_id),
                str(state.get("title") or Path(rel_name).stem),
                str(final_path),
                int(state.get("level", 2)),
                json.dumps(state.get("visible_dept_ids") or [], ensure_ascii=False),
                digest, int(state.get("size_bytes") or final_path.stat().st_size), user.user_id,
            ),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        grant_document_viewers(
            conn, doc_id=doc_id, viewer_user_ids=viewers,
            operator_id=user.user_id, reason="upload:viewers",
        )
        conn.commit()
    await runtime.cache.delete(f"{UPLOAD_CACHE_PREFIX}{upload_id}")
    log.info("upload_completed", doc_id=doc_id, bytes=final_path.stat().st_size, checksum=digest[:12])
    return JSONResponse(
        {"doc_id": doc_id, "status": "pending", "checksum": digest, "path": str(final_path)}
    )


# ---------------------------------------------------------------------------
# 索引重建（05 §8.2 的系统健康页）
# ---------------------------------------------------------------------------


class RebuildRequest(BaseModel):
    shard_id: str | None = Field(default=None, description="留空表示全部 active 分片")
    dry_run: bool = False


@router.post("/system/index/rebuild")
async def rebuild_index(
    payload: RebuildRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """重建 FAISS 分片索引（从 ``ekbs_chunk.content`` 重新嵌入；约束 C4 的"旋转而非重建"）。"""
    _principal(
        request, _headers(authorization, x_internal_token, x_internal_user_id),
        "sys:index:rebuild",
    )
    runtime = _runtime(request)
    from ekbs.tools.rebuild_shard import rebuild

    targets = (
        [payload.shard_id]
        if payload.shard_id
        else [row.shard_id for row in runtime.shards.registry()]
    )
    results = []
    for shard_id in targets:
        if shard_id not in {row.shard_id for row in runtime.shards.registry()}:
            results.append({"shard_id": shard_id, "ok": False, "error": "分片未登记"})
            continue
        try:
            results.append(
                {"shard_id": shard_id, "ok": True,
                 **(await rebuild(runtime, shard_id, dry_run=payload.dry_run))}
            )
        except Exception as exc:
            results.append({"shard_id": shard_id, "ok": False, "error": str(exc)})
    return JSONResponse({"results": results, "dry_run": payload.dry_run})


# ---------------------------------------------------------------------------
# 清空语料库（"清空重来"：块 / 权限 / 缓存 / 旧索引 / 分片计数）
# ---------------------------------------------------------------------------


class ResetCorpusRequest(BaseModel):
    dry_run: bool = True
    #: 非演练时必须显式传 ``CLEAR``，防止误点
    confirm: str = Field(default="", description="真清时必须等于 CLEAR")


@router.post("/system/corpus/reset")
async def reset_corpus_endpoint(
    payload: ResetCorpusRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """清空语料库，供「重导前重置」用。**保留分片注册行与知识库/用户**。"""
    _principal(
        request, _headers(authorization, x_internal_token, x_internal_user_id),
        "sys:index:rebuild",
    )
    if not payload.dry_run and payload.confirm.strip().upper() != "CLEAR":
        raise HTTPException(status_code=400, detail="真清需要 confirm=CLEAR")
    from ekbs.tools.reset_corpus import reset_corpus

    report = await asyncio.to_thread(
        reset_corpus, _runtime(request), dry_run=payload.dry_run
    )
    return JSONResponse(report.as_dict())
