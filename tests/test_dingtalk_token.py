"""钉钉 access_token 客户端（契约依据 `docs/spec/50-dingtalk.md` §1，已核实）。

三条断言针对官方明确写过的要求：
① 必须缓存、不许频繁调用；② 缓存 TTL 要「略小于 7200」；③ 密钥错是不可重试的配置错误。
"""

from __future__ import annotations

import httpx
import pytest
import respx

from ekbs.cache.store import CacheStore
from ekbs.dingtalk.token import TOKEN_PATH, DingTalkTokenClient
from ekbs.errors import ConfigError, ModelUnavailable
from ekbs.observability import metrics as m

URL = f"https://api.dingtalk.com{TOKEN_PATH}"


@pytest.fixture(autouse=True)
def _reset_metrics():
    m.reset()
    yield
    m.reset()


def client(cache=None, **kwargs) -> DingTalkTokenClient:
    return DingTalkTokenClient(
        app_key="dingtest-key", app_secret="dingtest-secret",
        cache=cache, retry_base_delay=0.0, **kwargs
    )


@respx.mock
async def test_token_is_fetched_and_cached(runtime) -> None:
    """官方要求：缓存到本地，不可频繁调用本接口。"""
    route = respx.post(URL).mock(
        return_value=httpx.Response(200, json={"accessToken": "tok-1", "expireIn": 7200})
    )
    cache = CacheStore(runtime.db, runtime.settings)
    token_client = client(cache)

    assert await token_client.get_token() == "tok-1"
    assert await token_client.get_token() == "tok-1"  # 命中缓存
    assert route.call_count == 1, "第二次必须走缓存，不能再次请求钉钉"

    import json

    body = json.loads(route.calls[0].request.content)
    assert body == {"appKey": "dingtest-key", "appSecret": "dingtest-secret"}
    assert route.calls[0].request.headers["Content-Type"].startswith("application/json")


@respx.mock
async def test_cache_ttl_is_slightly_less_than_expire_in(runtime) -> None:
    """expireIn=7200、提前 300s 刷新 → 缓存 6900s（官方「略小于 7200」的落地）。"""
    respx.post(URL).mock(
        return_value=httpx.Response(200, json={"accessToken": "tok-2", "expireIn": 7200})
    )
    cache = CacheStore(runtime.db, runtime.settings)
    token_client = client(cache, refresh_ahead_seconds=300)
    await token_client.get_token()

    import time

    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT expires_at FROM ekbs_cache WHERE cache_key LIKE 'ekbs:dt:app_token:%'"
        ).fetchone()
    ttl = int(row["expires_at"]) - int(time.time())
    assert 6800 <= ttl <= 6900, ttl


@respx.mock
async def test_tiny_expire_in_does_not_produce_zero_ttl(runtime) -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(200, json={"accessToken": "tok-3", "expireIn": 100})
    )
    token_client = client(CacheStore(runtime.db, runtime.settings), refresh_ahead_seconds=300)
    await token_client.get_token()
    import time

    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT expires_at FROM ekbs_cache WHERE cache_key LIKE 'ekbs:dt:app_token:%'"
        ).fetchone()
    assert int(row["expires_at"]) - int(time.time()) >= 9


@respx.mock
async def test_invalid_credentials_is_a_fatal_config_error() -> None:
    """密钥错不可重试，且必须是配置错误（而不是被当成云端抖动重试 3 次）。"""
    route = respx.post(URL).mock(
        return_value=httpx.Response(
            400, json={"code": "invalidClientIdOrSecret", "message": "invalid clientId or secret"}
        )
    )
    with pytest.raises(ConfigError) as excinfo:
        await client(max_retries=3, cache=None).get_token()
    assert route.call_count == 1
    assert "无效" in str(excinfo.value)
    assert m.current(m.TOKEN_REFRESH_FAILED) == 1


@respx.mock
async def test_server_error_is_retried_then_fails() -> None:
    route = respx.post(URL).mock(
        return_value=httpx.Response(500, json={"code": "SystemError", "message": "boom"})
    )
    with pytest.raises(ModelUnavailable):
        await client(max_retries=2, cache=None).get_token()
    assert route.call_count == 3  # 首次 + 2 次重试


@respx.mock
async def test_missing_token_in_response_is_rejected() -> None:
    respx.post(URL).mock(return_value=httpx.Response(200, json={"expireIn": 7200}))
    with pytest.raises(ModelUnavailable):
        await client(cache=None).get_token()
    assert m.current(m.TOKEN_REFRESH_FAILED) == 1


@respx.mock
async def test_expire_gauge_is_published(runtime) -> None:
    """``ekbs_dingtalk_token_expire_seconds`` 是 P3 告警（< 300s）的输入。"""
    respx.post(URL).mock(
        return_value=httpx.Response(200, json={"accessToken": "tok-4", "expireIn": 7200})
    )
    await client(CacheStore(runtime.db, runtime.settings)).get_token()
    remaining = m.current(m.TOKEN_EXPIRE)
    assert 7100 <= remaining <= 7200, remaining


@respx.mock
async def test_status_does_not_hit_network(runtime) -> None:
    route = respx.post(URL).mock(
        return_value=httpx.Response(200, json={"accessToken": "tok-5", "expireIn": 7200})
    )
    token_client = client(CacheStore(runtime.db, runtime.settings))
    assert (await token_client.status()).present is False
    await token_client.get_token()
    status = await token_client.status()
    assert status.present is True and status.cached is True and status.remaining_seconds > 0
    assert route.call_count == 1  # status() 不该发起请求


@respx.mock
async def test_force_refresh_bypasses_cache(runtime) -> None:
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(200, json={"accessToken": "tok-a", "expireIn": 7200}),
            httpx.Response(200, json={"accessToken": "tok-b", "expireIn": 7200}),
        ]
    )
    token_client = client(CacheStore(runtime.db, runtime.settings))
    assert await token_client.get_token() == "tok-a"
    assert await token_client.get_token(force=True) == "tok-b"
    assert route.call_count == 2


@respx.mock
async def test_invalidate_forces_new_token(runtime) -> None:
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(200, json={"accessToken": "tok-x", "expireIn": 7200}),
            httpx.Response(200, json={"accessToken": "tok-y", "expireIn": 7200}),
        ]
    )
    token_client = client(CacheStore(runtime.db, runtime.settings))
    await token_client.get_token()
    await token_client.invalidate()
    assert await token_client.get_token() == "tok-y"
    assert route.call_count == 2


def test_missing_credentials_fail_fast() -> None:
    with pytest.raises(ConfigError) as excinfo:
        DingTalkTokenClient(app_key="", app_secret="x")
    assert "DINGTALK_CLIENT_ID" in str(excinfo.value)


@respx.mock
async def test_cache_key_template_uses_app_key(runtime) -> None:
    respx.post(URL).mock(
        return_value=httpx.Response(200, json={"accessToken": "tok-6", "expireIn": 7200})
    )
    cache = CacheStore(runtime.db, runtime.settings)
    await client(cache).get_token()
    with runtime.db.sync_connect() as conn:
        key = conn.execute(
            "SELECT cache_key FROM ekbs_cache WHERE cache_key LIKE 'ekbs:dt:%'"
        ).fetchone()["cache_key"]
    assert key == "ekbs:dt:app_token:dingtest-key"


# ---------------------------------------------------------------------------
# H5 免登（docs/spec/50 §4，已核实）
# ---------------------------------------------------------------------------


@respx.mock
async def test_h5_login_resolves_user_and_consumes_code_once(seeded) -> None:
    """免登主链路：authCode → topapi/v2/user/getuserinfo → 映射 ekbs 用户。

    两处契约要点：旧版路径的 token 走**查询参数**；authCode 一次性。
    """
    from ekbs.cache.store import CacheStore
    from ekbs.dingtalk.auth import DingTalkAuthService
    from ekbs.errors import PermissionDenied
    from tests.conftest import user_id_of

    runtime, _summary = seeded
    respx.post(URL).mock(
        return_value=httpx.Response(200, json={"accessToken": "tok-auth", "expireIn": 7200})
    )
    route = respx.post(url__regex=r"https://oapi\.dingtalk\.com/topapi/v2/user/getuserinfo.*").mock(
        return_value=httpx.Response(
            200,
            json={"errcode": 0, "errmsg": "ok", "result": {"userid": "demo_alice", "name": "Alice"}},
        )
    )
    service = DingTalkAuthService(
        db=runtime.db, settings=runtime.settings,
        token_client=client(CacheStore(runtime.db, runtime.settings)),
        cache=CacheStore(runtime.db, runtime.settings),
    )
    identity = await service.resolve_user("authcode-1")
    assert identity["user_id"] == user_id_of(runtime.db, "demo_alice")
    assert identity["dingtalk_userid"] == "demo_alice"

    request = route.calls[0].request
    assert "access_token=tok-auth" in str(request.url), "旧版路径必须用查询参数传 token"
    assert "x-acs-dingtalk-access-token" not in {k.lower() for k in request.headers}
    import json

    assert json.loads(request.content) == {"code": "authcode-1"}

    # 同一个 authCode 第二次必须被拒（一次性）
    with pytest.raises(PermissionDenied):
        await service.resolve_user("authcode-1")
    assert m.current(m.AUTH_REPLAY) == 1


@respx.mock
async def test_h5_login_rejects_unmapped_user(seeded) -> None:
    from ekbs.cache.store import CacheStore
    from ekbs.dingtalk.auth import DingTalkAuthService
    from ekbs.errors import PermissionDenied

    runtime, _summary = seeded
    respx.post(URL).mock(
        return_value=httpx.Response(200, json={"accessToken": "tok", "expireIn": 7200})
    )
    respx.post(url__regex=r"https://oapi\.dingtalk\.com/topapi/v2/user/getuserinfo.*").mock(
        return_value=httpx.Response(200, json={"errcode": 0, "result": {"userid": "ghost"}})
    )
    service = DingTalkAuthService(
        db=runtime.db, settings=runtime.settings,
        token_client=client(CacheStore(runtime.db, runtime.settings)),
        cache=CacheStore(runtime.db, runtime.settings),
    )
    with pytest.raises(PermissionDenied) as excinfo:
        await service.resolve_user("authcode-2")
    assert "尚未同步" in str(excinfo.value)
    assert m.current(m.AUTH_LOGIN, result="failure") == 1


@respx.mock
async def test_h5_login_surfaces_dingtalk_error(seeded) -> None:
    from ekbs.cache.store import CacheStore
    from ekbs.dingtalk.auth import DingTalkAuthService
    from ekbs.errors import PermissionDenied

    runtime, _summary = seeded
    respx.post(URL).mock(
        return_value=httpx.Response(200, json={"accessToken": "tok", "expireIn": 7200})
    )
    respx.post(url__regex=r"https://oapi\.dingtalk\.com/topapi/v2/user/getuserinfo.*").mock(
        return_value=httpx.Response(200, json={"errcode": 40078, "errmsg": "invalid auth code"})
    )
    service = DingTalkAuthService(
        db=runtime.db, settings=runtime.settings,
        token_client=client(CacheStore(runtime.db, runtime.settings)),
        cache=CacheStore(runtime.db, runtime.settings),
    )
    with pytest.raises(PermissionDenied) as excinfo:
        await service.resolve_user("authcode-3")
    assert "40078" in str(excinfo.value)
