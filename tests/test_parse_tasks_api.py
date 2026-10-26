"""解析任务中心（`05` §8.2 的 ``/api/v1/parse/tasks/*``）。

盯三件事：权限点按 `05` 区分（read / reparse / manage）、
状态机的边界（不能重排进行中的任务）、以及 Schema 缺口的**诚实交代**
（没有 error 列、没有 cancelled 状态 —— 接口不编造字段）。
"""

from __future__ import annotations

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


def make_pending(runtime, *, title: str, status: str = "pending") -> int:
    with runtime.db.sync_connect() as conn:
        kb_id = int(conn.execute("SELECT kb_id FROM ekbs_kb LIMIT 1").fetchone()["kb_id"])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, status, level) "
            "VALUES (?, ?, ?, ?, 2)",
            (kb_id, title, f"demo://{title}", status),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.commit()
    return doc_id


async def test_parse_endpoints_require_permission(client, seeded) -> None:
    runtime, _ = seeded
    doc_id = make_pending(runtime, title="待解析")
    end_user = headers_for(user_id_of(runtime.db, "demo_alice"))
    for method, path, kwargs in (
        ("get", "/api/v1/parse/tasks", {}),
        ("get", f"/api/v1/parse/tasks/{doc_id}", {}),
        ("get", f"/api/v1/parse/tasks/{doc_id}/result", {}),
        ("get", f"/api/v1/parse/tasks/{doc_id}/draft", {}),
        ("get", "/api/v1/parse/config", {}),
        ("post", f"/api/v1/parse/tasks/{doc_id}/retry", {}),
        ("post", f"/api/v1/parse/tasks/{doc_id}/cancel", {}),
        ("post", f"/api/v1/parse/tasks/{doc_id}/approve", {}),
        ("post", f"/api/v1/parse/tasks/{doc_id}/reject", {}),
    ):
        assert (await getattr(client, method)(path, headers=end_user, **kwargs)).status_code == 403
        assert (await getattr(client, method)(path, **kwargs)).status_code == 401


async def test_list_tasks_reports_success_rate(client, seeded) -> None:
    """`01` §12 的 98% 验收线要能一眼看到。"""
    runtime, _ = seeded
    auditor_id = grant(runtime, "demo_alice", "auditor")
    make_pending(runtime, title="待解析甲")
    make_pending(runtime, title="解析失败乙", status="failed")
    body = (
        await client.get("/api/v1/parse/tasks", headers=headers_for(auditor_id))
    ).json()
    assert body["total"] >= 5
    assert body["by_status"]["published"] >= 3
    assert body["by_status"]["pending"] >= 1
    assert body["success_rate_target"] == 0.98
    assert 0 < body["success_rate"] <= 1
    assert "task_id" in body["items"][0]

    filtered = (
        await client.get("/api/v1/parse/tasks?status=failed", headers=headers_for(auditor_id))
    ).json()
    assert all(item["status"] == "failed" for item in filtered["items"])

    invalid = await client.get(
        "/api/v1/parse/tasks?status=nonsense", headers=headers_for(auditor_id)
    )
    assert invalid.status_code == 422


async def test_task_detail_explains_missing_error_column(client, seeded) -> None:
    """Schema 没有 error 列（docs/spec/20 的 T13）：接口不编造，而是指向日志。"""
    runtime, _ = seeded
    admin_id = grant(runtime, "demo_carol", "kb_admin")
    doc_id = make_pending(runtime, title="失败的文档", status="failed")
    body = (
        await client.get(f"/api/v1/parse/tasks/{doc_id}", headers=headers_for(admin_id))
    ).json()
    assert body["task"]["task_id" if "task_id" in body["task"] else "doc_id"] == doc_id
    assert "error" not in body["task"], "不得凭空造一个 Schema 里没有的字段"
    assert "worker" in body["failure_hint"]
    assert body["state_machine"] == [
        "pending",
        "parsing",
        "review",
        "chunked",
        "indexing",
        "published",
        "failed",
        "offline",
    ]


async def test_task_result_returns_structure_and_preview(client, seeded) -> None:
    runtime, _ = seeded
    admin_id = grant(runtime, "demo_carol", "kb_admin")
    with runtime.db.sync_connect() as conn:
        doc_id = int(
            conn.execute(
                "SELECT doc_id FROM ekbs_document WHERE status='published' LIMIT 1"
            ).fetchone()["doc_id"]
        )
    body = (
        await client.get(
            f"/api/v1/parse/tasks/{doc_id}/result?preview_chars=200",
            headers=headers_for(admin_id),
        )
    ).json()
    assert body["chunk_count"] >= 1
    assert any(item["chunk_type"] == "parent" for item in body["structure"])
    assert body["preview"]
    assert body["preview_truncated"] is True


async def test_retry_requeues_failed_task(client, seeded) -> None:
    runtime, _ = seeded
    admin_id = grant(runtime, "demo_carol", "kb_admin")
    doc_id = make_pending(runtime, title="待重试", status="failed")
    response = await client.post(
        f"/api/v1/parse/tasks/{doc_id}/retry", headers=headers_for(admin_id)
    )
    assert response.status_code == 200
    assert response.json()["previous_status"] == "failed"
    with runtime.db.sync_connect() as conn:
        assert (
            conn.execute("SELECT status FROM ekbs_document WHERE doc_id = ?", (doc_id,)).fetchone()[
                "status"
            ]
            == "pending"
        )
        row = conn.execute(
            "SELECT change_type FROM ekbs_perm_change_log WHERE object_type='document' "
            "AND object_id = ? ORDER BY change_id DESC LIMIT 1",
            (str(doc_id),),
        ).fetchone()
    assert row["change_type"] == "reparse"


async def test_retry_rejects_in_flight_and_published(client, seeded) -> None:
    runtime, _ = seeded
    admin_id = grant(runtime, "demo_carol", "kb_admin")
    headers = headers_for(admin_id)
    parsing = make_pending(runtime, title="解析中", status="parsing")
    assert (
        await client.post(f"/api/v1/parse/tasks/{parsing}/retry", headers=headers)
    ).status_code == 409
    with runtime.db.sync_connect() as conn:
        published = int(
            conn.execute(
                "SELECT doc_id FROM ekbs_document WHERE status='published' LIMIT 1"
            ).fetchone()["doc_id"]
        )
    assert (
        await client.post(f"/api/v1/parse/tasks/{published}/retry", headers=headers)
    ).status_code == 409


async def test_cancel_only_for_pending_and_explains_schema_gap(client, seeded) -> None:
    """Schema 无 cancelled 状态 —— 取消映射为 failed，并在响应里说明。"""
    runtime, _ = seeded
    admin_id = grant(runtime, "demo_carol", "kb_admin")
    headers = headers_for(admin_id)
    doc_id = make_pending(runtime, title="要取消的", status="pending")
    response = await client.post(f"/api/v1/parse/tasks/{doc_id}/cancel", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed" and "cancelled" in body["note"]

    # 已在解析的不能取消
    parsing = make_pending(runtime, title="解析中不可取消", status="parsing")
    assert (
        await client.post(f"/api/v1/parse/tasks/{parsing}/cancel", headers=headers)
    ).status_code == 409


async def test_published_draft_is_readable(client, seeded) -> None:
    """已入库的文档仍要能打开原件对照，不能只在待复核时读得到清洗稿。"""
    from ekbs.ingest.scan_review import save_review_draft

    runtime, _ = seeded
    admin_id = grant(runtime, "demo_carol", "kb_admin")
    headers = headers_for(admin_id)
    pending = make_pending(runtime, title="还没解析", status="pending")
    assert (await client.get(f"/api/v1/parse/tasks/{pending}/draft", headers=headers)).status_code == 409

    doc_id = make_pending(runtime, title="已发布对照", status="published")
    save_review_draft(
        data_dir=runtime.settings.data_dir,
        doc_id=doc_id,
        markdown="确认过的清洗稿",
        engine="docx",
    )
    resp = await client.get(f"/api/v1/parse/tasks/{doc_id}/draft", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "published"
    assert body["read_only"] is True
    assert "确认过的清洗稿" in body["markdown"]


async def test_parse_config_is_read_only(client, seeded) -> None:
    runtime, _ = seeded
    admin_id = grant(runtime, "demo_carol", "kb_admin")
    body = (await client.get("/api/v1/parse/config", headers=headers_for(admin_id))).json()
    assert body["engine"] == "mineru" and body["worker_concurrency"] == 1
    assert body["writable_via_api"] is False
    # 没有 PUT 路由（刻意不暴露：配置走 Git 评审）
    put = await client.put("/api/v1/parse/config", headers=headers_for(admin_id), json={})
    assert put.status_code in {404, 405}


async def test_auditor_can_read_but_not_manage(client, seeded) -> None:
    """`05` §3.4 矩阵：parse:task:read 给 AU，parse:task:manage 不给。"""
    runtime, _ = seeded
    auditor_id = grant(runtime, "demo_bob", "auditor")
    headers = headers_for(auditor_id)
    doc_id = make_pending(runtime, title="审计视角")
    assert (await client.get("/api/v1/parse/tasks", headers=headers)).status_code == 200
    assert (await client.get("/api/v1/parse/config", headers=headers)).status_code == 200
    assert (
        await client.post(f"/api/v1/parse/tasks/{doc_id}/cancel", headers=headers)
    ).status_code == 403
