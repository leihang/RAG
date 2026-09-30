"""分块预览与编辑（`05` §8.2 的 ``/api/v1/chunks/*``）。

**这里的核心难点是"编辑文本 = 换向量"**，而 FAISS 是**追加型**索引
（``IndexFlatIP`` 不支持原地更新/删除）。本模块的处理方式与分片迁移一致：

1. 生成新向量 → **追加**到分片索引 → 回写 ``ekbs_chunk.faiss_row``；
2. （旧实现）旧向量留在索引里成为**孤儿** —— 现在索引是 ``IndexIDMap2``，
   编辑走 upsert 原地替换，不再有孤儿；这条保留作历史说明：
   只白占容量；响应里提示去跑 ``/api/v1/system/index/rebuild`` 回收。
3. 本地全文索引（FTS5）必须**同步重写**：``ekbs_chunk_fts`` 存的是 jieba 预分词结果，
   改了正文不重写，稀疏召回就会继续命中旧内容（`03` §5.3）。

⚠️ **子块与父块的上下文关系**（`00` §2.1 的父块扩展）：命中子块后，**进 LLM 上下文的是父块全文**。
因此只改子块正文，答案里看到的仍是旧父块内容 —— 编辑子块后若希望答案立即反映修改，
**必须同时编辑它的父块**（编辑器应提示这一点；本模块在响应里给出 `parent_id` 供前端跳转）。

**`split` / `merge` 只支持子块**：子块才是检索单元，父子关系由分块器产出；
对父块做拆分/合并会牵连它的全部子块（每个都要重嵌入与重写 FTS），
本实现直接拒绝并说明原因 —— 宁可明确不做，也不要做出一个"看起来成功但索引不一致"的结果。

| 端点 | 权限点（`05` §8.2 / §3.4） |
|---|---|
| ``GET /chunks/documents/{doc_id}/tree`` | ``doc:chunk:read``（含 auditor） |
| ``GET /chunks/{chunk_id}`` | ``doc:chunk:read`` |
| ``GET /chunks/documents/{doc_id}/export`` | ``doc:chunk:read`` |
| ``PUT /chunks/{chunk_id}`` | ``doc:chunk:update``（SA/KA/DR） |
| ``POST /chunks/{chunk_id}/split`` ``/merge`` | ``doc:chunk:update`` |
| ``POST /chunks/{chunk_id}/reembed`` | ``doc:chunk:reembed``（**仅 SA/KA**） |
| ``POST /chunks/documents/{doc_id}/revectorize`` | ``doc:chunk:reembed`` |
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ekbs.api.auth import INTERNAL_TOKEN_HEADER, INTERNAL_USER_HEADER
from ekbs.api.qa import _authenticate
from ekbs.errors import PermissionDenied
from ekbs.ingest.chunker import count_tokens, retrieval_text
from ekbs.logging import get_logger

router = APIRouter(tags=["chunks"])
log = get_logger(component="api.chunks")


def _principal(request: Request, headers: dict[str, str | None], code: str) -> Any:
    runtime = request.app.state.runtime
    user = _authenticate(request, headers)
    try:
        return runtime.authz.require(user.user_id, code)
    except PermissionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _h(authorization: str | None, token: str | None, user_id: str | None) -> dict[str, str | None]:
    return {
        "authorization": authorization,
        INTERNAL_TOKEN_HEADER.lower(): token,
        INTERNAL_USER_HEADER.lower(): user_id,
    }


def _chunk_row(runtime: Any, chunk_id: int) -> Any:
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT c.*, d.title AS doc_title, d.status AS doc_status, d.level AS doc_level "
            "FROM ekbs_chunk c JOIN ekbs_document d ON d.doc_id = c.doc_id WHERE c.chunk_id = ?",
            (chunk_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="chunk_not_found")
    return row


def _chunk_view(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["token_count"] = item.get("token_count") or count_tokens(str(item.get("content") or ""))
    item["retrieval_text"] = retrieval_text(item.get("heading_path"), str(item.get("content") or ""))
    return item


async def _reembed_async(
    runtime: Any, chunk_id: int, *, content: str, heading_path: str | None
) -> dict[str, Any]:
    """异步版：嵌入 → 追加到分片 → 回写 faiss_row / 指纹 / token 数 → 重写 FTS 行。"""
    import numpy as np

    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT chunk_id, doc_id, shard_id, chunk_type FROM ekbs_chunk WHERE chunk_id = ?",
            (chunk_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="chunk_not_found")
    if str(row["chunk_type"]) != "child":
        return {"embedded": False, "reason": "parent_chunk_not_indexed"}

    shard_id = str(row["shard_id"])
    text = retrieval_text(heading_path, content)
    vector = await runtime.providers.embed.embed_one(text)
    # **upsert 而非 append**：编辑/重嵌用的是同一个 chunk_id，
    # 而 FAISS 的 add_with_ids 对重复 id 是累积而不是替换（会召回重复、索引越长越大）
    rows = runtime.shards.upsert(shard_id, [chunk_id], np.asarray([vector], dtype="float32"))
    fingerprint = runtime.providers.embed.ref.fingerprint
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_chunk SET content = ?, heading_path = ?, token_count = ?, "
            "faiss_row = ?, embedding_fingerprint = ? WHERE chunk_id = ?",
            (content, heading_path, count_tokens(content), rows[-1], fingerprint, chunk_id),
        )
        index = runtime.shards.get(shard_id)
        if index is not None:
            conn.execute(
                "UPDATE ekbs_shard SET vector_count = ?, dim = ?, embedding_fingerprint = ?, "
                "updated_at = datetime('now') WHERE shard_id = ?",
                (index.vector_count, int(index.dim), fingerprint, shard_id),
            )
        conn.commit()
    # FTS5 存的是 jieba 预分词：不重写就会继续命中旧内容
    runtime.fts.delete_chunk_ids([chunk_id])
    runtime.fts.upsert_chunks([(chunk_id, shard_id, content, heading_path or "")])
    return {"embedded": True, "shard_id": shard_id, "faiss_row": rows[-1]}


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------


@router.get("/chunks/documents/{doc_id}/tree")
async def chunk_tree(
    doc_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """父子块树 + 标题路径（编辑器的数据源）。"""
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "doc:chunk:read")
    runtime = request.app.state.runtime
    with runtime.db.sync_connect() as conn:
        doc = conn.execute(
            "SELECT doc_id, title, status, level FROM ekbs_document WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        if doc is None:
            raise HTTPException(status_code=404, detail="document_not_found")
        rows = conn.execute(
            "SELECT chunk_id, parent_id, chunk_type, seq, heading_path, page_from, page_to, "
            "token_count, shard_id, faiss_row, substr(content, 1, 200) AS content_head, content "
            "FROM ekbs_chunk WHERE doc_id = ? ORDER BY seq",
            (doc_id,),
        ).fetchall()
    parents = [dict(r) for r in rows if str(r["chunk_type"]) != "child"]
    children = [dict(r) for r in rows if str(r["chunk_type"]) == "child"]
    tree: list[dict[str, Any]] = []
    for parent in parents:
        node = dict(parent)
        node["children"] = [
            dict(child) for child in children if child.get("parent_id") == parent["chunk_id"]
        ]
        tree.append(node)
    orphans = [
        dict(child)
        for child in children
        if not any(child.get("parent_id") == parent["chunk_id"] for parent in parents)
    ]
    return JSONResponse(
        {
            "document": dict(doc),
            "parents": len(parents),
            "children": len(children),
            "tree": tree,
            "orphan_children": orphans,
        }
    )


@router.get("/chunks/{chunk_id}")
async def chunk_detail(
    chunk_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """分块详情（含分片归属与页码、嵌入指纹）。"""
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "doc:chunk:read")
    runtime = request.app.state.runtime
    return JSONResponse({"chunk": _chunk_view(_chunk_row(runtime, chunk_id))})


@router.get("/chunks/documents/{doc_id}/export")
async def export_chunks(
    doc_id: int,
    request: Request,
    fmt: str = Query(default="jsonl", pattern="^(jsonl|csv)$"),
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> PlainTextResponse:
    """导出分块（JSONL/CSV）。给数据治理与人工复核用。"""
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "doc:chunk:read")
    runtime = request.app.state.runtime
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT chunk_id, parent_id, chunk_type, seq, heading_path, page_from, page_to, "
            "token_count, shard_id, faiss_row, content FROM ekbs_chunk WHERE doc_id = ? "
            "ORDER BY seq",
            (doc_id,),
        ).fetchall()
    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()) if rows else ["chunk_id"])
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row[k] for k in writer.fieldnames})
        return PlainTextResponse(buffer.getvalue(), media_type="text/csv")
    lines = [json.dumps({k: row[k] for k in row.keys()}, ensure_ascii=False) for row in rows]
    return PlainTextResponse("\n".join(lines), media_type="application/x-ndjson")


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------


class ChunkUpdate(BaseModel):
    content: str = Field(min_length=1)
    heading_path: str | None = None


@router.put("/chunks/{chunk_id}")
async def update_chunk(
    chunk_id: int,
    payload: ChunkUpdate,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """编辑分块文本。

    * **子块**：内容变了就**原地替换**向量（``IndexIDMap2`` 的 upsert：先删后加）并重写 FTS 行；
    * **父块**：只改内容与 token 数（父块不进向量索引，也不会进 FTS）。
    """
    principal = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "doc:chunk:update"
    )
    runtime = request.app.state.runtime
    row = _chunk_row(runtime, chunk_id)
    heading = payload.heading_path if payload.heading_path is not None else row["heading_path"]
    old_content = str(row["content"])
    if old_content == payload.content and heading == row["heading_path"]:
        return JSONResponse({"chunk_id": chunk_id, "changed": False, "note": "内容与标题均未变化"})

    if str(row["chunk_type"]) == "child":
        result = await _reembed_async(runtime, chunk_id, content=payload.content, heading_path=heading)
    else:
        with runtime.db.sync_connect() as conn:
            conn.execute(
                "UPDATE ekbs_chunk SET content = ?, heading_path = ?, token_count = ? "
                "WHERE chunk_id = ?",
                (payload.content, heading, count_tokens(payload.content), chunk_id),
            )
            conn.commit()
        result = {"embedded": False, "reason": "parent_chunk_not_indexed"}
    log.info(
        "chunk_updated", chunk_id=chunk_id, operator=principal.user_id,
        chunk_type=str(row["chunk_type"]), **result,
    )
    return JSONResponse(
        {
            "chunk_id": chunk_id,
            "changed": True,
            **result,
            "parent_id": row["parent_id"],
            "old_content_chars": len(old_content),
            "new_content_chars": len(payload.content),
            "hint": "子块向量已按 chunk_id 原地替换（upsert），无需 rebuild 回收容量",
            "context_hint": (
                "进 LLM 上下文的是父块全文（00 §2.1）：只改子块时答案仍会引用旧父块内容，"
                "如需答案立即反映修改，请同时编辑父块"
            )
            if str(row["chunk_type"]) == "child" and row["parent_id"]
            else "",
        }
    )


@router.post("/chunks/{chunk_id}/reembed")
async def reembed_chunk(
    chunk_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """单独重新嵌入一个子块（内容不变，只换向量）。"""
    _principal(request, _h(authorization, x_internal_token, x_internal_user_id), "doc:chunk:reembed")
    runtime = request.app.state.runtime
    row = _chunk_row(runtime, chunk_id)
    result = await _reembed_async(
        runtime, chunk_id, content=str(row["content"]), heading_path=row["heading_path"]
    )
    if not result.get("embedded"):
        raise HTTPException(
            status_code=409,
            detail=f"该分块类型为 {row['chunk_type']}，不参与向量索引（只有 child 需要嵌入）",
        )
    return JSONResponse({"chunk_id": chunk_id, **result})


class SplitRequest(BaseModel):
    offset: int = Field(gt=0, description="按字符位置切分（左闭右开）")


@router.post("/chunks/{chunk_id}/split")
async def split_chunk(
    chunk_id: int,
    payload: SplitRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """按字符位置拆分一个**子块**（两半都重嵌入并重写 FTS）。"""
    principal = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "doc:chunk:update"
    )
    runtime = request.app.state.runtime
    row = _chunk_row(runtime, chunk_id)
    if str(row["chunk_type"]) != "child":
        raise HTTPException(
            status_code=409,
            detail="只支持拆分 child 分块：拆父块会牵连其全部子块（都需重嵌入与重写 FTS），"
            "本实现不做半途而废的索引变更",
        )
    content = str(row["content"])
    if not 0 < payload.offset < len(content):
        raise HTTPException(status_code=422, detail=f"offset 必须落在 (0, {len(content)}) 之内")
    left = content[: payload.offset].strip()
    right = content[payload.offset :].strip()
    if not left or not right:
        raise HTTPException(status_code=422, detail="切分后有一侧为空，请调整 offset")

    # 先给新块腾出 seq：后续同级块统一 +1（保证顺序稳定）
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_chunk SET seq = seq + 1 WHERE doc_id = ? AND seq > ?",
            (int(row["doc_id"]), int(row["seq"])),
        )
        conn.execute(
            "INSERT INTO ekbs_chunk(doc_id, parent_id, chunk_type, seq, content, token_count, "
            "heading_path, page_from, page_to, shard_id, embedding_fingerprint) "
            "VALUES (?, ?, 'child', ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(row["doc_id"]), row["parent_id"], int(row["seq"]) + 1, "", 0,
                row["heading_path"], row["page_from"], row["page_to"], row["shard_id"],
                runtime.providers.embed.ref.fingerprint,
            ),
        )
        new_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.commit()

    await _reembed_async(runtime, chunk_id, content=left, heading_path=row["heading_path"])
    await _reembed_async(runtime, new_id, content=right, heading_path=row["heading_path"])
    log.info("chunk_split", chunk_id=chunk_id, new_chunk_id=new_id, operator=principal.user_id)
    return JSONResponse(
        {
            "chunk_id": chunk_id,
            "new_chunk_id": new_id,
            "left_chars": len(left),
            "right_chars": len(right),
            "hint": "两半都已重嵌入并重写 FTS；原块向量已真删，无需 rebuild",
        }
    )


@router.post("/chunks/{chunk_id}/merge")
async def merge_chunk(
    chunk_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """把该子块与**同父的下一个兄弟块**合并（合并后的块重嵌入并重写 FTS，被并入的块删除）。"""
    principal = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "doc:chunk:update"
    )
    runtime = request.app.state.runtime
    row = _chunk_row(runtime, chunk_id)
    if str(row["chunk_type"]) != "child":
        raise HTTPException(status_code=409, detail="只支持合并 child 分块（理由同 split）")
    with runtime.db.sync_connect() as conn:
        sibling = conn.execute(
            "SELECT chunk_id, content, seq, shard_id FROM ekbs_chunk WHERE doc_id = ? AND chunk_type='child' "
            "AND parent_id IS ? AND seq > ? ORDER BY seq LIMIT 1",
            (int(row["doc_id"]), row["parent_id"], int(row["seq"])),
        ).fetchone()
    if sibling is None:
        raise HTTPException(status_code=409, detail="没有可合并的下一个兄弟块")

    merged = f"{str(row['content']).rstrip()}\n\n{str(sibling['content']).lstrip()}"
    runtime.fts.delete_chunk_ids([int(sibling["chunk_id"])])
    # 兄弟块的**向量也要真删**：索引是 IDMap2，删掉即回收（旧实现只能留孤儿）
    runtime.shards.remove(str(sibling["shard_id"]), [int(sibling["chunk_id"])])
    with runtime.db.sync_connect() as conn:
        conn.execute("DELETE FROM ekbs_chunk WHERE chunk_id = ?", (int(sibling["chunk_id"]),))
        conn.commit()
    await _reembed_async(runtime, chunk_id, content=merged, heading_path=row["heading_path"])
    log.info(
        "chunk_merged", chunk_id=chunk_id, removed=int(sibling["chunk_id"]),
        operator=principal.user_id,
    )
    return JSONResponse(
        {
            "chunk_id": chunk_id,
            "removed_chunk_id": int(sibling["chunk_id"]),
            "merged_chars": len(merged),
            "hint": "被并入块的向量已真删，无需 rebuild",
        }
    )


@router.post("/chunks/documents/{doc_id}/revectorize")
async def revectorize_document(
    doc_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
) -> JSONResponse:
    """整篇重新向量化：**重建该文档所在的全部分片**（从 ``ekbs_chunk.content`` 重嵌入）。

    实现上复用 ``rebuild_shard`` —— 这样能顺手把孤儿向量一并清理掉，
    比"只重算这篇文档的块"更干净（后者仍会留下旧向量）。
    """
    principal = _principal(
        request, _h(authorization, x_internal_token, x_internal_user_id), "doc:chunk:reembed"
    )
    runtime = request.app.state.runtime
    with runtime.db.sync_connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM ekbs_document WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="document_not_found")
        shards = [
            str(r["shard_id"])
            for r in conn.execute(
                "SELECT DISTINCT shard_id FROM ekbs_chunk WHERE doc_id = ?", (doc_id,)
            ).fetchall()
        ]
    if not shards:
        raise HTTPException(status_code=409, detail="该文档没有分块，无需重新向量化")
    from ekbs.tools.rebuild_shard import rebuild

    results = []
    for shard_id in shards:
        try:
            results.append({"shard_id": shard_id, "ok": True, **(await rebuild(runtime, shard_id))})
        except Exception as exc:  # pragma: no cover - 兜底
            results.append({"shard_id": shard_id, "ok": False, "error": str(exc)})
    log.info("document_revectorized", doc_id=doc_id, operator=principal.user_id, shards=shards)
    return JSONResponse({"doc_id": doc_id, "shards": shards, "results": results})
