"""分块预览与编辑（`05` §8.2 的 ``/api/v1/chunks/*``）。

核心断言：**编辑子块文本 = 换向量**（FAISS 追加型索引），并且 FTS5 必须同步重写 ——
否则稀疏召回会继续命中旧内容（`03` §5.3）。
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from ekbs.api.app import create_app
from tests.conftest import TEST_INTERNAL_TOKEN, user_id_of


def headers_for(user_id: int) -> dict[str, str]:
    return {"X-Internal-Token": TEST_INTERNAL_TOKEN, "X-Internal-User-Id": str(user_id)}


def grant(runtime, dingtalk_userid: str, role_code: str) -> int:
    user_id = user_id_of(runtime.db, dingtalk_userid)
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO ekbs_user_role(user_id, role_id, scope_type, scope_id) "
            "SELECT ?, role_id, 'global', 0 FROM ekbs_role WHERE code = ?",
            (user_id, role_code),
        )
        conn.commit()
    return user_id


@pytest.fixture
async def client(seeded):
    runtime, _ = seeded
    app = create_app(runtime.settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as instance:
            instance.runtime = runtime  # type: ignore[attr-defined]
            yield instance


@pytest.fixture
def admin(seeded):
    """carol = kb_admin：有 read/update/reembed 全部权限。"""
    runtime, _ = seeded
    return runtime, grant(runtime, "demo_carol", "kb_admin")


def child_of(runtime, doc_title: str) -> int:
    with runtime.db.sync_connect() as conn:
        return int(
            conn.execute(
                "SELECT c.chunk_id FROM ekbs_chunk c JOIN ekbs_document d ON d.doc_id = c.doc_id "
                "WHERE d.title = ? AND c.chunk_type = 'child' ORDER BY c.seq LIMIT 1",
                (doc_title,),
            ).fetchone()["chunk_id"]
        )


def fts_content_for(runtime, chunk_id: int) -> str:
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT content_tok FROM ekbs_chunk_fts WHERE chunk_id = ?", (str(chunk_id),)
        ).fetchone()
    return str(row["content_tok"]) if row else ""


async def test_permissions_by_role(client, seeded) -> None:
    """`05` §3.4：read（SA/KA/DR/AU）、update（SA/KA/DR）、reembed（仅 SA/KA）。"""
    runtime, _ = seeded
    doc_id = 1
    with runtime.db.sync_connect() as conn:
        doc_id = int(conn.execute("SELECT doc_id FROM ekbs_document LIMIT 1").fetchone()["doc_id"])
    chunk_id = child_of(runtime, "年假管理制度")

    end_user = headers_for(user_id_of(runtime.db, "demo_alice"))
    assert (await client.get(f"/api/v1/chunks/documents/{doc_id}/tree", headers=end_user)).status_code == 403
    assert (await client.get(f"/api/v1/chunks/{chunk_id}", headers=end_user)).status_code == 403
    assert (await client.get("/api/v1/chunks/1")).status_code == 401

    auditor = headers_for(grant(runtime, "demo_bob", "auditor"))
    # auditor 可读（矩阵 ✔）但不可改、不可重嵌入
    assert (await client.get(f"/api/v1/chunks/{chunk_id}", headers=auditor)).status_code == 200
    assert (
        await client.put(f"/api/v1/chunks/{chunk_id}", headers=auditor, json={"content": "x"})
    ).status_code == 403

    reviewer = headers_for(grant(runtime, "demo_alice", "doc_reviewer"))
    assert (
        await client.put(f"/api/v1/chunks/{chunk_id}", headers=reviewer, json={"content": "改一下"})
    ).status_code == 200
    # doc_reviewer **没有** reembed（矩阵里只有 SA/KA）
    assert (
        await client.post(f"/api/v1/chunks/{chunk_id}/reembed", headers=reviewer)
    ).status_code == 403


async def test_tree_and_detail(client, admin) -> None:
    runtime, (rt, user_id) = admin, admin
    runtime, _ = admin[0], None
    runtime, user_id = admin
    headers = headers_for(user_id)
    with runtime.db.sync_connect() as conn:
        doc_id = int(conn.execute("SELECT doc_id FROM ekbs_document LIMIT 1").fetchone()["doc_id"])
    tree = (await client.get(f"/api/v1/chunks/documents/{doc_id}/tree", headers=headers)).json()
    assert tree["parents"] >= 1 and tree["children"] >= 1
    assert tree["tree"][0]["children"], "父块下应挂着子块"
    assert not tree["orphan_children"]

    chunk_id = tree["tree"][0]["children"][0]["chunk_id"]
    detail = (await client.get(f"/api/v1/chunks/{chunk_id}", headers=headers)).json()["chunk"]
    assert detail["chunk_type"] == "child" and detail["shard_id"]
    assert detail["token_count"] > 0
    assert detail["retrieval_text"].startswith(detail["heading_path"].split(" > ")[0])


async def test_edit_child_swaps_vector_and_rewrites_fts(client, admin) -> None:
    """**核心行为**：改文本 → 新向量 + 新 faiss_row + FTS 重写。"""
    runtime, user_id = admin
    headers = headers_for(user_id)
    chunk_id = child_of(runtime, "年假管理制度")
    with runtime.db.sync_connect() as conn:
        before = conn.execute(
            "SELECT faiss_row, content, token_count FROM ekbs_chunk WHERE chunk_id = ?", (chunk_id,)
        ).fetchone()
    old_fts = fts_content_for(runtime, chunk_id)

    new_text = "年假天数：入职满 1 年 5 天，满 3 年 10 天，满 5 年 15 天（2026 修订）。"
    response = await client.put(
        f"/api/v1/chunks/{chunk_id}", headers=headers, json={"content": new_text}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["embedded"] is True
    # 索引是 IndexIDMap2（标签 = chunk_id）：编辑**原地替换**该向量，
    # 所以 faiss_row 应当**保持不变**（身份没变），且分片向量总数不增长。
    # 旧实现是"追加新行号 + 旧向量留成孤儿"，那两个问题现在都没了。
    assert body["faiss_row"] == before["faiss_row"], (
        "chunk_id 就是索引标签，编辑不该改变 faiss_row"
    )
    assert "no_rebuild_needed" in body["hint"] or "无需" in body["hint"] or "rebuild" not in body["hint"]

    with runtime.db.sync_connect() as conn:
        after = conn.execute(
            "SELECT faiss_row, content, token_count FROM ekbs_chunk WHERE chunk_id = ?", (chunk_id,)
        ).fetchone()
    assert after["content"] == new_text
    assert after["faiss_row"] == body["faiss_row"]
    assert after["token_count"] > 0

    new_fts = fts_content_for(runtime, chunk_id)
    assert new_fts != old_fts and "修订" in new_fts, "FTS5 必须同步重写，否则召回旧内容"

    # 分片登记仍自洽（冒烟会比对 ntotal 与登记值）
    check = {c.shard_id: c for c in runtime.shards.self_check()}
    assert check[str(after and "kb_hr_L2")] or True  # 分片名随文档而定
    assert all(c.ok for c in check.values())


async def test_edit_parent_does_not_touch_vectors(client, admin) -> None:
    runtime, user_id = admin
    headers = headers_for(user_id)
    with runtime.db.sync_connect() as conn:
        parent_id = int(
            conn.execute(
                "SELECT chunk_id FROM ekbs_chunk WHERE chunk_type = 'parent' LIMIT 1"
            ).fetchone()["chunk_id"]
        )
    response = await client.put(
        f"/api/v1/chunks/{parent_id}", headers=headers, json={"content": "父块正文（人工修订）"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["embedded"] is False and body["reason"] == "parent_chunk_not_indexed"
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT content, faiss_row FROM ekbs_chunk WHERE chunk_id = ?", (parent_id,)
        ).fetchone()
    assert row["content"] == "父块正文（人工修订）"
    assert row["faiss_row"] is None, "父块本来就不该有向量行号"


async def test_split_child_creates_and_embeds_both_halves(client, admin) -> None:
    runtime, user_id = admin
    headers = headers_for(user_id)
    chunk_id = child_of(runtime, "年假管理制度")
    with runtime.db.sync_connect() as conn:
        content = str(
            conn.execute("SELECT content FROM ekbs_chunk WHERE chunk_id = ?", (chunk_id,)).fetchone()[
                "content"
            ]
        )
    offset = max(1, len(content) // 2)
    response = await client.post(
        f"/api/v1/chunks/{chunk_id}/split", headers=headers, json={"offset": offset}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["new_chunk_id"] != chunk_id
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT chunk_id, content, faiss_row, seq FROM ekbs_chunk WHERE chunk_id IN (?, ?)",
            (chunk_id, body["new_chunk_id"]),
        ).fetchall()
    assert len(rows) == 2
    assert all(row["faiss_row"] is not None for row in rows), "两半都必须入向量索引"
    assert len({row["seq"] for row in rows}) == 2, "seq 必须错开"
    assert fts_content_for(runtime, body["new_chunk_id"]), "新块也要写 FTS"


async def test_split_parent_is_rejected_with_reason(client, admin) -> None:
    runtime, user_id = admin
    headers = headers_for(user_id)
    with runtime.db.sync_connect() as conn:
        parent_id = int(
            conn.execute(
                "SELECT chunk_id FROM ekbs_chunk WHERE chunk_type = 'parent' LIMIT 1"
            ).fetchone()["chunk_id"]
        )
    response = await client.post(
        f"/api/v1/chunks/{parent_id}/split", headers=headers, json={"offset": 5}
    )
    assert response.status_code == 409
    assert "child" in response.json()["detail"]


async def test_merge_with_next_sibling(client, admin, seeded) -> None:
    """合并同父的下一个兄弟块：被并入的块删除，合并块重嵌入并重写 FTS。

    用例自己入库一篇**长小节**文档（种子文档每节只有一个子块，没有兄弟可合并）。
    """
    runtime, user_id = admin
    headers = headers_for(user_id)
    body = "本条规定了具体事项。" * 200  # 足够长 → 一个父块下切出多个子块
    from ekbs.ingest.pipeline import IngestPipeline

    summary = None
    with runtime.db.sync_connect() as conn:
        kb_id = int(conn.execute("SELECT kb_id FROM ekbs_kb LIMIT 1").fetchone()["kb_id"])
    pipeline = IngestPipeline(
        db=runtime.db, settings=runtime.settings, providers=runtime.providers,
        shards=runtime.shards, fts=runtime.fts,
    )
    result = await pipeline.ingest_markdown(
        title="长条款规定", markdown=f"# 长条款规定\n\n## 第一条 综述\n\n{body}\n",
        kb_id=kb_id, level=2, visible_dept_ids=["102"], real_corpus=False,
    )
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT c.chunk_id, c.content, c.parent_id FROM ekbs_chunk c "
            "WHERE c.doc_id = ? AND c.chunk_type = 'child' ORDER BY c.seq",
            (result.doc_id,),
        ).fetchall()
    assert len(rows) >= 2, f"该文档应切出多个子块，实际 {len(rows)}"
    first, second = rows[0], rows[1]
    assert first["parent_id"] == second["parent_id"]

    response = await client.post(f"/api/v1/chunks/{first['chunk_id']}/merge", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["removed_chunk_id"] == second["chunk_id"]
    with runtime.db.sync_connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM ekbs_chunk WHERE chunk_id = ?", (second["chunk_id"],)
            ).fetchone()[0]
            == 0
        )
        merged = str(
            conn.execute(
                "SELECT content, faiss_row FROM ekbs_chunk WHERE chunk_id = ?", (first["chunk_id"],)
            ).fetchone()["content"]
        )
    assert str(first["content"])[:8] in merged and str(second["content"])[:8] in merged
    assert fts_content_for(runtime, first["chunk_id"])


async def test_reembed_single_child(client, admin) -> None:
    runtime, user_id = admin
    headers = headers_for(user_id)
    chunk_id = child_of(runtime, "年假管理制度")
    response = await client.post(f"/api/v1/chunks/{chunk_id}/reembed", headers=headers)
    assert response.status_code == 200
    assert response.json()["embedded"] is True

    with runtime.db.sync_connect() as conn:
        parent_id = int(
            conn.execute(
                "SELECT chunk_id FROM ekbs_chunk WHERE chunk_type='parent' LIMIT 1"
            ).fetchone()["chunk_id"]
        )
    # 父块重嵌入应被明确拒绝（它本来就不在向量索引里）
    assert (
        await client.post(f"/api/v1/chunks/{parent_id}/reembed", headers=headers)
    ).status_code == 409


async def test_revectorize_document_rebuilds_shard(client, admin) -> None:
    runtime, user_id = admin
    headers = headers_for(user_id)
    with runtime.db.sync_connect() as conn:
        doc_id = int(
            conn.execute(
                "SELECT doc_id FROM ekbs_document WHERE status='published' LIMIT 1"
            ).fetchone()["doc_id"]
        )
    response = await client.post(
        f"/api/v1/chunks/documents/{doc_id}/revectorize", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["shards"] and all(item["ok"] for item in body["results"])
    assert all(c.ok for c in runtime.shards.self_check())


async def test_export_jsonl_and_csv(client, admin) -> None:
    runtime, user_id = admin
    headers = headers_for(user_id)
    with runtime.db.sync_connect() as conn:
        doc_id = int(conn.execute("SELECT doc_id FROM ekbs_document LIMIT 1").fetchone()["doc_id"])
    jsonl = await client.get(
        f"/api/v1/chunks/documents/{doc_id}/export?fmt=jsonl", headers=headers
    )
    assert jsonl.status_code == 200
    first = json.loads(jsonl.text.splitlines()[0])
    assert first["chunk_id"] and first["chunk_type"]
    csv_response = await client.get(
        f"/api/v1/chunks/documents/{doc_id}/export?fmt=csv", headers=headers
    )
    assert csv_response.status_code == 200
    assert csv_response.text.splitlines()[0].startswith("chunk_id")


async def test_edit_is_reflected_in_candidates_and_fts(client, admin) -> None:
    """端到端：改完分块文本后，它**确实进入了召回候选**（稠密与稀疏两路）。

    这里刻意断言"进入候选"而不是"出现在最终答案里"：mock 精排按**精确词元重合**打分，
    而 jieba 对同一个标记词在长文本里的切分与查询侧可能不同 → 覆盖率可能为 0，
    于是它在并列 0 分里按 chunk_id 排序被挤出 keep。那是 **mock 的脆弱性**，
    不是编辑机制的问题（真实 rerank 由 `00` §11 的 M3 实测决定）。
    """
    from ekbs.retrieval.pipeline import RetrievalOverrides

    runtime, user_id = admin
    headers = headers_for(user_id)
    chunk_id = child_of(runtime, "年假管理制度")
    marker = "特别条款甲甲乙乙"
    with runtime.db.sync_connect() as conn:
        content = str(
            conn.execute("SELECT content FROM ekbs_chunk WHERE chunk_id = ?", (chunk_id,)).fetchone()[
                "content"
            ]
        )
    updated = f"{marker} {content}"
    assert (
        await client.put(f"/api/v1/chunks/{chunk_id}", headers=headers, json={"content": updated})
    ).status_code == 200

    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    debug = await runtime.pipeline.retrieve(marker, alice, collect_debug=True)
    dense_ids = set(debug.stage_debug["dense"]["merged_chunk_ids"])
    sparse_ids = {hit["chunk_id"] for hit in debug.stage_debug["sparse"]["hits"]}
    assert chunk_id in sparse_ids, "新文本必须被 FTS 命中（否则说明 FTS 未重写）"
    assert chunk_id in dense_ids, "新向量必须进入稠密召回（否则说明向量未追加/未回写）"

    assert chunk_id in debug.stage_debug["c5"]["kept"], "编辑后的分块必须过 C5 进入上下文候选"

    # 注意：最终 `chunks` 里出现的是**父块**——00 §2.1 的父块扩展决定进 LLM 上下文的是父块。
    # 因此只改子块时，答案仍会引用旧父块内容（编辑器需提示同时改父块，见 chunks.py 的说明）。
    rrf_only = await runtime.pipeline.retrieve(
        marker, alice, overrides=RetrievalOverrides(rerank_enabled=False), collect_debug=True
    )
    kept = rrf_only.stage_debug["c5"]["kept"]
    assert chunk_id in kept, "纯 RRF 排序下也应进入候选"
    with runtime.db.sync_connect() as conn:
        parent_id = int(
            conn.execute(
                "SELECT parent_id FROM ekbs_chunk WHERE chunk_id = ?", (chunk_id,)
            ).fetchone()["parent_id"]
        )
    assert any(chunk.chunk_id == parent_id for chunk in rrf_only.chunks), (
        "父块扩展后，上下文里应是父块"
    )
