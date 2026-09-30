"""文档写接口（`05` §8.2）—— 二期里唯一会改动线上检索结果的部分。

最要紧的两条断言：

* **改密级必须迁移分片**（否则低密级用户继续从旧分片召回，这是第 2 轮修过的漏洞，
  这里用 HTTP 端到端把它钉住）；
* **下线/删除必须立刻停止召回**（状态过滤 + 策略版本自增）。
"""

from __future__ import annotations

import io

import pytest
from httpx import ASGITransport, AsyncClient

from ekbs.api.app import create_app
from tests.conftest import TEST_INTERNAL_TOKEN, user_id_of


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
    """carol 有 kb_admin 角色（seed 已授予），返回她的 headers 与 user_id。"""
    runtime, _ = seeded
    user_id = user_id_of(runtime.db, "demo_carol")
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
    response = await client.post(
        "/api/v1/docs/documents",
        headers=headers,
        json={"title": "新制度（待解析）", "kb_id": kb_id, "level": 2, "visible_dept_ids": ["102"]},
    )
    assert response.status_code == 201
    doc_id = response.json()["doc_id"]
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT status, level, visible_dept_ids FROM ekbs_document WHERE doc_id = ?", (doc_id,)
        ).fetchone()
    assert row["status"] == "pending" and row["level"] == 2
    assert "102" in str(row["visible_dept_ids"])


async def test_register_rejects_l4_when_disabled(client, kb_admin) -> None:
    runtime, _, headers = kb_admin
    with runtime.db.sync_connect() as conn:
        kb_id = int(conn.execute("SELECT kb_id FROM ekbs_kb LIMIT 1").fetchone()["kb_id"])
    response = await client.post(
        "/api/v1/docs/documents",
        headers=headers,
        json={"title": "机密", "kb_id": kb_id, "level": 4},
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
    created = await client.post(
        "/api/v1/docs/documents",
        headers=headers, json={"title": "没有分块的文档", "kb_id": kb_id, "level": 1},
    )
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
    response = await client.delete(f"/api/v1/docs/documents/{doc_id}", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["deleted_chunks"] > 0 and "rebuild" in body["hint"]
    with runtime.db.sync_connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM ekbs_chunk WHERE doc_id = ?", (doc_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM ekbs_document WHERE doc_id = ?", (doc_id,)
        ).fetchone()[0] == 0
    assert runtime.fts.count() < fts_before
    assert (await client.get(f"/api/v1/docs/documents/{doc_id}", headers=headers)).status_code == 404


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
        json={"filename": "新制度.md", "size_bytes": len(content), "checksum": checksum, "kb_id": kb_id},
    )
    assert again.json()["instant"] is True
    assert again.json()["doc_id"] == body["doc_id"]


async def test_upload_rejects_bad_extension_and_oversize(client, kb_admin) -> None:
    _, _, headers = kb_admin
    bad_ext = await client.post(
        "/api/v1/docs/upload/init",
        headers=headers, json={"filename": "病毒.exe", "size_bytes": 100},
    )
    assert bad_ext.status_code == 422
    too_big = await client.post(
        "/api/v1/docs/upload/init",
        headers=headers, json={"filename": "大文件.pdf", "size_bytes": 500 * 1024 * 1024},
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
