"""图片与图表回源端点（`00` §2.2、`06` §4.11）。

``GET /assets/figure/{chunk_id}`` 的五步（缺一步就是越权）：

1. 鉴权（JWT 或内部回环 token）；
2. 取 chunk 的 ``shard_id`` / ``doc_id`` / ``image_path``；
3. **按 ``shard_id`` 做 ACL 复检**（不在当前 ``visible_shard_ids`` → **404**，不是 403，
   避免存在性泄露）；
4. 断言拼出的绝对路径落在 ``{data_dir}/parse/`` 之内（防 ``../`` 穿越）；
5. 写 ``ekbs_doc_access_log(action='view', channel='citation')``。

**禁止用 Caddy 静态托管 ``/data/ekbs/parse``** —— 那会在资源层绕开这里刚做对的权限。
"""

from __future__ import annotations


from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse

from ekbs.api.auth import (
    INTERNAL_CHANNEL_HEADER,
    INTERNAL_TOKEN_HEADER,
    INTERNAL_USER_HEADER,
)
from ekbs.api.qa import _authenticate
from ekbs.logging import get_logger

router = APIRouter(tags=["assets"])
log = get_logger(component="api.assets")


@router.get("/assets/figure/{chunk_id}")
async def get_figure(
    chunk_id: int,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
    x_internal_channel: str | None = Header(default=None, alias=INTERNAL_CHANNEL_HEADER),
) -> FileResponse:
    runtime = request.app.state.runtime
    user = _authenticate(
        request,
        {
            "authorization": authorization,
            INTERNAL_TOKEN_HEADER.lower(): x_internal_token,
            INTERNAL_USER_HEADER.lower(): x_internal_user_id,
            INTERNAL_CHANNEL_HEADER.lower(): x_internal_channel,
        },
    )
    perm = await runtime.perm_context(user.user_id)

    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT chunk_id, doc_id, shard_id, image_path, figure_id, chunk_type "
            "FROM ekbs_chunk WHERE chunk_id = ?",
            (int(chunk_id),),
        ).fetchone()
    if row is None or not row["image_path"]:
        raise HTTPException(status_code=404, detail="not_found")

    shard_id = str(row["shard_id"])
    if shard_id not in perm.visible_shard_ids:
        runtime.audit.record_access(
            user_id=user.user_id, doc_id=int(row["doc_id"]), action="view",
            channel="citation", result="denied",
        )
        raise HTTPException(status_code=404, detail="not_found")

    doc_id = int(row["doc_id"])
    facts = runtime.gate.load_facts([doc_id])
    entries = runtime.gate.load_document_acl([doc_id])
    decision = runtime.gate.can_read_document(perm, facts.get(doc_id), entries.get(doc_id, []))
    if not decision.allowed:
        runtime.audit.record_access(
            user_id=user.user_id, doc_id=doc_id, action="view", channel="citation",
            result="denied",
        )
        raise HTTPException(status_code=404, detail="not_found")

    parse_root = runtime.settings.data_paths()["parse"].resolve()
    candidate = (runtime.settings.data_dir / str(row["image_path"])).resolve()
    if parse_root not in candidate.parents and candidate != parse_root:
        log.error("figure_path_escape", chunk_id=chunk_id, path=str(candidate))
        raise HTTPException(status_code=404, detail="not_found")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="not_found")

    runtime.audit.record_access(
        user_id=user.user_id, doc_id=doc_id, action="view", channel="citation", result="ok"
    )
    return FileResponse(candidate)
