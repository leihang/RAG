"""管理台前端托管（`ekbs/api/admin_web.py`）的回归测试。

这层出错的症状**全都长得像"页面坏了"，但接口全绿**，所以必须逐条钉住：

* 深链刷新 404（缺 SPA 回落）→ 用户以为功能没了；
* `index.html` 被缓存（发版后浏览器拿旧 index 去要已删除的哈希资源）→ 白屏；
* 带哈希资源没有长缓存 → 每次访问都重下 1 MB bundle；
* 缺失资源回落成 HTML → 浏览器报"Unexpected token '<'"，与真因无关；
* 路径穿越 → 读服务器任意文件。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from ekbs.api.admin_web import NO_CACHE, find_web_dir, install_admin_web


@pytest.fixture
def web_dist(tmp_path):
    """造一份最小产物：index.html + 带哈希的 assets/。"""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!DOCTYPE html><div id=root></div>", encoding="utf-8")
    (dist / "assets" / "index.abc123.js").write_text("console.log(1)", encoding="utf-8")
    (dist / "favicon.ico").write_bytes(b"\x00\x01")
    return dist


@pytest.fixture
def app_with_web(web_dist, monkeypatch) -> FastAPI:
    monkeypatch.setenv("EKBS_ADMIN_WEB_DIR", str(web_dist))
    app = FastAPI()
    assert install_admin_web(app, settings=None) == web_dist

    @app.get("/api/v1/ping")
    async def ping() -> dict[str, bool]:  # pragma: no cover - 只用于确认互不干扰
        return {"ok": True}

    return app


@pytest.fixture
async def client(app_with_web):
    async with AsyncClient(
        transport=ASGITransport(app=app_with_web), base_url="http://testserver"
    ) as instance:
        yield instance


async def test_root_serves_spa_index_with_no_cache(client) -> None:
    resp = await client.get("/admin")
    assert resp.status_code == 200
    assert "id=root" in resp.text
    assert resp.headers["cache-control"] == NO_CACHE["Cache-Control"]


async def test_deep_link_falls_back_to_index(client) -> None:
    """`/admin/docs/list` 这类前端路由服务端没有对应文件，必须回落成 index.html。"""
    for path in ("/admin/docs/list", "/admin/iam/users", "/admin/logs/qa/abc-123"):
        resp = await client.get(path)
        assert resp.status_code == 200, path
        assert "id=root" in resp.text, path


async def test_hashed_asset_gets_immutable_cache(client) -> None:
    resp = await client.get("/admin/assets/index.abc123.js")
    assert resp.status_code == 200
    assert resp.text == "console.log(1)"
    assert "immutable" in resp.headers["cache-control"]


async def test_missing_asset_is_404_not_html(client) -> None:
    """缺失资源回落成 HTML 会让浏览器报与真因无关的语法错误，必须 404。"""
    resp = await client.get("/admin/assets/gone.999.js")
    assert resp.status_code == 404


async def test_real_file_other_than_assets_is_served(client) -> None:
    resp = await client.get("/admin/favicon.ico")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == NO_CACHE["Cache-Control"]


async def test_path_traversal_is_rejected(client, tmp_path) -> None:
    """产物目录之外的路径一律 404，且不得回落成 index（否则等于能探文件系统）。"""
    secret = tmp_path / "outside.txt"
    secret.write_text("绝密", encoding="utf-8")
    for evil in ("/admin/../outside.txt", "/admin/..%2Foutside.txt", "/admin/../../etc/passwd"):
        resp = await client.get(evil)
        assert resp.status_code in (404, 400), f"{evil} 实得 {resp.status_code}"
        assert "绝密" not in resp.text


async def test_api_routes_are_not_shadowed(client) -> None:
    """SPA 通配路由不能把 /api 吃掉（前缀不同，但这条容易被后续改动破坏）。"""
    resp = await client.get("/api/v1/ping")
    assert resp.status_code == 200 and resp.json() == {"ok": True}


def test_find_web_dir_prefers_env(web_dist, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("EKBS_ADMIN_WEB_DIR", str(web_dist))
    assert find_web_dir(settings=None) == web_dist

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("EKBS_ADMIN_WEB_DIR", str(empty))
    # 环境变量指向空目录时应继续往后找，而不是返回一个没有 index.html 的目录
    found = find_web_dir(settings=None)
    assert found is None or (found / "index.html").is_file()


def test_install_returns_none_when_dist_missing(monkeypatch, tmp_path) -> None:
    """产物缺失时必须**安全降级**（旧的单文件管理台继续可用），而不是抛异常让应用起不来。"""
    monkeypatch.setenv("EKBS_ADMIN_WEB_DIR", str(tmp_path / "nope"))
    monkeypatch.setattr("ekbs.api.admin_web.PROD_WEB_DIR", tmp_path / "nope2")
    monkeypatch.setattr("ekbs.api.admin_web.REPO_WEB_DIR", tmp_path / "nope3")
    app = FastAPI()
    assert install_admin_web(app, settings=None) is None
    # 降级时不该注册任何 /admin 路由，交给旧的单文件管理台
    assert not [r for r in app.routes if getattr(r, "path", "").startswith("/admin")]


def test_find_web_dir_skips_dir_without_index(monkeypatch, tmp_path) -> None:
    """目录存在但没有 index.html（例如只拷了 assets）不算可用产物。"""
    fake = tmp_path / "half"
    (fake / "assets").mkdir(parents=True)
    monkeypatch.setenv("EKBS_ADMIN_WEB_DIR", str(fake))
    monkeypatch.setattr("ekbs.api.admin_web.PROD_WEB_DIR", tmp_path / "none")
    monkeypatch.setattr("ekbs.api.admin_web.REPO_WEB_DIR", tmp_path / "none2")
    assert find_web_dir(settings=None) is None


async def test_dev_users_shape(client, seeded) -> None:
    from httpx import ASGITransport, AsyncClient
    from ekbs.api.app import create_app

    runtime, _ = seeded
    app = create_app(runtime.settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            resp = await c.get("/api/v1/auth/dev-users")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert items, "演示身份列表不该为空"
    for item in items:
        assert {"user_id", "dingtalk_userid", "clearance", "roles"} <= set(item)
    # 登录页要靠这些字段渲染下拉；nick 允许为空但角色要是数组
    assert all(isinstance(i["roles"], list) for i in items)


def test_dev_users_is_404_in_production(seeded) -> None:
    """生产环境必须 404（与 dev-token 同一道闸）。"""
    import asyncio

    from httpx import ASGITransport, AsyncClient
    from ekbs.api.app import create_app

    runtime, _ = seeded
    runtime.settings.app.env = "prod"

    async def run() -> int:
        app = create_app(runtime.settings)
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
                resp = await c.get("/api/v1/auth/dev-users")
                return resp.status_code

    assert asyncio.run(run()) == 404


# ---------------------------------------------------------------------------
# 上传扩展名归一化（用户实测踩到：.xlsx 被判"不支持的类型"）
# ---------------------------------------------------------------------------


def test_normalize_suffix_handles_real_world_filenames() -> None:
    """真实文件名里的"看不见的字符"不该让一个 .xlsx 被判成不支持。

    踩到的现场：用户传 `报表.xlsx` 被拒。裸取法 `PurePath(name).suffix` 在这三种
    文件名下都会取错，而它们都来自最普通的来源（Windows/邮件下载、中文输入法）。
    """
    from ekbs.ingest.parser import normalize_suffix

    assert normalize_suffix("报表.xlsx") == "xlsx"
    assert normalize_suffix("报表.xlsx ") == "xlsx"          # 尾部空格
    assert normalize_suffix("  报表.XLSX  ") == "xlsx"        # 前后空格 + 大写
    assert normalize_suffix("报表．xlsx") == "xlsx"           # 全角句点
    assert normalize_suffix("报表.xlsx.") == "xlsx"           # 尾部多余点
    assert normalize_suffix("汇总表.2026.xlsx") == "xlsx"     # 名字里有点
    assert normalize_suffix("无扩展名") == ""
    assert normalize_suffix(".gitignore") == ""
    assert normalize_suffix("") == ""


async def test_upload_init_accepts_awkward_xlsx_names(seeded) -> None:
    """经 HTTP 验一遍：这些文件名都必须被接受（否则用户根本传不上来）。"""
    from httpx import ASGITransport, AsyncClient
    from ekbs.api.app import create_app
    from tests.conftest import TEST_INTERNAL_TOKEN, user_id_of

    runtime, _ = seeded
    uid = user_id_of(runtime.db, "demo_carol")
    headers = {"X-Internal-Token": TEST_INTERNAL_TOKEN, "X-Internal-User-Id": str(uid)}
    app = create_app(runtime.settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            for name in ("报表.xlsx", "报表.xlsx ", "报表．xlsx", "报表.xlsx.", "  报表.XLSX"):
                resp = await c.post(
                    "/api/v1/docs/upload/init",
                    headers=headers,
                    json={"filename": name, "size_bytes": 1024, "level": 2, "visible_dept_ids": ["102"]},
                )
                assert resp.status_code == 200, f"{name!r} 被拒：{resp.text}"


async def test_upload_init_still_rejects_truly_unsupported(seeded) -> None:
    """归一化不能把"真不支持"也放过（别为了修 bug 把闸门拆了）。"""
    from httpx import ASGITransport, AsyncClient
    from ekbs.api.app import create_app
    from tests.conftest import TEST_INTERNAL_TOKEN, user_id_of

    runtime, _ = seeded
    uid = user_id_of(runtime.db, "demo_carol")
    headers = {"X-Internal-Token": TEST_INTERNAL_TOKEN, "X-Internal-User-Id": str(uid)}
    app = create_app(runtime.settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            # `.html` 自 2026-10-03 起已支持（标准库解析）—— 不能再当"真不支持"的例子；
            # 换成仍无解析分支的 `.ppt`（白名单里有、解析器没有 → 上传接口 422）
            for name in ("木马.exe", "演示.ppt ", "培训.ppt．", "没有后缀"):
                resp = await c.post(
                    "/api/v1/docs/upload/init",
                    headers=headers,
                    json={"filename": name, "size_bytes": 1024, "level": 2, "visible_dept_ids": ["102"]},
                )
                assert resp.status_code == 422, f"{name!r} 不该被放行：{resp.status_code}"


# ---------------------------------------------------------------------------
# 管理台前端依赖的响应形状（页面白屏的头号原因）
# ---------------------------------------------------------------------------


async def test_frontend_facing_response_shapes(seeded) -> None:
    """把前端**实际读的字段**钉住。

    实测踩到：我按"扁平对象"写了文档详情页，而 `/docs/documents/{id}` 返回的是
    `{document, chunks, shards, citation_count}`；解析结果页读 `markdown`，
    后端给的是 `preview`。这类"页面写完了但从没和真实报文对齐"的 bug，
    只有打开页面才看得到 —— 所以在这里把形状钉死。
    """
    from httpx import ASGITransport, AsyncClient
    from ekbs.api.app import create_app
    from tests.conftest import TEST_INTERNAL_TOKEN, user_id_of

    runtime, summary = seeded
    uid = user_id_of(runtime.db, "demo_carol")
    headers = {"X-Internal-Token": TEST_INTERNAL_TOKEN, "X-Internal-User-Id": str(uid)}
    app = create_app(runtime.settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            # 列表：扁平的 items
            lst = (await c.get("/api/v1/docs/documents?limit=1", headers=headers)).json()
            assert {"items", "total"} <= set(lst)
            doc_id = lst["items"][0]["doc_id"]
            assert "chunk_count" in lst["items"][0], "列表项应有 chunk_count"

            # 详情：嵌套 document + 分块统计
            det = (await c.get(f"/api/v1/docs/documents/{doc_id}", headers=headers)).json()
            assert "document" in det, f"详情必须是嵌套结构，实得 {list(det)[:5]}"
            assert {"chunks", "shards"} <= set(det)
            assert "chunk_count" not in det["document"], (
                "document 里没有 chunk_count —— 前端若读它就会显示 0；"
                "分块数必须从 chunks 求和"
            )
            assert isinstance(det["chunks"], dict)

            # 解析任务详情：嵌套 task + state_machine
            task = (await c.get(f"/api/v1/parse/tasks/{doc_id}", headers=headers)).json()
            assert {"task", "state_machine"} <= set(task)
            assert "status" in task["task"]

            # 解析结果：字段是 preview（不是 markdown）
            res = (await c.get(f"/api/v1/parse/tasks/{doc_id}/result", headers=headers)).json()
            assert "preview" in res, f"结果预览字段应为 preview，实得 {list(res)}"
            assert "markdown" not in res, "后端不提供 markdown 字段，前端不要按它读"

            # 分块树：parents/children 是**计数**，层级在 tree 里（每个父节点内嵌 children）
            tree = (await c.get(f"/api/v1/chunks/documents/{doc_id}/tree", headers=headers)).json()
            assert {"document", "parents", "children", "tree"} <= set(tree)
            assert isinstance(tree["parents"], int), "parents 是计数不是数组（前端曾当数组用导致崩溃）"
            assert isinstance(tree["children"], int)
            assert isinstance(tree["tree"], list)
            if tree["tree"]:
                node = tree["tree"][0]
                assert "chunk_id" in node and "chunk_type" in node
                assert isinstance(node.get("children"), list), "每个父节点内嵌自己的 children"


# ---------------------------------------------------------------------------
# 上传的「知识库必填」与「目录结构保留」
# ---------------------------------------------------------------------------


async def _upload_client(seeded):
    from httpx import ASGITransport, AsyncClient
    from ekbs.api.app import create_app
    from tests.conftest import TEST_INTERNAL_TOKEN, user_id_of

    runtime, _ = seeded
    uid = user_id_of(runtime.db, "demo_carol")
    app = create_app(runtime.settings)
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")
    return runtime, app, client, {"X-Internal-Token": TEST_INTERNAL_TOKEN, "X-Internal-User-Id": str(uid)}


async def test_upload_without_kb_is_rejected_with_actionable_message(seeded) -> None:
    """不给 kb_id/kb_code 时后端在 **complete** 阶段才拒（422）。

    这是用户实际踩到的：「上传 xlsx 提示提交内容不符合要求」。
    前端曾经只有一个空的「知识库 code」文本框，留空就走到这里；
    而错误文案又被状态码兜底盖住，看不出真正原因。
    接口这层的契约要守住：**原因必须是一句能指导操作的话**。
    """
    from ekbs.api.app import create_app

    runtime, app, client, headers = await _upload_client(seeded)
    async with app.router.lifespan_context(app):
        async with client as c:
            init = await c.post(
                "/api/v1/docs/upload/init",
                headers=headers,
                json={"filename": "预算表.xlsx", "size_bytes": 4, "level": 2, "visible_dept_ids": ["102"]},
            )
            assert init.status_code == 200, init.text
            upload_id = init.json()["upload_id"]
            await c.post(
                f"/api/v1/docs/upload/{upload_id}/chunk",
                headers=headers,
                files={"file": ("预算表.xlsx", b"abcd", "application/octet-stream")},
                data={"index": "0"},
            )
            done = await c.post(f"/api/v1/docs/upload/{upload_id}/complete", headers=headers)
            assert done.status_code == 422
            detail = done.json()["detail"]
            assert "kb_id" in detail and "kb_code" in detail, detail


async def test_upload_preserves_folder_structure(seeded) -> None:
    """带相对路径上传时，目录层级要落到 raw 下，并成为文档标题。

    用户反馈「上传文件夹时文件夹被分离了，只上传文件」——因为后端原来用
    `Path(filename).stem` 当标题、落盘只用 basename，整段目录都被丢掉。
    """
    from ekbs.api.app import create_app

    runtime, app, client, headers = await _upload_client(seeded)
    async with app.router.lifespan_context(app):
        async with client as c:
            body = b"col1,col2\n1,2\n"
            rel = "制度/2026/预算表.xlsx"
            init = await c.post(
                "/api/v1/docs/upload/init",
                headers=headers,
                json={
                    "filename": rel,
                    "title": "制度/2026/预算表",
                    "size_bytes": len(body),
                    "kb_id": 1,
                    "level": 1,
                },
            )
            assert init.status_code == 200, init.text
            upload_id = init.json()["upload_id"]
            await c.post(
                f"/api/v1/docs/upload/{upload_id}/chunk",
                headers=headers,
                files={"file": ("part0", body, "application/octet-stream")},
                data={"index": "0"},
            )
            done = await c.post(f"/api/v1/docs/upload/{upload_id}/complete", headers=headers)
            assert done.status_code == 200, done.text
            path = done.json()["path"]
            assert path.endswith("制度/2026/预算表.xlsx"), path
            doc_id = done.json()["doc_id"]

    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT title, source_path FROM ekbs_document WHERE doc_id = ?", (doc_id,)
        ).fetchone()
    assert row["title"] == "制度/2026/预算表", row["title"]
    assert "制度/2026/" in row["source_path"]


def test_safe_relative_name_blocks_escapes() -> None:
    """相对路径是客户端给的，必须自己挡：绝对路径、`..`、隐藏段。"""
    from ekbs.api.docs import safe_relative_name

    assert safe_relative_name("制度/2026/预算表.xlsx") == "制度/2026/预算表.xlsx"
    assert safe_relative_name("../../etc/passwd") == "etc/passwd"
    assert safe_relative_name("/abs/x.md") == "abs/x.md"
    assert safe_relative_name("a//b/./c.md") == "a/b/c.md"
    assert safe_relative_name(".hidden/x") == "x"
    assert safe_relative_name("..") == "未命名"
    assert safe_relative_name("") == "未命名"


async def test_failed_upload_leaves_no_orphan_file(seeded) -> None:
    """校验失败时**不得**在 raw 里留下孤儿文件。

    原先 `complete` 先 `shutil.move` 再校验 `kb_id`，所以每次 422 都会在 raw 下
    留一份没有文档记录的拷贝 —— 用户重试一次就多一份垃圾，而库里查不到，
    看起来像"上传没生效"（实测在 raw 里抓到过两份）。
    """
    from pathlib import Path

    from ekbs.api.app import create_app
    from ekbs.ingest.parser import normalize_suffix

    runtime, app, client, headers = await _upload_client(seeded)
    body = b"orphan-check-content"
    import hashlib

    digest = hashlib.sha256(body).hexdigest()
    raw_dir = Path(runtime.settings.data_paths()["raw"]) / digest[:16]
    assert not raw_dir.exists(), "前置条件：该内容此前不该存在"

    async with app.router.lifespan_context(app):
        async with client as c:
            init = await c.post(
                "/api/v1/docs/upload/init",
                headers=headers,
                json={"filename": "孤儿检查.xlsx", "size_bytes": len(body), "level": 2, "visible_dept_ids": ["102"]},
            )
            upload_id = init.json()["upload_id"]
            await c.post(
                f"/api/v1/docs/upload/{upload_id}/chunk",
                headers=headers,
                files={"file": ("part0", body, "application/octet-stream")},
                data={"index": "0"},
            )
            done = await c.post(f"/api/v1/docs/upload/{upload_id}/complete", headers=headers)
            assert done.status_code == 422  # 没给 kb_id
    assert normalize_suffix("孤儿检查.xlsx") == "xlsx"
    assert not raw_dir.exists(), f"失败的 422 上传在 raw 里留下了孤儿文件：{raw_dir}"


async def test_upload_hash_is_streamed_not_whole_file(seeded, monkeypatch) -> None:
    """合并分片时必须**流式**哈希，不能把整个文件读进内存。

    原先 `hashlib.sha256(merged.read_bytes())` 一次性读入 —— 上限放宽后
    这等于凭空多一个与文件等大的内存峰值，与「16 GB 机器不让峰值叠加」冲突。
    这里用「read_bytes 被调用就失败」来证明新实现确实没走那条路。
    """
    from pathlib import Path

    from ekbs.api.app import create_app

    runtime, app, client, headers = await _upload_client(seeded)

    def _boom(self: Path) -> bytes:  # pragma: no cover - 只应在回归时触发
        raise AssertionError("合并流程调用了 Path.read_bytes()，说明又把整个文件读进内存了")

    monkeypatch.setattr(Path, "read_bytes", _boom)

    body = b"streamed-hash-check" * 100
    async with app.router.lifespan_context(app):
        async with client as c:
            init = await c.post(
                "/api/v1/docs/upload/init",
                headers=headers,
                json={"filename": "流式哈希.xlsx", "size_bytes": len(body), "kb_id": 1, "level": 1},
            )
            upload_id = init.json()["upload_id"]
            await c.post(
                f"/api/v1/docs/upload/{upload_id}/chunk",
                headers=headers,
                files={"file": ("part0", body, "application/octet-stream")},
                data={"index": "0"},
            )
            done = await c.post(f"/api/v1/docs/upload/{upload_id}/complete", headers=headers)
            assert done.status_code == 200, done.text
            import hashlib

            assert done.json()["checksum"] == hashlib.sha256(body).hexdigest()


async def test_upload_rejects_l2_without_dept_up_front(seeded) -> None:
    """L2/L3 不给部门必须在**上传入口**就拒，不能等 worker 异步失败。

    用户实测：界面显示"上传成功"，而文档其实是 `failed` —— 原因
    「L2 文档未指定 visible_dept_ids」只出现在 worker 日志里。
    上传成功与入库成功是两件事，界面上必须能区分。
    """
    from ekbs.api.app import create_app

    runtime, app, client, headers = await _upload_client(seeded)
    async with app.router.lifespan_context(app):
        async with client as c:
            # L2 无部门 → 立刻 422，且原因可操作
            bad = await c.post(
                "/api/v1/docs/upload/init",
                headers=headers,
                json={"filename": "内部资料.xlsx", "size_bytes": 10, "kb_id": 1, "level": 2},
            )
            assert bad.status_code == 422, bad.text
            detail = bad.json()["detail"]
            assert "部门" in detail or "visible_dept_ids" in detail, detail

            # L2 + 给了部门 → 放行
            ok = await c.post(
                "/api/v1/docs/upload/init",
                headers=headers,
                json={
                    "filename": "内部资料.xlsx", "size_bytes": 10, "kb_id": 1,
                    "level": 2, "visible_dept_ids": ["102"],
                },
            )
            assert ok.status_code == 200, ok.text

            # L4 未开启（l4_enabled 默认 false）→ 也应在入口拒
            l4 = await c.post(
                "/api/v1/docs/upload/init",
                headers=headers,
                json={
                    "filename": "绝密.xlsx", "size_bytes": 10, "kb_id": 1,
                    "level": 4, "visible_dept_ids": ["102"],
                },
            )
            assert l4.status_code == 422, l4.text


def test_scan_reports_missing_dept_as_blocker(seeded) -> None:
    """目录导入的扫描阶段也要把「L2 没给部门」报成 blocker（界面能提前看到）。"""
    from ekbs.ingest.dir_import import scan_directory

    runtime, _ = seeded
    result = scan_directory(runtime.settings, runtime.db, rel_dir="", level=2, dept_ids=[])
    assert any("部门" in b or "visible_dept_ids" in b for b in result.blockers), result.blockers
    # 给了部门就不该再有这条 blocker
    ok = scan_directory(runtime.settings, runtime.db, rel_dir="", level=2, dept_ids=["102"])
    assert not any("visible_dept_ids" in b for b in ok.blockers), ok.blockers


# ---------------------------------------------------------------------------
# 知识库管理（方案 05 §8.1 的 /kb/knowledge-bases；本轮补齐）
# ---------------------------------------------------------------------------


async def test_kb_crud_and_delete_guard(seeded) -> None:
    """知识库能建、能改、能查；**库里有文档时不许删**（避免误删整库语料）。

    补这个接口的原因：在此之前只有 CLI 能建库（`ekbs.tools.import.ensure_kb`），
    管理台里只有种子造的那一个可选 —— 用起来就像演示程序。
    """
    from httpx import ASGITransport, AsyncClient
    from ekbs.api.app import create_app
    from tests.conftest import TEST_INTERNAL_TOKEN, user_id_of

    runtime, _ = seeded
    uid = user_id_of(runtime.db, "demo_carol")   # kb_admin 有 kb:knowledge_base:* 权限点
    headers = {"X-Internal-Token": TEST_INTERNAL_TOKEN, "X-Internal-User-Id": str(uid)}
    app = create_app(runtime.settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            listed = await c.get("/api/v1/kb/knowledge-bases", headers=headers)
            assert listed.status_code == 200, listed.text
            assert listed.json()["total"] >= 1
            assert "document_count" in listed.json()["items"][0]

            created = await c.post(
                "/api/v1/kb/knowledge-bases",
                headers=headers,
                json={"code": "policy", "name": "制度与流程库", "default_level": 2},
            )
            assert created.status_code == 201, created.text
            kb_id = created.json()["kb_id"]
            assert created.json()["name"] == "制度与流程库"

            # code 重复 → 409（它是稳定标识，不能撞）
            dup = await c.post(
                "/api/v1/kb/knowledge-bases",
                headers=headers,
                json={"code": "policy", "name": "另一个"},
            )
            assert dup.status_code == 409, dup.text

            # code 形态校验：不接受中文/大写（它要进分片名与命令行）
            bad = await c.post(
                "/api/v1/kb/knowledge-bases",
                headers=headers,
                json={"code": "制度库", "name": "中文 code"},
            )
            assert bad.status_code == 422, bad.text

            renamed = await c.put(
                f"/api/v1/kb/knowledge-bases/{kb_id}", headers=headers, json={"name": "制度库"}
            )
            assert renamed.status_code == 200 and renamed.json()["name"] == "制度库"

            # 删除按方案 §3.2 只授予 super_admin（kb_admin 没有这个点）
            # —— 上面 kb_admin 建/改能过、这里必须 403，正是目录在起作用
            denied = await c.delete(f"/api/v1/kb/knowledge-bases/{kb_id}", headers=headers)
            assert denied.status_code == 403, denied.text

            su_id = user_id_of(runtime.db, "demo_root")
            with runtime.db.sync_connect() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO ekbs_user_role(user_id, role_id, scope_type, scope_id) "
                    "SELECT ?, role_id, 'global', 0 FROM ekbs_role WHERE code = 'super_admin'",
                    (su_id,),
                )
                conn.commit()
            su_headers = {"X-Internal-Token": TEST_INTERNAL_TOKEN, "X-Internal-User-Id": str(su_id)}

            # 空库可以删
            deleted = await c.delete(f"/api/v1/kb/knowledge-bases/{kb_id}", headers=su_headers)
            assert deleted.status_code == 200, deleted.text

            # 有文档的库不许删（避免误删整库语料）
            with runtime.db.sync_connect() as conn:
                used = int(conn.execute("SELECT kb_id FROM ekbs_kb ORDER BY kb_id LIMIT 1").fetchone()[0])
            blocked = await c.delete(f"/api/v1/kb/knowledge-bases/{used}", headers=su_headers)
            assert blocked.status_code == 409, blocked.text
            assert "还有" in blocked.json()["detail"]


async def test_upload_lands_under_kb_code_not_sha_dir(seeded) -> None:
    """落盘布局：`raw/<知识库 code>/<相对路径>`，不再是 `raw/<sha16>/…` 那种壳子。

    旧的 `95fbf1ef16573acf/` 在文件管理器里完全认不出是什么；去重本来就在库里
    按 checksum 做（秒传），不依赖目录名。
    """
    from pathlib import Path

    from ekbs.api.app import create_app

    runtime, app, client, headers = await _upload_client(seeded)
    body = b"kb-layout-check"
    async with app.router.lifespan_context(app):
        async with client as c:
            init = await c.post(
                "/api/v1/docs/upload/init",
                headers=headers,
                json={
                    "filename": "制度/年假.md", "title": "制度/年假",
                    "size_bytes": len(body), "kb_id": 1, "level": 1,
                },
            )
            upload_id = init.json()["upload_id"]
            await c.post(
                f"/api/v1/docs/upload/{upload_id}/chunk",
                headers=headers,
                files={"file": ("p0", body, "application/octet-stream")},
                data={"index": "0"},
            )
            done = await c.post(f"/api/v1/docs/upload/{upload_id}/complete", headers=headers)
            assert done.status_code == 200, done.text
            path = Path(done.json()["path"])
    raw_root = Path(runtime.settings.data_paths()["raw"])
    rel = path.relative_to(raw_root)
    # 第一段必须是知识库 code（可读），不是 16 位十六进制
    assert rel.parts[0] == "main", f"落盘第一段应是知识库 code，实得 {rel.parts[0]}"
    assert rel.as_posix() == "main/制度/年假.md", rel.as_posix()


# ---------------------------------------------------------------------------
# 上传行为参数（方案 05 §6.5 的 upload.chunk_size_mb / concurrent / max_retry）
# ---------------------------------------------------------------------------


def test_upload_config_is_single_source() -> None:
    """三份配置都要有 `upload` 段，且默认值符合方案 §6.5（8 MB / 并发 3 / 重试 3）。

    这些值是**给界面用的默认值**：分片大小决定切片粒度，并发决定同时传几个文件。
    放在服务端配置里，改配置即生效，不用重新构建前端。
    """
    from pathlib import Path

    from ekbs.config import load_settings

    root = Path(__file__).resolve().parent.parent
    for name in ("application.dev.yaml", "application.cloud.yaml", "application.yaml"):
        path = root / "config" / name if name != "application.yaml" else root / "deploy" / "config" / name
        assert path.is_file(), f"配置文件不存在：{path}"
        cfg = load_settings(path)
        assert cfg.upload.chunk_size_mb == 8, name
        assert cfg.upload.concurrent == 3, name
        assert cfg.upload.max_retry == 3, name


async def test_chunk_size_hint_comes_from_config(seeded) -> None:
    """`init` 返回的分片大小必须来自配置，不能是硬编码的 5 MB。

    原先写死 `5 * 1024 * 1024`：配置里没有 `upload` 段、方案要的是 8 MB，
    界面拿到的提示与实际配置对不上（"改了配置没生效"这类问题的典型来源）。
    """
    from ekbs.api.app import create_app

    runtime, app, client, headers = await _upload_client(seeded)
    expected = int(runtime.settings.upload.chunk_size_mb) * 1024 * 1024
    assert expected == 8 * 1024 * 1024
    async with app.router.lifespan_context(app):
        async with client as c:
            init = await c.post(
                "/api/v1/docs/upload/init",
                headers=headers,
                json={"filename": "分片大小.xlsx", "size_bytes": 10, "kb_id": 1, "level": 1},
            )
            assert init.status_code == 200, init.text
            assert init.json()["chunk_size_hint"] == expected


async def test_import_meta_exposes_upload_defaults(seeded) -> None:
    """`/import/meta` 要把 upload 三项报给界面 —— 界面不再写死并发数与重试次数。"""
    from httpx import ASGITransport, AsyncClient
    from ekbs.api.app import create_app
    from tests.conftest import TEST_INTERNAL_TOKEN, user_id_of

    runtime, _ = seeded
    uid = user_id_of(runtime.db, "demo_carol")
    headers = {"X-Internal-Token": TEST_INTERNAL_TOKEN, "X-Internal-User-Id": str(uid)}
    app = create_app(runtime.settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            meta = (await c.get("/api/v1/import/meta", headers=headers)).json()
    up = meta.get("upload") or {}
    assert up.get("chunk_size_mb") == 8
    assert up.get("concurrent") == 3
    assert up.get("max_retry") == 3
