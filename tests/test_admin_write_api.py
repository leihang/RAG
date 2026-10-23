"""文档写接口（`05` §8.2）—— 二期里唯一会改动线上检索结果的部分。

最要紧的两条断言：

* **改密级必须迁移分片**（否则低密级用户继续从旧分片召回，这是第 2 轮修过的漏洞，
  这里用 HTTP 端到端把它钉住）；
* **下线/删除必须立刻停止召回**（状态过滤 + 策略版本自增）。
"""

from __future__ import annotations

import io
import pathlib
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from ekbs.api.app import create_app
from tests.conftest import TEST_INTERNAL_TOKEN, set_clearance, user_id_of


def headers_for(user_id: int) -> dict[str, str]:
    return {"X-Internal-Token": TEST_INTERNAL_TOKEN, "X-Internal-User-Id": str(user_id)}


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
def kb_admin(seeded):
    """carol 有 kb_admin 角色（seed 已授予），返回她的 headers 与 user_id。

    同时把她的密级提到 L4：上架范围校验要求「文档密级 ≤ 上传者密级」，
    而 seed 里的 carol 是 L1 的行政专员 —— 测试里她是全量知识库管理员。
    """
    runtime, _ = seeded
    user_id = user_id_of(runtime.db, "demo_carol")
    set_clearance(runtime, user_id, 4)
    return runtime, user_id, headers_for(user_id)


def doc_id_of(runtime, title: str) -> int:
    with runtime.db.sync_connect() as conn:
        return int(
            conn.execute("SELECT doc_id FROM ekbs_document WHERE title = ?", (title,)).fetchone()[
                "doc_id"
            ]
        )


def shards_of(runtime, doc_id: int) -> set[str]:
    with runtime.db.sync_connect() as conn:
        return {
            str(r["shard_id"])
            for r in conn.execute(
                "SELECT DISTINCT shard_id FROM ekbs_chunk WHERE doc_id = ?", (doc_id,)
            ).fetchall()
        }


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


async def test_write_endpoints_require_permission(client, seeded) -> None:
    runtime, _ = seeded
    doc_id = doc_id_of(runtime, "年假管理制度")
    end_user = headers_for(user_id_of(runtime.db, "demo_alice"))
    calls = [
        ("post", "/api/v1/docs/documents", {"json": {"title": "x", "kb_id": 1}}),
        ("put", f"/api/v1/docs/documents/{doc_id}", {"json": {"title": "y"}}),
        ("post", f"/api/v1/docs/documents/{doc_id}/publish", {}),
        ("post", f"/api/v1/docs/documents/{doc_id}/offline", {}),
        ("delete", f"/api/v1/docs/documents/{doc_id}", {}),
        ("post", "/api/v1/docs/documents/batch", {"json": {"doc_ids": [doc_id], "action": "publish"}}),
        ("post", "/api/v1/system/index/rebuild", {"json": {}}),
    ]
    for method, path, kwargs in calls:
        response = await getattr(client, method)(path, headers=end_user, **kwargs)
        assert response.status_code == 403, f"{method.upper()} {path} 应 403"
        unauthenticated = await getattr(client, method)(path, **kwargs)
        assert unauthenticated.status_code == 401, f"{method.upper()} {path} 应 401"


# ---------------------------------------------------------------------------
# 登记 / 编辑（含分片迁移）
# ---------------------------------------------------------------------------


async def test_register_document(client, kb_admin) -> None:
    runtime, _, headers = kb_admin
    with runtime.db.sync_connect() as conn:
        kb_id = int(conn.execute("SELECT kb_id FROM ekbs_kb LIMIT 1").fetchone()["kb_id"])
    raw = runtime.settings.data_paths()["raw"]
    source = raw / "登记" / "新制度.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("# 新制度\n\n正文。\n", encoding="utf-8")
    response = await client.post(
        "/api/v1/docs/documents",
        headers=headers,
        json={
            "title": "新制度（待解析）",
            "kb_id": kb_id,
            "level": 2,
            "visible_dept_ids": ["102"],
            "source_path": str(source),
        },
    )
    assert response.status_code == 201, response.text
    doc_id = response.json()["doc_id"]
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT status, level, visible_dept_ids, source_path FROM ekbs_document WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
    assert row["status"] == "pending" and row["level"] == 2
    assert "102" in str(row["visible_dept_ids"])
    assert str(row["source_path"]) == str(source.resolve())


async def test_register_rejects_path_outside_raw(client, kb_admin) -> None:
    runtime, _, headers = kb_admin
    with runtime.db.sync_connect() as conn:
        kb_id = int(conn.execute("SELECT kb_id FROM ekbs_kb LIMIT 1").fetchone()["kb_id"])
    response = await client.post(
        "/api/v1/docs/documents",
        headers=headers,
        json={
            "title": "逃逸",
            "kb_id": kb_id,
            "level": 1,
            "source_path": "/etc/passwd",
        },
    )
    assert response.status_code == 422
    assert "raw" in response.json()["detail"].lower() or "超出" in response.json()["detail"]


async def test_register_rejects_l4_when_disabled(client, kb_admin) -> None:
    runtime, _, headers = kb_admin
    # 显式关闭 L4 闸，不依赖配置文件默认值（2026-10-09 测试期配置已全开）
    runtime.settings.permission.l4_enabled = False
    with runtime.db.sync_connect() as conn:
        kb_id = int(conn.execute("SELECT kb_id FROM ekbs_kb LIMIT 1").fetchone()["kb_id"])
    raw = runtime.settings.data_paths()["raw"]
    source = raw / "登记" / "机密.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("机密", encoding="utf-8")
    response = await client.post(
        "/api/v1/docs/documents",
        headers=headers,
        json={
            "title": "机密",
            "kb_id": kb_id,
            "level": 4,
            "source_path": str(source),
        },
    )
    assert response.status_code == 400
    assert "L4" in response.json()["detail"] or "l4" in response.json()["detail"].lower()


async def test_level_change_migrates_shard_and_closes_leak(client, kb_admin) -> None:
    """**安全回归**：经 HTTP 把文档调到 L3 后，L2 用户不得再召回到它。"""
    runtime, _, headers = kb_admin
    runtime.settings.permission.l3_ingest_enabled = True  # 生产默认 false，测试内开闸
    doc_id = doc_id_of(runtime, "年假管理制度")
    assert shards_of(runtime, doc_id) == {"kb_hr_L2"}

    response = await client.put(
        f"/api/v1/docs/documents/{doc_id}",
        headers=headers,
        params={"reason": "升级为秘密"},
        json={"level": 3},
    )
    assert response.status_code == 200, response.text
    changes = response.json()["changes"]
    assert any(c.get("to_shard") == "kb_hr_L3" for c in changes)
    assert shards_of(runtime, doc_id) == {"kb_hr_L3"}

    # L2 用户（alice）现在看不到这片分片
    alice_id = user_id_of(runtime.db, "demo_alice")
    perm = (await client.get("/api/v1/perm/me", headers=headers_for(alice_id))).json()
    assert "kb_hr_L3" not in perm["visible_shards"]

    # 审计留痕（合规 ≥365 天）
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT change_type, old_value, new_value FROM ekbs_perm_change_log "
            "WHERE object_type = 'document' AND object_id = ? ORDER BY change_id DESC LIMIT 1",
            (str(doc_id),),
        ).fetchone()
    assert row["change_type"] == "level"
    assert (row["old_value"], row["new_value"]) == ("L2", "L3")


async def test_metadata_update_without_level_change_does_not_migrate(client, kb_admin) -> None:
    runtime, _, headers = kb_admin
    doc_id = doc_id_of(runtime, "年假管理制度")
    before = shards_of(runtime, doc_id)
    response = await client.put(
        f"/api/v1/docs/documents/{doc_id}",
        headers=headers,
        json={"title": "年假管理制度（2026 修订）", "effective_date": "2026-01-01"},
    )
    assert response.status_code == 200
    assert shards_of(runtime, doc_id) == before
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT title, effective_date FROM ekbs_document WHERE doc_id = ?", (doc_id,)
        ).fetchone()
    assert row["title"] == "年假管理制度（2026 修订）"
    assert row["effective_date"] == "2026-01-01"


# ---------------------------------------------------------------------------
# 发布 / 下线 / 删除
# ---------------------------------------------------------------------------


async def test_publish_requires_chunks(client, kb_admin) -> None:
    runtime, user_id, headers = kb_admin
    with runtime.db.sync_connect() as conn:
        kb_id = int(conn.execute("SELECT kb_id FROM ekbs_kb LIMIT 1").fetchone()["kb_id"])
    raw = runtime.settings.data_paths()["raw"]
    source = raw / "登记" / "无分块.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("# 空\n", encoding="utf-8")
    created = await client.post(
        "/api/v1/docs/documents",
        headers=headers,
        json={
            "title": "没有分块的文档",
            "kb_id": kb_id,
            "level": 1,
            "source_path": str(source),
        },
    )
    assert created.status_code == 201, created.text
    doc_id = created.json()["doc_id"]
    response = await client.post(f"/api/v1/docs/documents/{doc_id}/publish", headers=headers)
    assert response.status_code == 409
    assert "尚无分块" in response.json()["detail"]


async def test_offline_stops_retrieval_and_bumps_policy(client, kb_admin) -> None:
    runtime, _, headers = kb_admin
    doc_id = doc_id_of(runtime, "年假管理制度")
    with runtime.db.sync_connect() as conn:
        before = int(conn.execute("SELECT version FROM ekbs_policy_version").fetchone()[0])
    response = await client.post(
        f"/api/v1/docs/documents/{doc_id}/offline", headers=headers, json={"reason": "过期"}
    )
    assert response.status_code == 200 and response.json()["status"] == "offline"
    with runtime.db.sync_connect() as conn:
        after = int(conn.execute("SELECT version FROM ekbs_policy_version").fetchone()[0])
    assert after > before, "下线必须让策略版本自增（清掉可能已缓存的结果）"

    # 召回侧立刻停止：FTS 与稠密路径都带 status='published' 过滤
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    result = await runtime.pipeline.retrieve("年假有多少天", alice)
    assert all(chunk.doc_id != doc_id for chunk in result.chunks)


async def test_unpublish_alias_works(client, kb_admin) -> None:
    """05 §8.1 汇总表写 /unpublish，§8.2 全量清单写 /offline —— 两个都支持。"""
    runtime, _, headers = kb_admin
    doc_id = doc_id_of(runtime, "财务保密制度")
    response = await client.post(f"/api/v1/docs/documents/{doc_id}/unpublish", headers=headers, json={})
    assert response.status_code == 200 and response.json()["status"] == "offline"


async def test_delete_removes_chunks_and_fts(client, kb_admin) -> None:
    runtime, _, headers = kb_admin
    doc_id = doc_id_of(runtime, "员工手册（总则）")
    fts_before = runtime.fts.count()
    with runtime.db.sync_connect() as conn:
        vectors_before = int(
            conn.execute("SELECT COALESCE(SUM(vector_count), 0) FROM ekbs_shard").fetchone()[0]
        )
        # 只有**进了索引的块**（faiss_row 非空）才会让分片计数下降；
        # 父块本来就不入向量，所以不能用 deleted_chunks（那是全部块）
        indexed_before = int(
            conn.execute(
                "SELECT COUNT(*) FROM ekbs_chunk WHERE doc_id = ? AND faiss_row IS NOT NULL",
                (doc_id,),
            ).fetchone()[0]
        )
    assert indexed_before > 0, "前置条件：该文档应有已入向量的块"
    response = await client.delete(f"/api/v1/docs/documents/{doc_id}", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "soft"
    assert body["status"] == "deleted"
    assert body["deleted_chunks"] > 0
    # **真删的硬证明**：分片登记的向量数必须下降，而不是原地不动（旧实现留孤儿时它不变）
    with runtime.db.sync_connect() as conn:
        registered = int(
            conn.execute("SELECT COALESCE(SUM(vector_count), 0) FROM ekbs_shard").fetchone()[0]
        )
    assert registered == vectors_before - indexed_before, (
        f"删完分片向量应从 {vectors_before} 降到 {vectors_before - indexed_before}，"
        f"实得 {registered}（相等才说明向量被真删，而不是留成孤儿）"
    )
    with runtime.db.sync_connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM ekbs_chunk WHERE doc_id = ?", (doc_id,)
        ).fetchone()[0] == 0
        row = conn.execute(
            "SELECT status, deleted_at FROM ekbs_document WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        assert row is not None
        assert str(row["status"]) == "deleted"
        assert row["deleted_at"]
    assert runtime.fts.count() < fts_before
    # 软删后详情默认不可见（列表/详情排除 deleted）；恢复后可见
    assert (await client.get(f"/api/v1/docs/documents/{doc_id}", headers=headers)).status_code == 404
    restored = await client.post(f"/api/v1/docs/documents/{doc_id}/restore", headers=headers)
    assert restored.status_code == 200
    assert restored.json()["status"] == "offline"


# ---------------------------------------------------------------------------
# 批量
# ---------------------------------------------------------------------------


async def test_batch_offline_multiple_documents(client, kb_admin) -> None:
    runtime, _, headers = kb_admin
    first = doc_id_of(runtime, "年假管理制度")
    second = doc_id_of(runtime, "财务保密制度")
    response = await client.post(
        "/api/v1/docs/documents/batch",
        headers=headers,
        json={"doc_ids": [first, second], "action": "offline", "reason": "批量下线"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["succeeded"] == 2
    with runtime.db.sync_connect() as conn:
        statuses = {
            str(r["status"])
            for r in conn.execute(
                "SELECT status FROM ekbs_document WHERE doc_id IN (?, ?)", (first, second)
            ).fetchall()
        }
    assert statuses == {"offline"}


async def test_batch_delete_really_deletes(client, kb_admin) -> None:
    """**批量删除必须摘掉索引**（管理台的「删除 / 批量删除」走的都是这个接口）。

    默认软删除：文档行保留为 ``deleted``，向量与分块真的没了。
    """
    runtime, _, headers = kb_admin
    first = doc_id_of(runtime, "年假管理制度")
    second = doc_id_of(runtime, "财务保密制度")
    with runtime.db.sync_connect() as conn:
        vectors_before = int(
            conn.execute("SELECT COALESCE(SUM(vector_count), 0) FROM ekbs_shard").fetchone()[0]
        )
        indexed = int(
            conn.execute(
                "SELECT COUNT(*) FROM ekbs_chunk WHERE doc_id IN (?, ?) AND faiss_row IS NOT NULL",
                (first, second),
            ).fetchone()[0]
        )
    assert indexed > 0, "前置条件：这两篇应有已入向量的块"

    response = await client.post(
        "/api/v1/docs/documents/batch",
        headers=headers,
        json={"doc_ids": [first, second], "action": "delete", "reason": "制度废止，批量清理"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["succeeded"] == 2
    assert [item["status"] for item in body["results"]] == ["deleted", "deleted"]

    with runtime.db.sync_connect() as conn:
        statuses = {
            str(r["status"])
            for r in conn.execute(
                "SELECT status FROM ekbs_document WHERE doc_id IN (?, ?)", (first, second)
            ).fetchall()
        }
        assert statuses == {"deleted"}
        assert conn.execute(
            "SELECT COUNT(*) FROM ekbs_chunk WHERE doc_id IN (?, ?)", (first, second)
        ).fetchone()[0] == 0
        registered = int(
            conn.execute("SELECT COALESCE(SUM(vector_count), 0) FROM ekbs_shard").fetchone()[0]
        )
    # 与单篇删除同口径：向量被真删，登记数下降而不是留孤儿
    assert registered == vectors_before - indexed, (
        f"批量删完向量应从 {vectors_before} 降到 {vectors_before - indexed}，实得 {registered}"
    )
    # 同一批里已软删的 id 再软删一次 → 这一条失败、不拖垮整批
    again = await client.post(
        "/api/v1/docs/documents/batch",
        headers=headers,
        json={"doc_ids": [first], "action": "delete", "reason": "重复删除应被如实报失败"},
    )
    assert again.status_code == 200
    assert again.json()["succeeded"] == 0
    assert again.json()["results"][0]["ok"] is False


async def test_hard_delete_removes_disk_artifacts(client, kb_admin, tmp_path) -> None:
    """``hard=true`` 时连磁盘原件一起清。"""
    runtime, _, headers = kb_admin
    with runtime.db.sync_connect() as conn:
        row = conn.execute("SELECT doc_id FROM ekbs_document ORDER BY doc_id LIMIT 1").fetchone()
    doc_id = int(row["doc_id"])
    # 合成语料的 source_path 是 `demo://...`（磁盘上并没有文件），这里造一个真原件放进 raw
    source = runtime.settings.data_paths()["raw"] / "测试库" / "待删文档.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("待删内容", encoding="utf-8")
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_document SET source_path = ? WHERE doc_id = ?", (str(source), doc_id)
        )
        conn.commit()
    assert source.is_file(), f"前置条件：原件应存在（{source}）"
    parse_dir = runtime.settings.data_paths()["parse"] / str(doc_id)
    parse_dir.mkdir(parents=True, exist_ok=True)
    (parse_dir / "images").mkdir(exist_ok=True)
    (parse_dir / "images" / "x.jpg").write_bytes(b"fake")

    response = await client.delete(
        f"/api/v1/docs/documents/{doc_id}?hard=true", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "hard"
    assert body["assets_removed"], body
    assert not source.exists(), "原件应被删除"
    assert not parse_dir.exists(), "解析产物目录应被删除"

    # 同目录下**别人的**文件不能被牵连（这里放一个同目录的邻居文件）
    sibling = source.parent / "别人的文件.md"
    sibling.write_text("不该被删", encoding="utf-8")
    with runtime.db.sync_connect() as conn:
        other = conn.execute(
            "SELECT doc_id FROM ekbs_document ORDER BY doc_id LIMIT 1"
        ).fetchone()
    if other is not None:
        await client.delete(
            f"/api/v1/docs/documents/{int(other['doc_id'])}?hard=true", headers=headers
        )
    assert sibling.is_file(), "删一份文档不能带走同目录下别的文件"
    sibling.unlink()


async def test_hard_delete_honours_purge_assets_false(client, kb_admin) -> None:
    """``hard=true&purge_assets=false`` 时删库行与向量，磁盘原件保留。"""
    runtime, _, headers = kb_admin
    with runtime.db.sync_connect() as conn:
        row = conn.execute("SELECT doc_id FROM ekbs_document ORDER BY doc_id LIMIT 1").fetchone()
    doc_id = int(row["doc_id"])
    source = runtime.settings.data_paths()["raw"] / "测试库" / "留档文档.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("留档内容", encoding="utf-8")
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_document SET source_path = ? WHERE doc_id = ?", (str(source), doc_id)
        )
        conn.commit()

    response = await client.delete(
        f"/api/v1/docs/documents/{doc_id}?hard=true&purge_assets=false", headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["assets_removed"] == []
    assert source.is_file(), "purge_assets=false 时必须保留原件"
    with runtime.db.sync_connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM ekbs_document WHERE doc_id = ?", (doc_id,)
        ).fetchone()[0] == 0


async def test_soft_delete_keeps_raw_but_purges_parse_and_captions(client, kb_admin) -> None:
    """软删除：原件迁入 trash/；解析图与图注缓存立刻清（图是文档内容）。"""
    runtime, _, headers = kb_admin
    with runtime.db.sync_connect() as conn:
        row = conn.execute("SELECT doc_id FROM ekbs_document ORDER BY doc_id LIMIT 1").fetchone()
    doc_id = int(row["doc_id"])
    raw = runtime.settings.data_paths()["raw"]
    trash = runtime.settings.data_paths()["trash"]
    parse = runtime.settings.data_paths()["parse"]
    source = raw / "测试库" / "软删保留.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("软删保留", encoding="utf-8")
    parse_dir = parse / str(doc_id) / "images"
    parse_dir.mkdir(parents=True, exist_ok=True)
    (parse_dir / "img-deadbeef.png").write_bytes(b"fake-png")
    caption = runtime.settings.data_dir / "cache" / "figure-captions" / "img-deadbeef.json"
    caption.parent.mkdir(parents=True, exist_ok=True)
    caption.write_text('{"caption":"测试图注"}', encoding="utf-8")
    rel_image = str((parse / str(doc_id) / "images" / "img-deadbeef.png").relative_to(runtime.settings.data_dir))
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_document SET source_path = ? WHERE doc_id = ?", (str(source), doc_id)
        )
        # 挂一条 figure 块，删除链路靠 image_path 找图注 stem
        conn.execute(
            "INSERT INTO ekbs_chunk(doc_id, chunk_type, seq, content, shard_id, image_path) "
            "VALUES (?, 'figure', 9999, '图', 'kb_all_L1', ?)",
            (doc_id, rel_image),
        )
        conn.commit()

    response = await client.delete(f"/api/v1/docs/documents/{doc_id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["mode"] == "soft"
    assert not source.exists(), "软删后原件不应再留在 raw/"
    quarantined = trash / str(doc_id) / "测试库" / "软删保留.md"
    assert quarantined.is_file(), f"原件应迁入 trash/（期望 {quarantined}）"
    with runtime.db.sync_connect() as conn:
        sp = conn.execute(
            "SELECT source_path FROM ekbs_document WHERE doc_id = ?", (doc_id,)
        ).fetchone()["source_path"]
    assert Path(str(sp)).resolve() == quarantined.resolve()
    assert not (parse / str(doc_id)).exists(), "软删应立刻清 parse/<doc_id>"
    assert not caption.exists(), "软删应立刻清图注缓存"

    # 恢复：迁回 raw，status=offline
    restored = await client.post(f"/api/v1/docs/documents/{doc_id}/restore", headers=headers)
    assert restored.status_code == 200
    assert restored.json()["status"] == "offline"
    assert source.is_file(), "恢复后原件应回到 raw/"
    assert not quarantined.exists(), "恢复后 trash 副本应搬走"
    with runtime.db.sync_connect() as conn:
        sp2 = conn.execute(
            "SELECT source_path FROM ekbs_document WHERE doc_id = ?", (doc_id,)
        ).fetchone()["source_path"]
    assert Path(str(sp2)).resolve() == source.resolve()


async def test_hard_delete_purges_caption_cache_and_sha_parse(client, kb_admin) -> None:
    """硬删：parse/sha-*、parse/<doc_id>、图注缓存与原件一并清。"""
    runtime, _, headers = kb_admin
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT doc_id, checksum FROM ekbs_document ORDER BY doc_id LIMIT 1"
        ).fetchone()
    doc_id = int(row["doc_id"])
    checksum = str(row["checksum"] or "abcdef0123456789ffff")
    if len(checksum) < 16:
        checksum = (checksum + "0" * 16)[:32]
        with runtime.db.sync_connect() as conn:
            conn.execute(
                "UPDATE ekbs_document SET checksum = ? WHERE doc_id = ?", (checksum, doc_id)
            )
            conn.commit()

    raw = runtime.settings.data_paths()["raw"]
    parse = runtime.settings.data_paths()["parse"]
    source = raw / "测试库" / "硬删清缓存.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("硬删", encoding="utf-8")
    sha_dir = parse / f"sha-{checksum[:16]}" / "images"
    sha_dir.mkdir(parents=True, exist_ok=True)
    (sha_dir / "img-cafef00d.png").write_bytes(b"x")
    caption = runtime.settings.data_dir / "cache" / "figure-captions" / "img-cafef00d.json"
    caption.parent.mkdir(parents=True, exist_ok=True)
    caption.write_text('{"caption":"sha图"}', encoding="utf-8")
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_document SET source_path = ? WHERE doc_id = ?", (str(source), doc_id)
        )
        conn.commit()

    response = await client.delete(
        f"/api/v1/docs/documents/{doc_id}?hard=true", headers=headers
    )
    assert response.status_code == 200, response.text
    assert not source.exists()
    assert not (parse / f"sha-{checksum[:16]}").exists()
    assert not caption.exists()



async def test_delete_never_touches_files_outside_raw(client, kb_admin, tmp_path) -> None:
    """**越界保护**：source_path 指向 raw 之外时只告警、绝不 unlink。

    没有这道判断，一行被篡改/历史遗留的 source_path 就能把删除接口变成任意文件删除。
    """
    runtime, _, headers = kb_admin
    outside = tmp_path / "重要文件.txt"
    outside.write_text("不在 raw 里", encoding="utf-8")
    with runtime.db.sync_connect() as conn:
        row = conn.execute("SELECT doc_id FROM ekbs_document ORDER BY doc_id LIMIT 1").fetchone()
        doc_id = int(row["doc_id"])
        conn.execute(
            "UPDATE ekbs_document SET source_path = ? WHERE doc_id = ?",
            (str(outside), doc_id),
        )
        conn.commit()

    response = await client.delete(
        f"/api/v1/docs/documents/{doc_id}?hard=true", headers=headers
    )
    assert response.status_code == 200, response.text
    assert outside.is_file(), "raw 外的路径绝不能被 unlink"
    outside.unlink()


async def test_batch_rejects_unknown_action(client, kb_admin) -> None:
    _, _, headers = kb_admin
    response = await client.post(
        "/api/v1/docs/documents/batch",
        headers=headers, json={"doc_ids": [1], "action": "explode"},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 上传（含秒传）
# ---------------------------------------------------------------------------


async def test_upload_flow_creates_pending_document(client, kb_admin) -> None:
    runtime, user_id, headers = kb_admin
    with runtime.db.sync_connect() as conn:
        kb_id = int(conn.execute("SELECT kb_id FROM ekbs_kb LIMIT 1").fetchone()["kb_id"])
    content = "# 新上传的制度\n\n## 第一条\n\n正文内容。\n".encode("utf-8")
    import hashlib

    checksum = hashlib.sha256(content).hexdigest()

    init = await client.post(
        "/api/v1/docs/upload/init",
        headers=headers,
        json={"filename": "新制度.md", "size_bytes": len(content), "checksum": checksum,
              "kb_id": kb_id, "level": 2, "visible_dept_ids": ["102"]},
    )
    assert init.status_code == 200
    assert init.json()["instant"] is False
    upload_id = init.json()["upload_id"]

    chunk = await client.post(
        f"/api/v1/docs/upload/{upload_id}/chunk",
        headers=headers,
        files={"file": ("blob", io.BytesIO(content), "application/octet-stream")},
        data={"index": "0"},
    )
    assert chunk.status_code == 200 and chunk.json()["received"] == 1

    done = await client.post(f"/api/v1/docs/upload/{upload_id}/complete", headers=headers)
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["status"] == "pending" and body["checksum"] == checksum
    assert __import__("pathlib").Path(body["path"]).is_file()

    # 秒传：同样的 checksum 再 init 应直接命中已有文档
    again = await client.post(
        "/api/v1/docs/upload/init",
        headers=headers,
        json={"filename": "新制度.md", "size_bytes": len(content), "checksum": checksum,
              "kb_id": kb_id, "level": 2, "visible_dept_ids": ["102"]},
    )
    assert again.json()["instant"] is True
    assert again.json()["doc_id"] == body["doc_id"]


async def test_upload_missing_chunk_is_rejected_without_checksum(client, kb_admin) -> None:
    """**内网 http 场景**：没有客户端校验和时，缺片必须被服务端拦住。

    浏览器算 SHA-256 依赖 `crypto.subtle`，它只在 https/localhost 可用；内网
    `http://<ip>:8000/admin` 下 checksum 恒为空，校验和那道闸会被跳过。
    此时若连字节数都不比，少传的片会静默入库 —— 对 .md/.txt 就是丢内容。
    """
    runtime, _user_id, headers = kb_admin
    with runtime.db.sync_connect() as conn:
        kb_id = int(conn.execute("SELECT kb_id FROM ekbs_kb LIMIT 1").fetchone()["kb_id"])

    whole = b"A" * 300
    init = await client.post(
        "/api/v1/docs/upload/init",
        headers=headers,
        json={"filename": "缺片.md", "size_bytes": len(whole), "checksum": None,
              "kb_id": kb_id, "level": 2, "visible_dept_ids": ["102"]},
    )
    assert init.status_code == 200, init.text
    upload_id = init.json()["upload_id"]

    # 只传前 100 字节（= 少了两片），且**不带任何校验和**
    chunk = await client.post(
        f"/api/v1/docs/upload/{upload_id}/chunk",
        headers=headers,
        files={"file": ("blob", io.BytesIO(whole[:100]), "application/octet-stream")},
        data={"index": "0"},
    )
    assert chunk.status_code == 200

    done = await client.post(f"/api/v1/docs/upload/{upload_id}/complete", headers=headers)
    assert done.status_code == 422, done.text
    assert "分片不完整" in done.json()["detail"]
    # 被拦下的上传不该留下文档行
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT count(*) AS c FROM ekbs_document WHERE title = '缺片'"
        ).fetchone()["c"]
    assert rows == 0


async def test_upload_same_chunk_retry_is_idempotent(client, kb_admin) -> None:
    """重传同一 index 必须**覆盖**而不是追加。

    界面有"失败自动重试 3 次"：如果服务端是 append 语义，一次超时重试就会把
    同一段内容写两遍，合并出比原文件更长、内容错位的文件。
    """
    runtime, _user_id, headers = kb_admin
    with runtime.db.sync_connect() as conn:
        kb_id = int(conn.execute("SELECT kb_id FROM ekbs_kb LIMIT 1").fetchone()["kb_id"])

    import hashlib

    first, second = b"X" * 120, b"Y" * 120
    whole = first + second
    init = await client.post(
        "/api/v1/docs/upload/init",
        headers=headers,
        json={"filename": "重试.md", "size_bytes": len(whole),
              "checksum": hashlib.sha256(whole).hexdigest(),
              "kb_id": kb_id, "level": 2, "visible_dept_ids": ["102"]},
    )
    upload_id = init.json()["upload_id"]

    for index, blob in ((0, first), (1, second)):
        r = await client.post(
            f"/api/v1/docs/upload/{upload_id}/chunk",
            headers=headers,
            files={"file": ("blob", io.BytesIO(blob), "application/octet-stream")},
            data={"index": str(index)},
        )
        assert r.status_code == 200
    # 第 1 片超时重传（同样的 index 再来一次）
    again = await client.post(
        f"/api/v1/docs/upload/{upload_id}/chunk",
        headers=headers,
        files={"file": ("blob", io.BytesIO(second), "application/octet-stream")},
        data={"index": "1"},
    )
    assert again.status_code == 200 and again.json()["received"] == 2, "重传不该把片数算成 3"

    done = await client.post(f"/api/v1/docs/upload/{upload_id}/complete", headers=headers)
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["checksum"] == hashlib.sha256(whole).hexdigest(), "合并结果被写重了"
    assert pathlib.Path(body["path"]).stat().st_size == len(whole)


async def test_upload_size_match_but_content_changed_still_caught(client, kb_admin) -> None:
    """字节数一致、内容不同：这道只能靠校验和抓（有 https/localhost 时才有）。"""
    runtime, _user_id, headers = kb_admin
    with runtime.db.sync_connect() as conn:
        kb_id = int(conn.execute("SELECT kb_id FROM ekbs_kb LIMIT 1").fetchone()["kb_id"])

    import hashlib

    real = "正确的内容".encode("utf-8") * 10
    tampered = "被改的内容".encode("utf-8") * 10
    assert len(real) == len(tampered)
    init = await client.post(
        "/api/v1/docs/upload/init",
        headers=headers,
        json={"filename": "被改.md", "size_bytes": len(real),
              "checksum": hashlib.sha256(real).hexdigest(),
              "kb_id": kb_id, "level": 2, "visible_dept_ids": ["102"]},
    )
    upload_id = init.json()["upload_id"]
    await client.post(
        f"/api/v1/docs/upload/{upload_id}/chunk",
        headers=headers,
        files={"file": ("blob", io.BytesIO(tampered), "application/octet-stream")},
        data={"index": "0"},
    )
    done = await client.post(f"/api/v1/docs/upload/{upload_id}/complete", headers=headers)
    assert done.status_code == 422
    assert "校验和不匹配" in done.json()["detail"]


async def test_upload_rejects_bad_extension_and_oversize(client, kb_admin) -> None:
    _, _, headers = kb_admin
    bad_ext = await client.post(
        "/api/v1/docs/upload/init",
        headers=headers, json={"filename": "病毒.exe", "size_bytes": 100},
    )
    assert bad_ext.status_code == 422
    too_big = await client.post(
        "/api/v1/docs/upload/init",
        headers=headers, json={"filename": "大文件.pdf", "size_bytes": 501 * 1024 * 1024},
    )
    assert too_big.status_code == 413


# ---------------------------------------------------------------------------
# 索引重建
# ---------------------------------------------------------------------------


async def test_index_rebuild_requires_super_admin(client, seeded, kb_admin) -> None:
    """`05` §3.4 的角色矩阵里 ``sys:index:rebuild`` **只有 super_admin**（SA ✔ | KA — | DR — | AU —）。

    重建索引会重写整个分片的向量，影响面最大，因此门槛最高 —— 这条口径值得显式钉住。
    """
    runtime, carol_id, kb_admin_headers = kb_admin
    denied = await client.post(
        "/api/v1/system/index/rebuild", headers=kb_admin_headers,
        json={"shard_id": "kb_hr_L2"},
    )
    assert denied.status_code == 403
    assert "sys:index:rebuild" in denied.json()["detail"]

    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO ekbs_user_role(user_id, role_id, scope_type, scope_id) "
            "SELECT ?, role_id, 'global', 0 FROM ekbs_role WHERE code = 'super_admin'",
            (carol_id,),
        )
        conn.commit()
    allowed = await client.post(
        "/api/v1/system/index/rebuild", headers=kb_admin_headers,
        json={"shard_id": "kb_hr_L2", "dry_run": True},
    )
    assert allowed.status_code == 200
    assert allowed.json()["results"][0]["ok"] is True


async def test_index_rebuild_dry_run_and_real(client, kb_admin) -> None:
    runtime, carol_id, headers = kb_admin
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO ekbs_user_role(user_id, role_id, scope_type, scope_id) "
            "SELECT ?, role_id, 'global', 0 FROM ekbs_role WHERE code = 'super_admin'",
            (carol_id,),
        )
        conn.commit()

    dry = await client.post(
        "/api/v1/system/index/rebuild", headers=headers, json={"shard_id": "kb_hr_L2", "dry_run": True}
    )
    assert dry.status_code == 200
    assert dry.json()["results"][0]["chunks"] >= 1

    real = await client.post(
        "/api/v1/system/index/rebuild", headers=headers, json={"shard_id": "kb_hr_L2"}
    )
    assert real.status_code == 200
    result = real.json()["results"][0]
    assert result["ok"] is True and result["vectors"] >= 1
    # 重建后登记值仍然自洽（冒烟会比对 ntotal 与登记值）
    check = {c.shard_id: c for c in runtime.shards.self_check()}
    assert check["kb_hr_L2"].ok, check["kb_hr_L2"].detail


async def test_index_rebuild_unknown_shard(client, kb_admin) -> None:
    runtime, carol_id, headers = kb_admin
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO ekbs_user_role(user_id, role_id, scope_type, scope_id) "
            "SELECT ?, role_id, 'global', 0 FROM ekbs_role WHERE code = 'super_admin'",
            (carol_id,),
        )
        conn.commit()
    response = await client.post(
        "/api/v1/system/index/rebuild", headers=headers, json={"shard_id": "kb_nope_L2"}
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["ok"] is False
