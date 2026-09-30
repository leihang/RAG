"""权限管理页（`05` §8.2 的 ``/api/v1/perm/*``）。

最要紧的两条：**改密级与改 ACL 必须走权限变更服务**（策略版本 + 缓存失效 + 审计一体），
以及**权限模拟必须留痕**（它是以他人身份看权限，属高危操作）。
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


@pytest.fixture
def root(seeded):
    """carol 提升为 super_admin：权限管理页里多数写操作的唯一持有者。"""
    runtime, _ = seeded
    return runtime, grant(runtime, "demo_carol", "super_admin")


@pytest.fixture
def kb_admin(seeded):
    runtime, _ = seeded
    return runtime, grant(runtime, "demo_bob", "kb_admin")


async def test_permission_matrix_for_read_endpoints(client, seeded) -> None:
    """`05` §3.4：读类（user/role/dept/acl/audit）给 SA/KA/AU，不给 DR/EU。"""
    runtime, _ = seeded
    end_user = headers_for(user_id_of(runtime.db, "demo_alice"))
    for path in ("/api/v1/perm/roles", "/api/v1/perm/users", "/api/v1/perm/depts",
                 "/api/v1/perm/acl", "/api/v1/perm/audit"):
        assert (await client.get(path, headers=end_user)).status_code == 403
        assert (await client.get(path)).status_code == 401

    auditor = headers_for(grant(runtime, "demo_alice", "auditor"))
    for path in ("/api/v1/perm/roles", "/api/v1/perm/users", "/api/v1/perm/depts",
                 "/api/v1/perm/acl", "/api/v1/perm/audit"):
        assert (await client.get(path, headers=auditor)).status_code == 200, path


async def test_role_crud_and_builtin_protection(client, root) -> None:
    runtime, root_id = root
    headers = headers_for(root_id)
    created = await client.post(
        "/api/v1/perm/roles",
        headers=headers,
        json={"code": "hr_editor", "name": "HR 编辑", "permissions": ["doc:document:read", "audit:qa:chat"]},
    )
    assert created.status_code == 201, created.text
    role_id = created.json()["role_id"]
    assert created.json()["permissions"] == 2

    # 未知权限点必须报错（防止静默漏配）
    bad = await client.post(
        "/api/v1/perm/roles", headers=headers,
        json={"code": "bad_role", "name": "坏角色", "permissions": ["no:such:perm"]},
    )
    assert bad.status_code == 422

    listing = (await client.get("/api/v1/perm/roles", headers=headers)).json()
    entry = next(item for item in listing["items"] if item["role_id"] == role_id)
    assert sorted(entry["permissions"]) == ["audit:qa:chat", "doc:document:read"]

    # 编辑权限点 → 策略版本自增（权限点集合变了，缓存必须全量失效）
    with runtime.db.sync_connect() as conn:
        before = int(conn.execute("SELECT version FROM ekbs_policy_version").fetchone()[0])
    updated = await client.put(
        f"/api/v1/perm/roles/{role_id}", headers=headers,
        json={"name": "HR 编辑（v2）", "permissions": ["doc:document:read"]},
    )
    assert updated.status_code == 200
    with runtime.db.sync_connect() as conn:
        after = int(conn.execute("SELECT version FROM ekbs_policy_version").fetchone()[0])
    assert after > before

    # 内置角色不可删
    builtin_id = next(
        item["role_id"] for item in listing["items"] if item["code"] == "kb_admin"
    )
    denied = await client.delete(f"/api/v1/perm/roles/{builtin_id}", headers=headers)
    assert denied.status_code == 409 and "内置角色" in denied.json()["detail"]

    assert (await client.delete(f"/api/v1/perm/roles/{role_id}", headers=headers)).status_code == 200


async def test_assign_role_changes_user_permissions(client, root) -> None:
    runtime, root_id = root
    headers = headers_for(root_id)
    alice = user_id_of(runtime.db, "demo_alice")
    assert "audit:qa:read" not in (await client.get("/api/v1/auth/me", headers=headers_for(alice))).json()["permissions"]

    listing = (await client.get("/api/v1/perm/roles", headers=headers)).json()
    auditor_id = next(item["role_id"] for item in listing["items"] if item["code"] == "auditor")
    assigned = await client.post(
        f"/api/v1/perm/roles/{auditor_id}/assign", headers=headers, json={"user_ids": [alice]}
    )
    assert assigned.status_code == 200 and assigned.json()["granted"] == 1

    me = (await client.get("/api/v1/auth/me", headers=headers_for(alice))).json()
    assert "audit:qa:read" in me["permissions"]

    # 幂等：再分配一次不应重复授予
    again = await client.post(
        f"/api/v1/perm/roles/{auditor_id}/assign", headers=headers, json={"user_ids": [alice]}
    )
    assert again.json()["granted"] == 0


async def test_clearance_change_goes_through_change_service(client, root) -> None:
    """改密级必须自增策略版本 + 写审计 + 立刻影响可见分片。"""
    runtime, root_id = root
    headers = headers_for(root_id)
    alice = user_id_of(runtime.db, "demo_alice")
    with runtime.db.sync_connect() as conn:
        before = int(conn.execute("SELECT version FROM ekbs_policy_version").fetchone()[0])
    response = await client.put(
        f"/api/v1/perm/users/{alice}/clearance", headers=headers,
        json={"clearance": 3, "reason": "晋升主管"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["old_value"] == "L2" and body["new_value"] == "L3"
    assert body["policy_version"] > before

    perm = (await client.get("/api/v1/perm/me", headers=headers_for(alice))).json()
    assert perm["clearance"] == 3
    # L3 资格让他能看到 finance 的 L3 分片（部门链未变，但密级上限抬高了）
    assert "kb_finance_L3" not in perm["visible_shards"], "跨部门仍需 ACL，密级只是纵向上限"

    with runtime.db.sync_connect() as conn:
        change = conn.execute(
            "SELECT change_type, reason FROM ekbs_perm_change_log WHERE object_type='user' "
            "ORDER BY change_id DESC LIMIT 1"
        ).fetchone()
    assert change["change_type"] == "clearance" and change["reason"] == "晋升主管"

    # 未变化的密级应被拒绝（避免无意义的缓存失效）
    assert (
        await client.put(
            f"/api/v1/perm/users/{alice}/clearance", headers=headers, json={"clearance": 3}
        )
    ).status_code == 400


async def test_update_user_only_allows_status(client, kb_admin) -> None:
    runtime, admin_id = kb_admin
    headers = headers_for(admin_id)
    alice = user_id_of(runtime.db, "demo_alice")
    ok = await client.put(
        f"/api/v1/perm/users/{alice}", headers=headers, json={"status": "disabled", "reason": "停用"}
    )
    assert ok.status_code == 200 and ok.json()["status"] == "disabled"
    # 空 body 应报错（该接口只支持 status）
    assert (
        await client.put(f"/api/v1/perm/users/{alice}", headers=headers, json={})
    ).status_code == 422
    # 未同步的字段不该被这个接口改：确认 DB 里昵称没变
    with runtime.db.sync_connect() as conn:
        nick = conn.execute("SELECT nick FROM ekbs_user WHERE user_id = ?", (alice,)).fetchone()["nick"]
    assert nick == "Alice"


async def test_acl_grant_and_revoke_take_effect(client, kb_admin) -> None:
    """授权 → 用户立刻可看跨部门文档；撤销 → 立刻收回。"""
    runtime, admin_id = kb_admin
    headers = headers_for(admin_id)
    alice = user_id_of(runtime.db, "demo_alice")
    with runtime.db.sync_connect() as conn:
        doc_id = int(
            conn.execute(
                "SELECT doc_id FROM ekbs_document WHERE title = '财务保密制度'"
            ).fetchone()["doc_id"]
        )

    # 授权前：C5 拒绝（部门不符 + 密级不够）
    before = (await client.get("/api/v1/perm/me", headers=headers_for(alice))).json()
    assert "kb_finance_L3" not in before["visible_shards"]

    created = await client.post(
        "/api/v1/perm/acl",
        headers=headers,
        json={
            "object_type": "shard", "object_id": "kb_finance_L3",
            "subject_type": "dept", "subject_id": "102", "effect": "allow",
            "reason": "跨部门协作",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["change_type"] == "acl_grant"

    # 密级仍是硬上限：ACL allow 不能突破 clearance（00 §3.2 规则 1）
    after = (await client.get("/api/v1/perm/me", headers=headers_for(alice))).json()
    assert "kb_finance_L3" not in after["visible_shards"], "alice 密级 2 < L3，授权也不该可见"

    # 抬到 L3 之后才生效 —— 两步合起来才是"横向授权 + 纵向上限"的完整语义
    await client.put(
        f"/api/v1/perm/users/{alice}/clearance", headers=headers_for(
            grant(runtime, "demo_carol", "super_admin")
        ), json={"clearance": 3},
    )
    elevated = (await client.get("/api/v1/perm/me", headers=headers_for(alice))).json()
    assert "kb_finance_L3" in elevated["visible_shards"]

    acl_id = created.json()["object_id"] and created.json().get("object_id")
    listing = (await client.get("/api/v1/perm/acl?object_id=kb_finance_L3", headers=headers)).json()
    entry = next(item for item in listing["items"] if item["object_id"] == "kb_finance_L3")
    revoked = await client.delete(f"/api/v1/perm/acl/{entry['acl_id']}", headers=headers)
    assert revoked.status_code == 200
    final = (await client.get("/api/v1/perm/me", headers=headers_for(alice))).json()
    assert "kb_finance_L3" not in final["visible_shards"]

    # 文档级判定也能解释"为什么看不到"
    simulate = await client.post(
        "/api/v1/perm/acl/simulate",
        headers=headers_for(grant(runtime, "demo_carol", "super_admin")),
        json={"user_id": alice, "doc_ids": [doc_id], "reason": "验证回收"},
    )
    assert simulate.status_code == 200
    decision = simulate.json()["document_decisions"][0]
    assert decision["allowed"] is False and decision["reason"] in {"level", "dept"}


async def test_acl_revoke_missing_is_404(client, kb_admin) -> None:
    runtime, admin_id = kb_admin
    response = await client.delete("/api/v1/perm/acl/999999", headers=headers_for(admin_id))
    assert response.status_code == 404


async def test_acl_simulate_is_audited_and_super_admin_only(client, kb_admin, root) -> None:
    runtime, admin_id = kb_admin
    alice = user_id_of(runtime.db, "demo_alice")
    denied = await client.post(
        "/api/v1/perm/acl/simulate", headers=headers_for(admin_id),
        json={"user_id": alice, "reason": "试试"},
    )
    assert denied.status_code == 403
    assert "perm:acl:simulate" in denied.json()["detail"]

    runtime2, root_id = root
    allowed = await client.post(
        "/api/v1/perm/acl/simulate", headers=headers_for(root_id),
        json={"user_id": alice, "reason": "排查可见性"},
    )
    assert allowed.status_code == 200
    body = allowed.json()
    assert body["operator"] == root_id and body["simulated_user"] == alice
    assert body["visible_shards"] == ["kb_all_L1", "kb_hr_L2"]
    with runtime.db.sync_connect() as conn:
        event = conn.execute(
            "SELECT severity, detail FROM ekbs_security_event "
            "WHERE event_type='permission_simulated' AND detail LIKE '%acl_only%' "
            "ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        audit = conn.execute(
            "SELECT 1 FROM ekbs_perm_change_log WHERE change_type='simulate' AND field='acl'"
        ).fetchone()
    assert event is not None and event["severity"] == "high"
    assert audit is not None


async def test_dept_views_and_mapping_is_not_implemented(client, root) -> None:
    """部门映射（O6-1）未定稿：读得出来，写返回 501 且说明原因。"""
    runtime, root_id = root
    headers = headers_for(root_id)
    depts = (await client.get("/api/v1/perm/depts", headers=headers)).json()
    assert depts["items"] and depts["closure_rows"] >= len(depts["items"])
    assert "O6-1" in depts["note"]
    mapping = await client.put("/api/v1/perm/depts/1/mapping", headers=headers, json={})
    assert mapping.status_code == 501
    assert "O6-1" in mapping.json()["detail"]


async def test_permission_audit_lists_changes(client, root) -> None:
    runtime, root_id = root
    headers = headers_for(root_id)
    alice = user_id_of(runtime.db, "demo_alice")
    await client.put(
        f"/api/v1/perm/users/{alice}/clearance", headers=headers, json={"clearance": 3}
    )
    body = (await client.get("/api/v1/perm/audit?change_type=clearance", headers=headers)).json()
    assert body["total"] >= 1
    assert body["items"][0]["change_type"] == "clearance"
    assert body["retention_days"] == 365
