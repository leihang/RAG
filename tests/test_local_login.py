"""本机账号登录（管理台第二种登录方式）—— 安全护栏必须是真闸门。

覆盖四条断言（都是"看起来实现了、其实没有"的典型）：
1. 正口令能换到可用 JWT，且**权限链路与钉钉免登完全一致**；
2. 密码错 / 账号不存在 → 同一句话（不可账号枚举）；
3. 连续失败达上限 → **账号锁定**（暴力破解被锁死，且留审计）；
4. **只有直连回环**才算本机：带 X-Forwarded-For（经反向代理）一律拒绝——
   这是最容易搞错的一条：代理进来时 client.host 恒为 127.0.0.1。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from ekbs.local_auth import MIN_PASSWORD_LENGTH, LocalAccountService, hash_password, verify_password
from ekbs.api.app import create_app
from tests.conftest import TEST_INTERNAL_TOKEN, user_id_of

PASSWORD = "correct-horse-battery-staple"


def _make_account(runtime, *, username: str = "opsadmin", must_change: bool = False) -> int:
    """把本机账号绑到 alice（同一个库文件，两个 Runtime 都能看见）。"""
    user_id = user_id_of(runtime.db, "demo_alice")
    LocalAccountService(runtime.db, runtime.settings).create(
        username=username, user_id=user_id, password=PASSWORD, must_change=must_change
    )
    return user_id


@pytest.fixture
async def client(seeded):
    """回环来源的客户端（等价于"人在服务器本机"）。"""
    runtime, _ = seeded
    app = create_app(runtime.settings)
    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51234)),
        base_url="http://testserver",
    ) as instance:
        instance.app = app  # type: ignore[attr-defined]
        yield instance


async def _client_with_host(seeded, host: str):
    """自建客户端以便伪造来源 IP（钉钉/回环/远程三种来源的差异靠它验证）。"""
    runtime, _ = seeded
    app = create_app(runtime.settings)
    transport = ASGITransport(app=app, client=(host, 51234))
    return app, AsyncClient(transport=transport, base_url="http://testserver")


# ---------------------------------------------------------------------------
# 哈希与口令策略
# ---------------------------------------------------------------------------


def test_password_hash_roundtrip_and_tamper() -> None:
    encoded = hash_password(PASSWORD)
    assert encoded.startswith("scrypt$")
    assert PASSWORD not in encoded, "编码串里绝不能出现明文"
    assert verify_password(PASSWORD, encoded) is True
    assert verify_password(PASSWORD + "x", encoded) is False
    assert verify_password(PASSWORD, "broken$encoded") is False


def test_short_password_is_rejected() -> None:
    with pytest.raises(ValueError):
        hash_password("short")


# ---------------------------------------------------------------------------
# 登录成功 / 失败口径
# ---------------------------------------------------------------------------


async def test_local_login_issues_working_jwt(client, seeded) -> None:
    runtime, _ = seeded
    expected_user_id = _make_account(runtime, must_change=False)

    response = await client.post(
        "/api/v1/auth/local-login", json={"username": "opsadmin", "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user_id"] == expected_user_id
    assert body["must_change"] is False

    me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {body['token']}"}
    )
    assert me.status_code == 200
    assert me.json()["user_id"] == expected_user_id


async def test_local_login_flags_must_change(client, seeded) -> None:
    runtime, _ = seeded
    _make_account(runtime, username="firstlogin", must_change=True)
    body = (
        await client.post(
            "/api/v1/auth/local-login", json={"username": "firstlogin", "password": PASSWORD}
        )
    ).json()
    assert body["must_change"] is True


async def test_local_login_bad_credentials_are_indistinguishable(client, seeded) -> None:
    runtime, _ = seeded
    _make_account(runtime, username="opsadmin")

    wrong_password = await client.post(
        "/api/v1/auth/local-login", json={"username": "opsadmin", "password": "wrong-password-123"}
    )
    unknown_user = await client.post(
        "/api/v1/auth/local-login", json={"username": "nobody", "password": PASSWORD}
    )
    assert wrong_password.status_code == 401
    assert unknown_user.status_code == 401
    assert wrong_password.json()["detail"] == unknown_user.json()["detail"], (
        "两种失败必须同口径，否则可以枚举账号"
    )


async def test_local_login_locks_account_after_failures(client, seeded) -> None:
    runtime, _ = seeded
    _make_account(runtime, username="opsadmin")
    client.app.state.runtime.settings.security["local_login_max_failures"] = 3
    client.app.state.runtime.settings.security["local_login_lock_minutes"] = 10

    for _ in range(3):
        failed = await client.post(
            "/api/v1/auth/local-login",
            json={"username": "opsadmin", "password": "wrong-password-123"},
        )
        assert failed.status_code == 401

    # 锁定后即便口令正确也拒绝（423），并给出解锁时间
    locked = await client.post(
        "/api/v1/auth/local-login", json={"username": "opsadmin", "password": PASSWORD}
    )
    assert locked.status_code == 423
    assert "锁定" in locked.json()["detail"]

    # 锁定事件必须留痕（审计里能看见暴力破解尝试）
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM ekbs_security_event WHERE event_type = 'local_login_locked'"
        ).fetchone()
    assert row is not None


async def test_local_login_disabled_returns_404(client, seeded) -> None:
    runtime, _ = seeded
    _make_account(runtime, username="opsadmin")
    client.app.state.runtime.settings.security["local_login_enabled"] = False
    response = await client.post(
        "/api/v1/auth/local-login", json={"username": "opsadmin", "password": PASSWORD}
    )
    assert response.status_code == 404, "关闭后不应暴露该端点存在"


# ---------------------------------------------------------------------------
# 来源限制（本机登录的核心约束）
# ---------------------------------------------------------------------------


async def test_local_login_rejected_from_remote_address(seeded) -> None:
    runtime, _ = seeded
    _make_account(runtime, username="opsadmin")
    app, remote = await _client_with_host(seeded, "10.0.0.9")
    async with app.router.lifespan_context(app), remote:
        response = await remote.post(
            "/api/v1/auth/local-login", json={"username": "opsadmin", "password": PASSWORD}
        )
    assert response.status_code == 403
    assert "本机" in response.json()["detail"]


async def test_local_login_rejected_when_proxied(seeded) -> None:
    """经反向代理时 client.host 是回环，**必须**靠 X-Forwarded-For 识别为外部来源。"""
    runtime, _ = seeded
    _make_account(runtime, username="opsadmin")
    app = create_app(runtime.settings)
    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 51234)),
        base_url="http://testserver",
    ) as proxied:
        response = await proxied.post(
            "/api/v1/auth/local-login",
            json={"username": "opsadmin", "password": PASSWORD},
            headers={"X-Forwarded-For": "203.0.113.7"},
        )
    assert response.status_code == 403, "代理来源必须被拒，否则等于把内网所有人都当成'本机'"


async def test_local_login_allow_remote_switch(seeded) -> None:
    """显式打开 allow_remote 后才允许远程（默认关闭；文档标注不推荐）。"""
    runtime, _ = seeded
    _make_account(runtime, username="opsadmin")
    app, remote = await _client_with_host(seeded, "10.0.0.9")
    runtime.settings.security["local_login_allow_remote"] = True
    async with app.router.lifespan_context(app), remote:
        response = await remote.post(
            "/api/v1/auth/local-login", json={"username": "opsadmin", "password": PASSWORD}
        )
    assert response.status_code == 200


def test_min_password_length_is_enforced_in_config_docs() -> None:
    """口令长度下限是安全底线，改动时必须是有意识的（此处钉住数值）。"""
    assert MIN_PASSWORD_LENGTH == 6


async def test_change_own_password_then_login_with_new(client, seeded) -> None:
    runtime, _ = seeded
    _make_account(runtime, username="opsadmin", must_change=True)
    issued = await client.post(
        "/api/v1/auth/local-login", json={"username": "opsadmin", "password": PASSWORD}
    )
    token = issued.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    wrong = await client.post(
        "/api/v1/auth/password",
        json={"current_password": "not-the-password", "new_password": "newpass"},
        headers=headers,
    )
    assert wrong.status_code == 401

    same = await client.post(
        "/api/v1/auth/password",
        json={"current_password": PASSWORD, "new_password": PASSWORD},
        headers=headers,
    )
    assert same.status_code == 422

    short = await client.post(
        "/api/v1/auth/password",
        json={"current_password": PASSWORD, "new_password": "12345"},
        headers=headers,
    )
    assert short.status_code == 422

    changed = await client.post(
        "/api/v1/auth/password",
        json={"current_password": PASSWORD, "new_password": "newpass"},
        headers=headers,
    )
    assert changed.status_code == 200

    old = await client.post(
        "/api/v1/auth/local-login", json={"username": "opsadmin", "password": PASSWORD}
    )
    assert old.status_code == 401
    fresh = await client.post(
        "/api/v1/auth/local-login", json={"username": "opsadmin", "password": "newpass"}
    )
    assert fresh.status_code == 200
    assert fresh.json()["must_change"] is False


async def test_admin_reset_password_and_reject_dingtalk_admin_token(client, seeded) -> None:
    runtime, _ = seeded
    user_id = _make_account(runtime, username="opsadmin")
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO ekbs_user_role(user_id, role_id, scope_type, scope_id) "
            "SELECT ?, role_id, 'global', 0 FROM ekbs_role WHERE code = 'super_admin'",
            (user_id,),
        )
        conn.commit()
    headers = {"X-Internal-Token": TEST_INTERNAL_TOKEN, "X-Internal-User-Id": str(user_id)}
    missing = await client.put(
        f"/api/v1/perm/users/{user_id_of(runtime.db, 'demo_bob')}/password",
        json={"password": "654321"},
        headers=headers,
    )
    assert missing.status_code == 404

    reset = await client.put(
        f"/api/v1/perm/users/{user_id}/password",
        json={"password": "654321"},
        headers=headers,
    )
    assert reset.status_code == 200, reset.text
    login = await client.post(
        "/api/v1/auth/local-login", json={"username": "opsadmin", "password": "654321"}
    )
    assert login.status_code == 200

    token = client.app.state.runtime.authenticator.issue_token(user_id, channel="dingtalk_h5")
    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 401
