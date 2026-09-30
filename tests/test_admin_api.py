"""管理台只读接口与 RBAC（路径/权限点取自 `05` §8.2 的权威映射表）。

三条最要紧的断言：

* **权限点必须真的拦得住** —— 每个角色对每个端点 200/403 都要对（不是"有 token 就放行"）；
* **无凭证 401、权限不足 403** 两者语义分开（前者是没登录，后者是登录了但不够格）；
* ``super_admin`` 是**通配**（`00` §3.3），新增权限点不需要每次都去改授权数据。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from ekbs.api.app import create_app
from tests.conftest import TEST_INTERNAL_TOKEN, user_id_of

#: `05` §8.2 的权威映射：端点 → 权限点
ENDPOINTS = {
    "/api/v1/logs/qa/sessions": "audit:qa:read",
    "/api/v1/audit/security-events": "audit:qa:read_all",
    "/api/v1/logs/doc-access": "audit:trace:read",
    "/api/v1/dashboard/overview": "sys:dashboard:read",
    "/api/v1/system/health": "sys:health:read",
    "/api/v1/system/shards": "sys:health:read",
    "/api/v1/system/alert-rules": "sys:health:read",
    "/api/v1/docs/documents": "doc:document:read",
}


def grant_role(runtime, dingtalk_userid: str, role_code: str) -> int:
    """把某个内置角色授予某演示用户，并返回其 user_id。"""
    user_id = user_id_of(runtime.db, dingtalk_userid)
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_user_role(user_id, role_id, scope_type, scope_id, granted_at) "
            "SELECT ?, role_id, 'global', 0, datetime('now') FROM ekbs_role WHERE code = ? "
            "ON CONFLICT DO NOTHING",
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


def headers_for(user_id: int) -> dict[str, str]:
    return {"X-Internal-Token": TEST_INTERNAL_TOKEN, "X-Internal-User-Id": str(user_id)}


async def test_endpoints_require_credentials(client) -> None:
    for path in ENDPOINTS:
        response = await client.get(path)
        assert response.status_code == 401, path


async def test_end_user_is_forbidden_everywhere(client, seeded) -> None:
    """end_user 只有 qa:chat：所有运维只读接口都必须 403（而不是 200）。"""
    runtime, _ = seeded
    user_id = user_id_of(runtime.db, "demo_alice")
    for path in ENDPOINTS:
        response = await client.get(path, headers=headers_for(user_id))
        assert response.status_code == 403, f"{path} 应 403，实际 {response.status_code}"


async def test_auditor_can_read_audit_but_not_documents(client, seeded) -> None:
    runtime, _ = seeded
    auditor_id = grant_role(runtime, "demo_alice", "auditor")
    for path in ("/api/v1/logs/qa/sessions", "/api/v1/audit/security-events",
                 "/api/v1/logs/doc-access", "/api/v1/dashboard/overview"):
        assert (await client.get(path, headers=headers_for(auditor_id))).status_code == 200, path
    # auditor 不该能看文档清单
    assert (
        await client.get("/api/v1/docs/documents", headers=headers_for(auditor_id))
    ).status_code == 403


async def test_kb_admin_can_read_documents_but_not_security_events(client, seeded) -> None:
    runtime, _ = seeded
    admin_id = grant_role(runtime, "demo_bob", "kb_admin")
    assert (
        await client.get("/api/v1/docs/documents", headers=headers_for(admin_id))
    ).status_code == 200
    assert (
        await client.get("/api/v1/audit/security-events", headers=headers_for(admin_id))
    ).status_code == 403


async def test_super_admin_is_wildcard(client, seeded) -> None:
    """`00` §3.3：super_admin 含全部权限 —— 通配兜底，避免每加一个权限点都要改授权。"""
    runtime, _ = seeded
    root_id = grant_role(runtime, "demo_carol", "super_admin")
    for path in ENDPOINTS:
        assert (await client.get(path, headers=headers_for(root_id))).status_code == 200, path


async def test_documents_list_filters_and_pagination(client, seeded) -> None:
    runtime, _ = seeded
    admin_id = grant_role(runtime, "demo_carol", "kb_admin")
    headers = headers_for(admin_id)
    body = (await client.get("/api/v1/docs/documents", headers=headers)).json()
    assert body["total"] >= 3
    titles = {item["title"] for item in body["items"]}
    assert "年假管理制度" in titles
    assert body["items"][0]["chunk_count"] >= 1
    assert body["items"][0]["shard_id"]

    filtered = (
        await client.get("/api/v1/docs/documents?level=3", headers=headers)
    ).json()
    assert all(item["level"] == 3 for item in filtered["items"])

    searched = (
        await client.get("/api/v1/docs/documents?q=年假", headers=headers)
    ).json()
    assert [item["title"] for item in searched["items"]] == ["年假管理制度"]

    # 分页护栏：limit 再大也不超过 200
    big = (await client.get("/api/v1/docs/documents?limit=100000", headers=headers)).json()
    assert big["limit"] == 200


async def test_document_detail_and_404(client, seeded) -> None:
    runtime, _ = seeded
    admin_id = grant_role(runtime, "demo_carol", "kb_admin")
    headers = headers_for(admin_id)
    listing = (await client.get("/api/v1/docs/documents", headers=headers)).json()
    doc_id = listing["items"][0]["doc_id"]
    detail = (await client.get(f"/api/v1/docs/documents/{doc_id}", headers=headers)).json()
    assert detail["document"]["doc_id"] == doc_id
    assert detail["chunks"].get("child", 0) >= 1
    assert detail["shards"]
    assert (await client.get("/api/v1/docs/documents/999999", headers=headers)).status_code == 404


async def test_qa_session_log_exposes_audit_without_snippet_text(client, seeded) -> None:
    """审计接口回耗时拆解与引用，但**不回片段全文**（01 §5.4 的设计意图）。"""
    runtime, _ = seeded
    runtime.settings.limits = {"global_qps": 0, "per_user_qps": 0, "per_conversation_qps": 0}
    alice_id = user_id_of(runtime.db, "demo_alice")
    asked = await client.post(
        "/api/v1/qa/chat", json={"question": "年假有多少天"},
        headers=headers_for(alice_id),
    )
    session_id = asked.json()["session_id"]

    auditor_id = grant_role(runtime, "demo_bob", "auditor")
    headers = headers_for(auditor_id)
    sessions = (await client.get("/api/v1/logs/qa/sessions", headers=headers)).json()
    assert sessions["total"] >= 1
    assert len(sessions["items"][0]["perm_fingerprint"]) == 12, "指纹只回前 12 位"

    detail = (
        await client.get(f"/api/v1/logs/qa/sessions/{session_id}", headers=headers)
    ).json()
    assert detail["messages"] and detail["messages"][0]["question"] == "年假有多少天"
    assert detail["citations"] and detail["citations"][0]["chunk_id"] > 0
    # 引用行里只有 hash 与页码，没有正文
    citation = detail["citations"][0]
    assert citation.get("snippet_hash")
    assert "content" not in citation and "text" not in citation


async def test_system_health_and_shards_expose_gates(client, seeded) -> None:
    runtime, _ = seeded
    admin_id = grant_role(runtime, "demo_carol", "kb_admin")
    headers = headers_for(admin_id)
    health = (await client.get("/api/v1/system/health", headers=headers)).json()
    assert health["schema"]["ok"] is True and health["schema"]["tables"] == 29
    assert health["embedding"]["fingerprint_match"] is True
    assert health["gates"] == {
        "corpus_egress_ack": False, "l3_ingest_enabled": False, "l4_enabled": False,
    }
    shards = (await client.get("/api/v1/system/shards", headers=headers)).json()
    assert shards["count"] == 3
    assert all(item["self_check_ok"] for item in shards["items"])
    assert all(len(item["embedding_fingerprint"]) == 12 for item in shards["items"])


async def test_dashboard_overview_reports_alerts_and_counts(client, seeded) -> None:
    runtime, _ = seeded
    admin_id = grant_role(runtime, "demo_carol", "kb_admin")
    body = (await client.get("/api/v1/dashboard/overview", headers=headers_for(admin_id))).json()
    assert body["documents"]["published"] >= 3
    assert body["shards"]["active"] == 3 and body["shards"]["vectors"] >= 3
    assert isinstance(body["active_alerts"], list)
    assert isinstance(body["disk_used_pct"], float)


async def test_me_returns_permissions_for_menu_filtering(client, seeded) -> None:
    """`05` §2.1：前端用 ``permissions[]`` 动态生成菜单。"""
    runtime, _ = seeded
    auditor_id = grant_role(runtime, "demo_alice", "auditor")
    body = (await client.get("/api/v1/auth/me", headers=headers_for(auditor_id))).json()
    assert "audit:qa:read" in body["permissions"]
    assert "auditor" in body["roles"]
    assert len(body["perm_fingerprint"]) == 64
    assert body["policy_version"] >= 1

    end_user = user_id_of(runtime.db, "demo_bob")
    plain = (await client.get("/api/v1/auth/me", headers=headers_for(end_user))).json()
    assert plain["permissions"] == ["qa:chat"]
