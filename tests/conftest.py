"""测试夹具。

原则：**每个用例用独立的临时数据目录**（``tmp_path``），互不污染；
嵌入与重排一律走 ``mock`` provider，因此整套测试**不出网、不花钱、可重复**。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DEV_CONFIG = REPO_ROOT / "config" / "application.dev.yaml"

#: 测试用的内部回环 token 与 JWT 密钥（只存在于测试进程环境变量里）
TEST_INTERNAL_TOKEN = "test-internal-token"
TEST_JWT_SECRET = "test-jwt-secret"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """所有用例都跑在 mock provider + 临时数据目录上。"""
    monkeypatch.setenv("EKBS_INTERNAL_TOKEN", TEST_INTERNAL_TOKEN)
    monkeypatch.setenv("EKBS_JWT_SECRET", TEST_JWT_SECRET)
    # 避免继承外部环境造成的不确定（例如误配真实 provider）
    for key in ("DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def settings(tmp_path: Path):
    from ekbs.config import load_settings

    return load_settings(
        DEV_CONFIG,
        data_dir=tmp_path / "var",
        db_path=tmp_path / "var" / "index" / "ekbs.db",
    )


@pytest.fixture
def db(settings):
    from ekbs.db import Database

    database = Database(settings)
    database.apply_schema()
    return database


@pytest.fixture
def runtime(settings, db):
    """运行时实例。依赖 ``db`` 夹具，确保 Schema 已初始化（否则启动自检会拒绝）。"""
    from ekbs.runtime import Runtime

    instance = Runtime.create(settings, service="test")
    yield instance
    asyncio.run(instance.aclose())


@pytest.fixture
async def seeded(runtime):
    """运行时 + 一套合成语料（3 部门 / 3 用户 / 3 文档）。"""
    from ekbs.tools.seed_demo import seed

    summary = await seed(runtime.settings, runtime=runtime)
    return runtime, summary


def user_id_of(db, dingtalk_userid: str) -> int:
    with db.sync_connect() as conn:
        row = conn.execute(
            "SELECT user_id FROM ekbs_user WHERE dingtalk_userid = ?", (dingtalk_userid,)
        ).fetchone()
    assert row is not None, f"未找到用户 {dingtalk_userid}"
    return int(row["user_id"])


async def make_client(app):
    """带 lifespan 的 ASGI 测试客户端（httpx 的 ASGITransport 不会自动跑 lifespan）。"""
    from httpx import ASGITransport, AsyncClient

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")
