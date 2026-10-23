"""检索调试台（`05` §8.2 的 ``/api/v1/rag/debug/*``）。

这组用例盯三件事：

* **调参只影响本次请求**（并发安全）——同一进程里两次并发调用不同参数不得互相污染；
* **分阶段明细真的能定位问题**（各分片召回、FTS、RRF、rerank、C5）；
* **simulate 是跨权限操作**：权限点仅 super_admin、必须留安全事件与审计。
"""

from __future__ import annotations

import asyncio

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
def playground_headers(seeded):
    """用 alice（clearance=2、HR 部门，能看到 kb_hr_L2）并授予 doc_reviewer。

    为什么不用 carol（kb_admin）：她 clearance=1 且属于总部，**看不到** HR 制度文档，
    「有权限但没数据」会让检索类断言全部落到拒答分支（实测踩到）。
    """
    runtime, _ = seeded
    user_id = grant(runtime, "demo_alice", "doc_reviewer")
    return runtime, user_id, headers_for(user_id)


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


async def test_playground_requires_permission(client, seeded) -> None:
    runtime, _ = seeded
    end_user = headers_for(user_id_of(runtime.db, "demo_alice"))
    for path, body in (
        ("/api/v1/rag/debug/rewrite", {"query": "年假"}),
        ("/api/v1/rag/debug/retrieve", {"query": "年假"}),
        ("/api/v1/rag/debug/query", {"query": "年假"}),
        ("/api/v1/rag/debug/simulate", {"query": "年假", "user_id": 2, "reason": "x"}),
    ):
        assert (await client.post(path, headers=end_user, json=body)).status_code == 403
        assert (await client.post(path, json=body)).status_code == 401
    assert (await client.get("/api/v1/rag/debug/history", headers=end_user)).status_code == 403
    assert (await client.get("/api/v1/rag/debug/shards", headers=end_user)).status_code == 403


async def test_debug_retrieve_shows_every_stage(client, playground_headers) -> None:
    _runtime, _uid, headers = playground_headers
    body = (
        await client.post(
            "/api/v1/rag/debug/retrieve", headers=headers, json={"query": "年假有多少天"}
        )
    ).json()
    stages = body["stages"]
    assert set(stages) >= {"params", "dense", "sparse", "rrf", "rerank", "c5"}
    assert stages["params"]["dense_top_k"] == 30 and stages["params"]["rrf_k"] == 60
    assert stages["dense"]["per_shard"], "应逐分片给出稠密召回"
    assert stages["sparse"]["hits"], "FTS 命中明细应可查"
    assert stages["rrf"]["fused"]
    assert stages["rerank"]["scores"]
    assert body["visible_shards"] and body["chunks"]
    assert body["chunks"][0]["rerank_score"] is not None


async def test_debug_retrieve_overrides_do_not_touch_global_config(client, playground_headers) -> None:
    """调参只作用于本次请求（RetrievalOverrides），绝不改全局配置。"""
    runtime, _uid, headers = playground_headers
    original = (runtime.settings.retrieval.dense_top_k, runtime.settings.retrieval.rrf_k)
    body = (
        await client.post(
            "/api/v1/rag/debug/retrieve", headers=headers,
            json={"query": "年假有多少天", "dense_top_k": 3, "rrf_k": 10, "rerank_enabled": False},
        )
    ).json()
    assert body["stages"]["params"]["dense_top_k"] == 3
    assert body["stages"]["params"]["rrf_k"] == 10
    assert body["stages"]["rerank"]["enabled"] is False
    assert (runtime.settings.retrieval.dense_top_k, runtime.settings.retrieval.rrf_k) == original


async def test_overrides_are_concurrency_safe(client, playground_headers) -> None:
    """并发不同参数不得互相污染 —— 这正是"不改全局配置"要解决的问题。"""
    _runtime, _uid, headers = playground_headers

    async def call(top_k: int):
        return await client.post(
            "/api/v1/rag/debug/retrieve", headers=headers,
            json={"query": "年假有多少天", "dense_top_k": top_k},
        )

    first, second = await asyncio.gather(call(3), call(25))
    assert first.json()["stages"]["params"]["dense_top_k"] == 3
    assert second.json()["stages"]["params"]["dense_top_k"] == 25


async def test_debug_retrieve_min_score_threshold_semantics(client, playground_headers) -> None:
    """阈值语义是 ``top_score < min_score`` 才拒答（边界：等于阈值**不算**无依据）。

    用调试台正好可以把这条边界问清楚：
    * 精确命中（score=1.0）+ 阈值 1.0 → **不拒答**；
    * 弱命中（覆盖率只有一部分）+ 阈值 0.99 → 拒答 ``below_threshold``。
    """
    _runtime, _uid, headers = playground_headers
    exact = (
        await client.post(
            "/api/v1/rag/debug/retrieve", headers=headers,
            json={"query": "年假有多少天", "rerank_min_score": 1.0},
        )
    ).json()
    assert exact["top_score"] == 1.0
    assert exact["refused"] is False, "等于阈值不应判为无依据"

    weak = (
        await client.post(
            "/api/v1/rag/debug/retrieve", headers=headers,
            json={"query": "年假 报销 培训 差旅 预算", "rerank_min_score": 0.99},
        )
    ).json()
    assert weak["refused"] is True
    assert weak["refusal_reason"] == "below_threshold"


async def test_debug_rewrite_uses_session_history(client, playground_headers) -> None:
    _runtime, _uid, headers = playground_headers
    first = await client.post(
        "/api/v1/qa/chat",
        headers=headers,
        json={"question": "年假有多少天", "conversation_id": "dbg-1"},
    )
    assert first.status_code == 200

    rewritten = await client.post(
        "/api/v1/rag/debug/rewrite",
        headers=headers,
        json={"query": "那它需要提前几天申请", "conversation_id": "dbg-1"},
    )
    body = rewritten.json()
    assert body["used_history"] is True
    assert body["rewritten"].startswith("年假有多少天")
    assert body["strategy"] == "heuristic"


async def test_debug_query_returns_answer_and_stages(client, playground_headers) -> None:
    _runtime, _uid, headers = playground_headers
    body = (
        await client.post(
            "/api/v1/rag/debug/query", headers=headers, json={"query": "年假有多少天"}
        )
    ).json()
    assert body["answer"] and body["citations"]
    assert set(body["stages"]) >= {"dense", "sparse", "rrf", "rerank", "c5"}
    assert body.get("history_used") is False
    assert body.get("rewritten_query") == "年假有多少天"
    assert body.get("session_id"), "完整作答应落审计并回传 session_id"


async def test_debug_query_multi_turn_rewrites_followup(client, playground_headers) -> None:
    """调试台带 conversation_id 时走正式多轮：追问改写进检索串。"""
    _runtime, _uid, headers = playground_headers
    conv = "pg-test-followup"
    first = await client.post(
        "/api/v1/rag/debug/query",
        headers=headers,
        json={"query": "年假有多少天", "conversation_id": conv},
    )
    assert first.status_code == 200
    assert first.json()["history_used"] is False

    second = await client.post(
        "/api/v1/rag/debug/query",
        headers=headers,
        json={"query": "能介绍详细点吗？", "conversation_id": conv},
    )
    body = second.json()
    assert second.status_code == 200
    assert body["history_used"] is True
    assert body["rewritten_query"].startswith("年假有多少天")
    assert "详细" in body["rewritten_query"]
    assert set(body["stages"]) >= {"dense", "sparse", "rrf"}
    assert body["timings_ms"]["generate_ms"] >= 0


async def test_simulate_requires_its_own_permission(client, seeded) -> None:
    """`rag:playground:simulate` 仅 super_admin —— kb_admin 能跑调试台但不能模拟他人。"""
    runtime, _ = seeded
    admin_id = grant(runtime, "demo_carol", "kb_admin")
    alice_id = user_id_of(runtime.db, "demo_alice")
    denied = await client.post(
        "/api/v1/rag/debug/simulate",
        headers=headers_for(admin_id),
        json={"query": "年假", "user_id": alice_id, "reason": "排障"},
    )
    assert denied.status_code == 403
    assert "rag:playground:simulate" in denied.json()["detail"]
    # 常规调试台接口他是有的（05 §3.4：kb_admin 有 rag:playground:run）
    assert (
        await client.post(
            "/api/v1/rag/debug/retrieve", headers=headers_for(admin_id), json={"query": "年假"}
        )
    ).status_code == 200


async def test_simulate_by_super_admin_is_audited(client, seeded) -> None:
    runtime, _ = seeded
    root_id = grant(runtime, "demo_carol", "super_admin")
    alice_id = user_id_of(runtime.db, "demo_alice")
    response = await client.post(
        "/api/v1/rag/debug/simulate",
        headers=headers_for(root_id),
        json={"query": "年假有多少天", "user_id": alice_id, "reason": "排查召回缺失"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["operator"] == root_id and body["simulated_user"] == alice_id
    assert body["simulated_perm"]["visible_shards"] == ["kb_all_L1", "kb_hr_L2"]

    # 安全事件（high）与审计都必须留痕
    with runtime.db.sync_connect() as conn:
        event = conn.execute(
            "SELECT severity, detail FROM ekbs_security_event "
            "WHERE event_type = 'permission_simulated' ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        audit = conn.execute(
            "SELECT change_type, operator_id FROM ekbs_perm_change_log "
            "WHERE change_type = 'simulate' ORDER BY change_id DESC LIMIT 1"
        ).fetchone()
    assert event is not None and event["severity"] == "high"
    assert str(root_id) in str(event["detail"]) and str(alice_id) in str(event["detail"])
    assert audit is not None and int(audit["operator_id"]) == root_id

    # 模拟结果确实是"以 alice 的身份"：财务文档不该出现
    titles = {chunk["doc_title"] for chunk in body["chunks"]}
    assert "财务保密制度" not in titles


async def test_simulate_unknown_user_is_404(client, seeded) -> None:
    runtime, _ = seeded
    root_id = grant(runtime, "demo_carol", "super_admin")
    response = await client.post(
        "/api/v1/rag/debug/simulate",
        headers=headers_for(root_id),
        json={"query": "年假", "user_id": 999999, "reason": "排障"},
    )
    assert response.status_code == 404


async def test_history_is_recorded(client, playground_headers) -> None:
    _runtime, _uid, headers = playground_headers
    await client.post("/api/v1/rag/debug/retrieve", headers=headers, json={"query": "年假有多少天"})
    body = (await client.get("/api/v1/rag/debug/history", headers=headers)).json()
    assert body["count"] >= 1
    assert body["items"][0]["kind"] == "retrieve"
    assert "Schema" in body["retention"]


async def test_shards_endpoint_explains_routing(client, playground_headers) -> None:
    """排障第一步：他为什么命不中 —— 直接看分片路由的输入输出。"""
    _runtime, _uid, headers = playground_headers
    body = (await client.get("/api/v1/rag/debug/shards", headers=headers)).json()
    assert body["visible_shards"] and body["registered_shards"]
    assert body["clearance"] == 2
    assert any(item["level"] > body["clearance"] for item in body["registered_shards"]), (
        "登记表里应能看到超出密级的分片（便于解释为什么没被路由到）"
    )


async def test_numeric_mode_is_optional_and_validated(client, playground_headers) -> None:
    """取数模式是**可选路径**：默认不带 mode 时行为不变，非法 mode 被拒。

    这条钉住三件事：① `/rag/debug/query` 的响应里始终有 `numeric` 字段（prose 下为 None）；
    ② 不传 mode 时仍走业务问答；③ mode 只接受 prose/numeric（Pydantic pattern）。
    """
    _runtime, _uid, headers = playground_headers
    resp = await client.post(
        "/api/v1/rag/debug/query", headers=headers, json={"query": "年假有多少天"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "numeric" in body and body["numeric"] is None
    assert body.get("mode") in (None, "prose")
    assert body["citations"] is not None

    bad = await client.post(
        "/api/v1/rag/debug/query", headers=headers,
        json={"query": "年假有多少天", "mode": "bogus"},
    )
    assert bad.status_code == 422
