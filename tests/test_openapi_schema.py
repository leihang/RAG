"""接口契约与中文文档的回归测试。

对应 `02-测试方案与验收标准.md` 点名的 ``tests/test_openapi_schema.py`` 与验收项 **E4**
（「接口文档（OpenAPI 3.1）与本文档用例的接口契约一致」）。

这里锁三件事：

1. **契约面存在且是 3.1**：`/openapi.json` 是 OpenAPI 3.1（`02 §102` 要求「编码前定契约：
   OpenAPI 3.1」），并且能被 schemathesis 这类工具直接消费；
2. **中文文档不回归**：标题/分组/摘要必须是中文 —— 摘要是从函数 docstring 第一行取的，
   新写接口若忘了写 docstring，摘要就会退回函数名的英文（`Qa Chat` 这种），
   这条用例就是拦这个的；
3. **文档页可交互**：`securitySchemes` 不能为空 —— 空的时候 Swagger 的「授权」按钮
   根本不出现，所有接口点一下都 401（本仓库实测踩过）。
"""

from __future__ import annotations

import re

import pytest
from httpx import ASGITransport, AsyncClient

from ekbs.api.app import create_app

CJK = re.compile(r"[\u4e00-\u9fff]")


def _operations(schema: dict) -> list[tuple[str, str, dict]]:
    return [
        (path, method, operation)
        for path, item in schema.get("paths", {}).items()
        for method, operation in item.items()
        if method in ("get", "post", "put", "delete", "patch")
    ]


@pytest.fixture()
def schema(seeded) -> dict:
    runtime, _ = seeded
    return create_app(runtime.settings).openapi()


def test_openapi_is_31_and_has_operations(schema: dict) -> None:
    """契约版本与规模：`02 §102` 要 OpenAPI 3.1；接口数不能悄悄归零。"""
    assert schema["openapi"].startswith("3.1"), f"应为 OpenAPI 3.1，实际 {schema['openapi']}"
    operations = _operations(schema)
    assert len(operations) >= 100, f"接口数异常偏少：{len(operations)}"


def test_every_operation_has_chinese_summary(schema: dict) -> None:
    """摘要必须中文 —— 否则说明该接口没写 docstring（会退回函数名的英文）。"""
    offenders = [
        f"{method.upper()} {path} → {operation.get('summary')!r}"
        for path, method, operation in _operations(schema)
        if not CJK.search(operation.get("summary") or "")
    ]
    assert not offenders, "以下接口的中文摘要缺失（给函数补一行中文 docstring 即可）：\n  " + "\n  ".join(offenders)


def test_tags_are_chinese_and_ordered(schema: dict) -> None:
    """分组名与分组说明都要中文，且顺序稳定（文档页按这个顺序渲染）。"""
    tags = schema.get("tags") or []
    assert tags, "未声明 openapi_tags，文档页分组会退化成英文标识符"
    for tag in tags:
        assert CJK.search(tag["name"]), f"分组名未中文化：{tag['name']}"
        assert CJK.search(tag.get("description", "")), f"分组说明缺失：{tag['name']}"

    declared = {tag["name"] for tag in tags}
    used = {name for _, _, op in _operations(schema) for name in (op.get("tags") or [])}
    assert used <= declared, f"有接口用了未声明的分组：{sorted(used - declared)}"


def test_security_schemes_are_declared(schema: dict) -> None:
    """`securitySchemes` 为空时 Swagger 的「授权」按钮不出现，接口点不动（实测踩过）。"""
    schemes = (schema.get("components") or {}).get("securitySchemes") or {}
    assert "BearerAuth" in schemes, "缺少 Bearer 授权声明，文档页无法填令牌"
    assert schemes["BearerAuth"]["scheme"] == "bearer"


def test_core_contract_paths_are_present(schema: dict) -> None:
    """关键路径必须在契约里（`02 §4.7`/`05 §8.2` 的接口清单口径）。"""
    paths = set(schema["paths"])
    for required in (
        "/livez", "/healthz", "/readyz",
        "/api/v1/qa/chat", "/api/v1/qa/retrieve",
        "/api/v1/auth/me", "/api/v1/perm/me",
    ):
        assert required in paths, f"契约缺少关键路径：{required}"


async def test_chinese_docs_page_renders(seeded) -> None:
    """`/docs` 要是中文页：中文标题 + 注入的界面文案替换脚本。"""
    runtime, _ = seeded
    app = create_app(runtime.settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/docs")
    assert response.status_code == 200
    body = response.text
    assert "EKBS 企业知识中枢 API" in body, "文档页标题未中文化"
    assert "swagger-ui" in body.lower(), "文档页未加载 Swagger UI 资源"
    # 注入的替换脚本（Swagger UI 没有官方 i18n，只能做 DOM 文本替换）
    assert "Authorize" in body and "授权" in body, "界面文案替换脚本未注入"


@pytest.mark.parametrize(
    "path,expected_status",
    [("/", 200), ("/ui", 200), ("/docs", 200), ("/openapi.json", 200), ("/favicon.ico", 204)],
)
async def test_human_facing_entry_points_do_not_404(seeded, path: str, expected_status: int) -> None:
    """**回归**：这些入口曾经直接 404。

    实测踩到两类：① 根路径 `/` 没建路由 → 只拿到 `{"detail":"Not Found"}`，
    对「访问服务」的人等于没有入口；② `/favicon.ico` 没处理 → 浏览器必定请求它，
    开发者工具 Network 里留下一条 404，排查真问题时容易误判成服务故障。
    """
    runtime, _ = seeded
    app = create_app(runtime.settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(path)
    assert response.status_code == expected_status, f"{path} → {response.status_code}"


async def test_redoc_redirects_instead_of_404(seeded) -> None:
    """`/redoc` 曾出现在交付说明里；关掉英文 ReDoc 后不能变成 404，要重定向到中文文档页。"""
    runtime, _ = seeded
    app = create_app(runtime.settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/redoc", follow_redirects=False)
    assert response.status_code in (301, 302, 307, 308), f"/redoc 应重定向，实际 {response.status_code}"
    assert response.headers["location"] == "/docs"


async def test_openapi_json_endpoint_serves_the_contract(seeded) -> None:
    """`/openapi.json` 必须可被工具直接消费（`02 §2076` 用 schemathesis 打它）。"""
    runtime, _ = seeded
    app = create_app(runtime.settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/openapi.json")
    assert response.status_code == 200
    body = response.json()
    assert body["openapi"].startswith("3.1")
    assert CJK.search(body["info"]["title"])
