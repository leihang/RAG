"""API 契约（对齐 ``deploy/scripts/smoke.sh`` 的断言）。

``smoke.sh`` 是本项目**可执行的验收标准**，因此这里的字段名与状态码刻意与它一致：

* ``/healthz`` → ``status == "ok"``
* ``/readyz`` → ``status == "ready"``
* ``POST /api/v1/qa/chat`` 无凭证 → **非 200**（权限红线）
* ``POST /api/v1/qa/chat`` 带内环 token → ``answer`` 与 ``citations[0].chunk_id``
* ``/health/deps`` → ``faiss_shards > 0``、``embed/llm/rerank/fingerprint_match == true``
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from ekbs.api.app import create_app
from tests.conftest import TEST_INTERNAL_TOKEN, user_id_of


@pytest.fixture
async def client(seeded):
    runtime, _summary = seeded
    app = create_app(runtime.settings)
    # httpx 的 ASGITransport 不会自动执行 lifespan，这里显式跑
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as instance:
            instance.app = app  # type: ignore[attr-defined]
            yield instance


@pytest.fixture
def internal_headers(seeded):
    runtime, _ = seeded
    alice_id = user_id_of(runtime.db, "demo_alice")
    return {
        "X-Internal-Token": TEST_INTERNAL_TOKEN,
        "X-Internal-User-Id": str(alice_id),
        "X-Internal-Channel": "dingtalk_robot",
    }


async def test_livez(client: AsyncClient) -> None:
    response = await client.get("/livez")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_healthz(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"  # smoke.sh 的断言
    assert body["checks"]["db"] is True


async def test_readyz(client: AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"  # smoke.sh 的断言
    assert body["checks"]["schema"] is True


async def test_health_deps_reports_probe_results(client: AsyncClient) -> None:
    response = await client.get("/health/deps")
    body = response.json()
    assert body["faiss_shards"] > 0
    assert body["embed"] is True
    assert body["llm"] is True
    assert body["rerank"] is True
    assert body["fingerprint_match"] is True


async def test_qa_without_credentials_is_rejected(client: AsyncClient) -> None:
    """红线：没有凭证的问答必须被拒（smoke.sh 判定 HTTP 200 即为禁止上线）。"""
    response = await client.post("/api/v1/qa/chat", json={"question": "年假有多少天"})
    assert response.status_code == 401


async def test_qa_rejects_wrong_internal_token(client: AsyncClient, internal_headers) -> None:
    headers = {**internal_headers, "X-Internal-Token": "wrong-token"}
    response = await client.post(
        "/api/v1/qa/chat", json={"question": "年假有多少天"}, headers=headers
    )
    assert response.status_code == 401


async def test_qa_with_internal_token_answers(client: AsyncClient, internal_headers) -> None:
    response = await client.post(
        "/api/v1/qa/chat", json={"question": "年假有多少天", "stream": False},
        headers=internal_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["answer"]
    assert body["citations"], "必须带引用"
    assert body["citations"][0]["chunk_id"] > 0
    assert body["perm_fingerprint"]
    # 推荐问答不只在拒答时给：正常作答也要带回「延展/相关/预判」推荐
    assert body["suggested_questions"], "正常作答也要带推荐问"
    assert body["visible_shard_count"] >= 1


async def test_qa_alias_prefix_also_mounted(client: AsyncClient, internal_headers) -> None:
    """smoke.sh 会同时尝试 /api/v1 与 /v1 两种前缀（口径冲突的兼容处理）。"""
    response = await client.post(
        "/v1/qa/chat", json={"question": "年假有多少天"}, headers=internal_headers
    )
    assert response.status_code == 200
    assert response.json()["answer"]


async def test_qa_refuses_when_permission_changed_midway(client: AsyncClient, seeded) -> None:
    """权限被回收后，缓存不得继续供出旧答案（C2：指纹变了就不再命中）。"""
    runtime, _ = seeded
    bob_id = user_id_of(runtime.db, "demo_bob")
    headers = {
        "X-Internal-Token": TEST_INTERNAL_TOKEN,
        "X-Internal-User-Id": str(bob_id),
    }
    first = await client.post(
        "/api/v1/qa/chat", json={"question": "财务专用章由谁保管"}, headers=headers
    )
    assert first.json()["citations"], "bob 应当能拿到财务文档引用"

    # 把 bob 的密级降到 1 并自增策略版本 → 指纹变化
    with runtime.db.sync_connect() as conn:
        conn.execute("UPDATE ekbs_user SET clearance = 1 WHERE user_id = ?", (bob_id,))
        from ekbs.permission.router import bump_policy_version

        bump_policy_version(conn, reason="test:demote")
        conn.commit()

    second = await client.post(
        "/api/v1/qa/chat", json={"question": "财务专用章由谁保管"}, headers=headers
    )
    assert all(
        citation["doc_id"] != first.json()["citations"][0]["doc_id"]
        for citation in second.json()["citations"]
    )


async def test_retrieve_endpoint_returns_candidates(client: AsyncClient, internal_headers) -> None:
    response = await client.post(
        "/api/v1/qa/retrieve", json={"query": "年假有多少天"}, headers=internal_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["refused"] is False
    assert body["chunks"]
    assert body["chunks"][0]["rerank_score"] is not None


async def test_perm_endpoint_computes_fingerprint_server_side(
    client: AsyncClient, internal_headers
) -> None:
    """权限指纹只能由服务端算；请求头里塞指纹也不会被采信。"""
    spoofed = {**internal_headers, "X-Perm-Fingerprint": "deadbeef"}
    response = await client.get("/api/v1/auth/perm", headers=spoofed)
    assert response.status_code == 200
    body = response.json()
    assert body["fingerprint"] != "deadbeef"
    assert len(body["fingerprint"]) == 64


async def test_question_length_limit(client: AsyncClient, internal_headers) -> None:
    response = await client.post(
        "/api/v1/qa/chat", json={"question": "年" * 5000}, headers=internal_headers
    )
    assert response.status_code == 413


async def test_qa_stream_emits_events(client: AsyncClient, internal_headers) -> None:
    async with client.stream(
        "POST", "/api/v1/qa/chat/stream", json={"question": "年假有多少天"},
        headers=internal_headers,
    ) as response:
        assert response.status_code == 200
        text = ""
        async for chunk in response.aiter_text():
            text += chunk
    assert "event: meta" in text
    assert "event: delta" in text
    assert "event: final" in text
    assert "event: done" in text


async def test_figure_endpoint_enforces_acl(client: AsyncClient, seeded) -> None:
    """图片回源必须按 shard_id 做 ACL 复检：无权用户得到 404（不是 403）。"""
    runtime, _ = seeded
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT chunk_id FROM ekbs_chunk WHERE shard_id = 'kb_finance_L3' LIMIT 1"
        ).fetchone()
    alice_id = user_id_of(runtime.db, "demo_alice")
    response = await client.get(
        f"/api/v1/assets/figure/{int(row['chunk_id'])}",
        headers={"X-Internal-Token": TEST_INTERNAL_TOKEN, "X-Internal-User-Id": str(alice_id)},
    )
    assert response.status_code == 404


async def test_dev_token_issues_jwt(client: AsyncClient, seeded) -> None:
    runtime, _ = seeded
    alice_id = user_id_of(runtime.db, "demo_alice")
    issued = await client.post("/api/v1/auth/dev-token", json={"user_id": alice_id})
    assert issued.status_code == 200
    token = issued.json()["token"]

    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["user_id"] == alice_id

    answered = await client.post(
        "/api/v1/qa/chat", json={"question": "年假有多少天"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert answered.status_code == 200
    assert answered.json()["answer"]


async def test_dingtalk_login_is_refused_for_admin(client) -> None:
    """管理台不发钉钉免登令牌。钉钉同事不能由此进入管理台。"""
    response = await client.post("/api/v1/auth/dingtalk-login", json={"auth_code": "x"})
    assert response.status_code == 403
    assert "登录名和口令" in response.json()["detail"]


async def test_dingtalk_login_validates_input(client) -> None:
    response = await client.post("/api/v1/auth/dingtalk-login", json={"auth_code": ""})
    assert response.status_code == 422


async def test_dingtalk_config_is_public_and_lists_missing_keys(client, seeded) -> None:
    """前端免登配置端点：**无需鉴权**（登录页在拿到令牌前就要读它），
    只返回公开标识（corpId / AppKey），并把缺失项列出来供管理员照单补配置。"""
    response = await client.get("/api/v1/auth/dingtalk-config")
    assert response.status_code == 200
    body = response.json()
    # 测试环境没有钉钉凭据 → 不可用，且三项都缺
    assert body["enabled"] is False
    assert set(body["missing"]) == {
        "DINGTALK_CORP_ID", "DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET",
    }
    # 公开标识可以为空，但**绝不能出现任何密钥值**
    assert "secret" not in str(body).lower().replace("dingtalk_client_secret", "")


async def test_dingtalk_config_reports_enabled_when_credentials_present(
    client, seeded
) -> None:
    # 注意：`client` 夹具内部会 create_app(...) 并自建 runtime，
    # 因此要改**应用实例**的 secrets，而不是 seeded 那个 runtime。
    app_runtime = client.app.state.runtime
    app_runtime.secrets._env.update(  # noqa: SLF001 - 测试直接注入凭据视图
        {
            "DINGTALK_CORP_ID": "ding-corpid-1",
            "DINGTALK_CLIENT_ID": "ding-appkey-1",
            "DINGTALK_CLIENT_SECRET": "ding-appsecret-1",
        }
    )
    body = (await client.get("/api/v1/auth/dingtalk-config")).json()
    assert body["enabled"] is True
    assert body["corp_id"] == "ding-corpid-1"
    assert body["client_id"] == "ding-appkey-1"
    assert body["missing"] == []
    # AppSecret 永不外发（`04` §2.6 纪律）
    assert "ding-appsecret-1" not in str(body)
