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
from ekbs.logging import get_logger
from ekbs.permission.changes import PermissionChangeService
from ekbs.permission.router import bump_policy_version

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

    kb_id = payload.kb_id
    if kb_id is None and payload.kb_code:
        with runtime.db.sync_connect() as conn:
            row = conn.execute("SELECT kb_id FROM ekbs_kb WHERE code = ?", (payload.kb_code,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"知识库不存在：{payload.kb_code}")
        kb_id = int(row["kb_id"])
    if kb_id is None:
        raise HTTPException(status_code=422, detail="必须给出 kb_id 或 kb_code")

    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, visible_dept_ids, "
            "status, checksum, doc_version, effective_date, expire_date, created_by) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
            (
                kb_id, payload.title, payload.source_path or payload.title, payload.level,
                json.dumps(payload.visible_dept_ids, ensure_ascii=False), payload.checksum,
                payload.doc_version, payload.effective_date, payload.expire_date, user.user_id,
            ),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
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
    doc = _load_document(runtime, doc_id)
    with runtime.db.sync_connect() as conn:
        chunks = int(
            conn.execute(
                "SELECT COUNT(*) FROM ekbs_chunk WHERE doc_id = ? AND chunk_type = 'child'", (doc_id,)
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
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """删除文档：分块 + FTS5 行 + 文档行一并清理，并自增策略版本。

    ⚠️ FAISS 是**追加型**索引（``IndexFlatIP`` 不支持删除），因此旧向量会留成分片里的
    孤儿 —— 它们召不回（按 ``shard_id`` 与 ``ekbs_chunk`` 联查不到），但会白占容量。
    响应里会提示去跑 ``POST /api/v1/system/index/rebuild``。
    """
    user, _ = _principal(
        request, _headers(authorization, x_internal_token, x_internal_user_id),
        "doc:document:delete",
    )
    runtime = _runtime(request)
    _load_document(runtime, doc_id)
    with runtime.db.sync_connect() as conn:
        chunk_ids = [
            int(r["chunk_id"])
            for r in conn.execute("SELECT chunk_id FROM ekbs_chunk WHERE doc_id = ?", (doc_id,)).fetchall()
        ]
        shards = [
            str(r["shard_id"])
            for r in conn.execute(
                "SELECT DISTINCT shard_id FROM ekbs_chunk WHERE doc_id = ?", (doc_id,)
            ).fetchall()
        ]
    if chunk_ids:
        runtime.fts.delete_chunk_ids(chunk_ids)
    with runtime.db.sync_connect() as conn:
        conn.execute("DELETE FROM ekbs_chunk WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM ekbs_document WHERE doc_id = ?", (doc_id,))
        conn.execute(
            "INSERT INTO ekbs_perm_change_log(operator_id, object_type, object_id, change_type, "
            "field, old_value, new_value, reason) "
            "VALUES (?, 'document', ?, 'delete', 'deleted', 'present', 'absent', ?)",
            (user.user_id, str(doc_id), reason or "api:delete"),
        )
        version = bump_policy_version(conn, reason=f"api:delete:doc={doc_id}")
        conn.commit()
    log.warning(
        "document_deleted",
        doc_id=doc_id,
        chunks=len(chunk_ids),
        shards=shards,
        note="FAISS 旧向量成为孤儿，建议 rebuild 回收容量",
    )
    return JSONResponse(
        {
            "doc_id": doc_id,
            "deleted_chunks": len(chunk_ids),
            "affected_shards": shards,
            "policy_version": version,
            "hint": "FAISS 为追加型索引：建议对这些分片执行 /api/v1/system/index/rebuild 回收容量",
        }
    )


# ---------------------------------------------------------------------------
# 批量
# ---------------------------------------------------------------------------


class BatchRequest(BaseModel):
    doc_ids: list[int] = Field(min_length=1, max_length=200)
    action: str = Field(description="publish | offline | update")
    level: int | None = Field(default=None, ge=1, le=4)
    visible_dept_ids: list[str] | None = None
    reason: str = ""


@router.post("/docs/documents/batch")
async def batch_documents(
    payload: BatchRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """批量操作：按动作分别校验权限点（publish → ``doc:document:publish``，
    offline → ``doc:document:offline``，update → ``doc:document:update``）。"""
    headers = _headers(authorization, x_internal_token, x_internal_user_id)
    action = payload.action.strip().lower()
    permission = {
        "publish": "doc:document:publish",
        "offline": "doc:document:offline",
        "update": "doc:document:update",
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


class UploadInit(BaseModel):
    filename: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    checksum: str | None = Field(default=None, description="客户端计算的 SHA-256，用于秒传")
    kb_id: int | None = None
    kb_code: str | None = None
    level: int = Field(default=2, ge=1, le=4)
    visible_dept_ids: list[str] = Field(default_factory=list)


@router.post("/docs/upload/init")
async def upload_init(
    payload: UploadInit,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """上传初始化：校验扩展名与大小闸门，并按 SHA-256 做**秒传**判定（`05` §8.2）。"""
    _, _p = _principal(
        request, _headers(authorization, x_internal_token, x_internal_user_id),
        "doc:document:create",
    )
    runtime = _runtime(request)
    settings = runtime.settings
    extension = Path(payload.filename).suffix.lower().lstrip(".")
    whitelist = {str(e).lower().lstrip(".") for e in (settings.limits or {}).get("file_ext_whitelist", DEFAULT_EXTS)}
    if extension not in whitelist:
        raise HTTPException(status_code=422, detail=f"不支持的文件类型：.{extension}")
    max_mb = float((settings.limits or {}).get("max_file_size_mb", 100) or 100)
    if payload.size_bytes > max_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"文件超过 {max_mb:g} MB 上限")

    if payload.checksum:
        with runtime.db.sync_connect() as conn:
            row = conn.execute(
                "SELECT doc_id FROM ekbs_document WHERE checksum = ? AND status != 'failed' "
                "ORDER BY doc_id LIMIT 1",
                (payload.checksum,),
            ).fetchone()
        if row is not None:
            # 秒传：同样的内容已经在库里，不再重复上传
            return JSONResponse(
                {"instant": True, "doc_id": int(row["doc_id"]), "message": "秒传：内容已存在"}
            )

    upload_id = uuid.uuid4().hex
    await runtime.cache.set(
        f"{UPLOAD_CACHE_PREFIX}{upload_id}",
        {
            "filename": payload.filename,
            "size_bytes": payload.size_bytes,
            "checksum": payload.checksum or "",
            "kb_id": payload.kb_id,
            "kb_code": payload.kb_code,
            "level": payload.level,
            "visible_dept_ids": list(payload.visible_dept_ids),
            "parts": [],
        },
        ttl=UPLOAD_TTL_SECONDS,
    )
    (runtime.settings.data_paths()["tmp"] / upload_id).mkdir(parents=True, exist_ok=True)
    return JSONResponse({"instant": False, "upload_id": upload_id, "chunk_size_hint": 5 * 1024 * 1024})


@router.post("/docs/upload/{upload_id}/chunk")
async def upload_chunk(
    upload_id: str,
    request: Request,
    index: int = Form(default=0),
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """上传一个分片（单请求也可，分片只为断点续传）。"""
    _principal(
        request, _headers(authorization, x_internal_token, x_internal_user_id),
        "doc:document:create",
    )
    runtime = _runtime(request)
    state = await runtime.cache.get(f"{UPLOAD_CACHE_PREFIX}{upload_id}")
    if not isinstance(state, dict):
        raise HTTPException(status_code=404, detail="upload_id 不存在或已过期")
    target_dir = runtime.settings.data_paths()["tmp"] / upload_id
    target_dir.mkdir(parents=True, exist_ok=True)
    part_path = target_dir / f"part-{int(index):05d}"
    part_path.write_bytes(await file.read())
    parts = set(state.get("parts") or [])
    parts.add(int(index))
    state["parts"] = sorted(parts)
    await runtime.cache.set(f"{UPLOAD_CACHE_PREFIX}{upload_id}", state, ttl=UPLOAD_TTL_SECONDS)
    return JSONResponse({"upload_id": upload_id, "received": len(parts), "index": int(index)})


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
    with merged.open("wb") as sink:
        for part in parts:
            sink.write(part.read_bytes())

    digest = hashlib.sha256(merged.read_bytes()).hexdigest()
    if state.get("checksum") and str(state["checksum"]) != digest:
        raise HTTPException(status_code=422, detail="校验和不匹配（文件在传输中被改动或分片缺失）")

    raw_dir = runtime.settings.data_paths()["raw"] / digest[:16]
    raw_dir.mkdir(parents=True, exist_ok=True)
    final_path = raw_dir / str(state["filename"])
    shutil.move(str(merged), final_path)
    shutil.rmtree(tmp_dir, ignore_errors=True)

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

    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, visible_dept_ids, "
            "status, checksum, size_bytes, doc_version, created_by) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, 'v1', ?)",
            (
                int(kb_id), Path(str(state["filename"])).stem, str(final_path),
                int(state.get("level", 2)),
                json.dumps(state.get("visible_dept_ids") or [], ensure_ascii=False),
                digest, int(state.get("size_bytes") or final_path.stat().st_size), user.user_id,
            ),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
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
