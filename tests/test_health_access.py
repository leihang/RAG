"""健康端点的访问边界（`04` §7.3）。

两条口径必须同时成立：

* ``/livez`` ``/healthz`` ``/readyz`` —— **保持公开**：监控探针无法携带凭据，
  且它们只做本机查询、不出网；
* ``/health/deps`` —— **收口**：它会真实出网并暴露内部状态（模型型号、分片/向量数、
  磁盘水位、指纹一致性）。只允许「服务器本机直连」或「已登录且有
  ``sys:health:read`` 的管理员」—— 与用户定下的两条登录路径（钉钉免登 / 本机账号）一致，
  **不引入第三种凭据**。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from ekbs.api.app import create_app
from tests.conftest import user_id_of


@pytest.fixture
async def loopback(seeded):
    """本机直连客户端（等价于在服务器上跑 smoke.sh）。"""
    runtime, _ = seeded
    app = create_app(runtime.settings)
    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51234)),
        base_url="http://testserver",
    ) as client:
        client.app = app  # type: ignore[attr-defined]
        yield client


@pytest.fixture
async def remote(seeded):
    """外部来源客户端（内网另一台机器）。"""
    runtime, _ = seeded
    app = create_app(runtime.settings)
    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app, client=("10.0.0.9", 51234)),
        base_url="http://testserver",
    ) as client:
        client.app = app  # type: ignore[attr-defined]
        yield client


async def test_probes_stay_public_from_remote(remote) -> None:
    """探针必须无凭据可调（监控用），且不出网、不泄露内部状态。"""
    assert (await remote.get("/livez")).status_code == 200
    assert (await remote.get("/healthz")).status_code == 200
    assert (await remote.get("/readyz")).status_code == 200


async def test_health_deps_allows_loopback(loopback) -> None:
    """本机直连放行：smoke.sh / 本机排障不需要任何令牌。"""
    response = await loopback.get("/health/deps")
    assert response.status_code == 200, response.text
    assert "faiss_shards" in response.json()


async def test_health_deps_requires_identity_from_remote(remote) -> None:
    response = await remote.get("/health/deps")
    assert response.status_code == 401
    assert "本机" in response.json()["detail"]


async def test_health_deps_rejects_proxied_request(seeded) -> None:
    """经反向代理时 client.host 是回环 —— 必须靠 X-Forwarded-For 判为外部来源。"""
    runtime, _ = seeded
    app = create_app(runtime.settings)
    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51234)),
        base_url="http://testserver",
    ) as proxied:
        response = await proxied.get(
            "/health/deps", headers={"X-Forwarded-For": "203.0.113.7"}
        )
    assert response.status_code == 401


async def test_health_deps_allows_authenticated_admin(seeded, remote) -> None:
    """已登录且有 sys:health:read 的管理员（如 kb_admin）远程也能看深探测。"""
    runtime, _ = seeded
    carol_id = user_id_of(runtime.db, "demo_carol")
    # 令牌由应用实例的 authenticator 签发（app.state 在 lifespan 里装配）
    token = remote.app.state.authenticator.issue_token(carol_id, channel="local")
    response = await remote.get(
        "/health/deps", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text


async def test_health_deps_rejects_user_without_permission(seeded, remote) -> None:
    """普通用户（end_user 无 sys:health:read）即便登录了也不能看深探测。

    状态码是 **401**（不是 403）：项目全局把 ``PermissionDenied`` 映射为 401，
    口径是「不泄露资源是否存在」（见 `app.py` 的异常处理器）。
    """
    runtime, _ = seeded
    alice_id = user_id_of(runtime.db, "demo_alice")
    token = remote.app.state.authenticator.issue_token(alice_id, channel="local")
    response = await remote.get(
        "/health/deps", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401
