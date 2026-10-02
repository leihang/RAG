"""管理台「导入」页的后端：**服务器目录导入**（`ekbs/api/imports.py` + `ekbs/ingest/dir_import.py`）。

这个功能把「读服务器上的任意文件并入库」变成了一个网页动作，所以本文件的重心不是
"能不能导"，而是**边界**：

1. **路径只能落在 raw 之内** —— `..`、绝对路径、指向外部的软链接三种逃逸都要挡住
   （否则任何有 ``doc:document:create`` 的账号都能把 ``/etc/passwd`` 导进向量库）；
2. **闸门不能被界面绕过** —— 合规闸（真实语料出网确认）与 L3 闸要走与 CLI 同一套判据；
3. **目录导入只登记、不解析** —— 置 ``status='pending'`` 交给 worker，否则网页请求会挂几十分钟；
4. **去重口径与 CLI/上传一致** —— 内容 checksum，重复的文件不重复入库。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from ekbs.api.app import create_app
from ekbs.errors import ConfigError
from ekbs.ingest.dir_import import (
    ACTION_DUPLICATE,
    ACTION_NEW,
    ACTION_TOO_LARGE,
    ImportPathError,
    enqueue_files,
    list_children,
    raw_root,
    resolve_within_raw,
    scan_directory,
)
from tests.conftest import TEST_INTERNAL_TOKEN, user_id_of


def headers_for(user_id: int) -> dict[str, str]:
    return {"X-Internal-Token": TEST_INTERNAL_TOKEN, "X-Internal-User-Id": str(user_id)}


def grant_role(runtime, dingtalk_userid: str, role_code: str) -> int:
    user_id = user_id_of(runtime.db, dingtalk_userid)
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO ekbs_user_role(user_id, role_id, scope_type, scope_id) "
            "SELECT ?, role_id, 'global', 0 FROM ekbs_role WHERE code = ?",
            (user_id, role_code),
        )
        conn.commit()
    return user_id


def open_compliance(runtime) -> None:
    """打开合规闸（真实语料出网确认）—— 生产上由人工确认，测试里直接置位。

    **必须同时把 provider 说成云端**：``assert_compliance_gate`` 对 ``mock``/``local``
    是按设计豁免的（不出网就没有出网风险），只置 ack 是走不到闸门的。
    """
    runtime.settings.models.embed.provider = "dashscope"
    runtime.settings.permission.corpus_egress_ack = True


def close_compliance_gate(runtime) -> None:
    """模拟生产现状：云端 provider + 未取得合规确认 → 闸门应当拦住导入。"""
    runtime.settings.models.embed.provider = "dashscope"
    runtime.settings.permission.corpus_egress_ack = False


def make_raw_files(runtime, layout: dict[str, str]) -> None:
    """在 raw 下造文件：``{"制度/差旅.md": "正文"}``。"""
    root = raw_root(runtime.settings)
    for rel, text in layout.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


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


# ---------------------------------------------------------------------------
# 1. 路径边界（最重要）
# ---------------------------------------------------------------------------


def test_assert_path_within_raw_accepts_absolute_under_raw(runtime) -> None:
    from ekbs.ingest.dir_import import assert_path_within_raw

    make_raw_files(runtime, {"制度/a.md": "正文"})
    abs_path = raw_root(runtime.settings) / "制度" / "a.md"
    assert assert_path_within_raw(runtime.settings, abs_path) == abs_path.resolve()
    assert assert_path_within_raw(runtime.settings, "制度/a.md") == abs_path.resolve()


def test_assert_path_within_raw_rejects_escape(runtime, tmp_path) -> None:
    from ekbs.ingest.dir_import import assert_path_within_raw

    with pytest.raises(ImportPathError):
        assert_path_within_raw(runtime.settings, "/etc/passwd")
    with pytest.raises(ImportPathError):
        assert_path_within_raw(runtime.settings, tmp_path / "outside.md")


def test_dotdot_escape_is_rejected(runtime) -> None:
    for evil in ("../etc/passwd", "制度/../../../../etc/passwd", "..", "../.."):
        with pytest.raises(ImportPathError):
            resolve_within_raw(runtime.settings, evil)


def test_absolute_path_is_rejected(runtime) -> None:
    for evil in ("/etc/passwd", "/data/ekbs/raw/ok.md"):
        with pytest.raises(ImportPathError):
            resolve_within_raw(runtime.settings, evil)


def test_symlink_pointing_outside_raw_is_rejected(runtime, tmp_path) -> None:
    """软链接也要挡住：``raw/link`` → ``/tmp`` 是最容易漏的一种逃逸。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("机密内容", encoding="utf-8")
    root = raw_root(runtime.settings)
    root.mkdir(parents=True, exist_ok=True)
    link = root / "escape"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ImportPathError):
        resolve_within_raw(runtime.settings, "escape/secret.md")
    # 目录浏览也不该把它列出来（列出即等于暗示可用）
    names = [d["name"] for d in list_children(runtime.settings)["dirs"]]
    assert "escape" not in names


def test_internal_symlink_is_allowed(runtime) -> None:
    """raw 内部的软链接是正常用法，不该被误伤。"""
    make_raw_files(runtime, {"真目录/a.md": "x"})
    root = raw_root(runtime.settings)
    (root / "别名").symlink_to(root / "真目录", target_is_directory=True)
    assert resolve_within_raw(runtime.settings, "别名/a.md").name == "a.md"


# ---------------------------------------------------------------------------
# 2. 中文多级子目录与目录浏览
# ---------------------------------------------------------------------------


def test_chinese_nested_dirs_are_listed_with_counts(runtime) -> None:
    make_raw_files(
        runtime,
        {
            "人事制度/考勤/考勤管理办法.md": "x",
            "人事制度/考勤/请假流程.md": "y",
            "人事制度/薪酬/薪酬制度.md": "z",
            "财务制度/报销.md": "w",
            "说明.txt": "顶层文件",
        },
    )
    top = list_children(runtime.settings)
    names = {d["name"]: d for d in top["dirs"]}
    assert set(names) == {"人事制度", "财务制度"}
    # 计数含子目录：人事制度 3 份
    assert names["人事制度"]["importable_files"] == 3
    assert names["人事制度"]["has_children"] is True

    sub = list_children(runtime.settings, "人事制度")
    assert {d["name"] for d in sub["dirs"]} == {"考勤", "薪酬"}
    assert sub["parent"] == ""
    third = list_children(runtime.settings, "人事制度/考勤")
    assert third["dirs"] == []
    assert third["rel_path"] == "人事制度/考勤"

    from ekbs.ingest.dir_import import list_descendant_dirs

    flat = list_descendant_dirs(runtime.settings, "")
    assert "人事制度" in flat
    assert "人事制度/考勤" in flat
    assert "人事制度/薪酬" in flat
    assert "财务制度" in flat
    under = list_descendant_dirs(runtime.settings, "人事制度")
    assert under == ["考勤", "薪酬"] or set(under) == {"考勤", "薪酬"}


def test_unsupported_suffixes_are_not_counted_or_scanned(runtime) -> None:
    make_raw_files(runtime, {"a.md": "x", "b.csv": "x", "c.exe": "x", "d.json": "x"})
    result = scan_directory(runtime.settings, runtime.db)
    rel = {c.rel_path for c in result.candidates}
    assert rel == {"a.md"}, f".csv/.exe/.json 不该被收：{rel}"


# ---------------------------------------------------------------------------
# 3. 扫描分类
# ---------------------------------------------------------------------------


def test_scan_classifies_new_duplicate_and_too_large(seeded) -> None:
    runtime, _ = seeded
    open_compliance(runtime)
    make_raw_files(runtime, {"新文件.md": "新内容", "重复.md": "同样的内容"})
    # 先入库一份，制造 checksum 重复
    root = raw_root(runtime.settings)
    (root / "同内容.md").write_text("同样的内容", encoding="utf-8")
    kb_id = _first_kb_id(runtime)
    enqueue_files(
        runtime.settings, runtime.db,
        rel_paths=["同内容.md"], kb_id=kb_id, level=1,
    )
    # 让 worker 之外的路径也能被去重识别：直接查 checksum
    import hashlib

    checksum = hashlib.sha256("同样的内容".encode()).hexdigest()
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_document SET checksum = ?, status = 'published' "
            "WHERE source_path LIKE '%同内容.md'",
            (checksum,),
        )
        conn.commit()

    runtime.settings.parse.max_file_mb = 1
    (root / "太大.md").write_text("x" * (2 * 1024 * 1024), encoding="utf-8")

    result = scan_directory(runtime.settings, runtime.db)
    by_rel = {c.rel_path: c for c in result.candidates}
    assert by_rel["新文件.md"].action == ACTION_NEW
    assert by_rel["重复.md"].action == ACTION_DUPLICATE
    assert by_rel["同内容.md"].action == ACTION_DUPLICATE
    assert by_rel["太大.md"].action == ACTION_TOO_LARGE
    assert result.importable == 1
    # **全量**可导入路径与预览截断无关
    assert result.importable_paths() == ["新文件.md"]


def test_scan_reports_blockers_instead_of_raising(runtime) -> None:
    """合规闸未开时：扫描要把原因列出来（界面要显示），而不是抛异常让用户看 500。"""
    make_raw_files(runtime, {"a.md": "x"})
    close_compliance_gate(runtime)
    result = scan_directory(runtime.settings, runtime.db)
    assert result.blockers, "合规闸关闭时扫描应当报出 blocker"
    assert any("合规闸" in b for b in result.blockers)


def test_scan_warns_when_directory_has_nothing_importable(runtime) -> None:
    make_raw_files(runtime, {"a.csv": "x"})
    result = scan_directory(runtime.settings, runtime.db)
    assert result.total_files == 0
    assert result.warn, "空目录要给出提示，否则用户以为界面坏了"


# ---------------------------------------------------------------------------
# 4. 登记（只入队，不解析）
# ---------------------------------------------------------------------------


def test_enqueue_registers_pending_documents_only(seeded) -> None:
    runtime, _ = seeded
    open_compliance(runtime)
    make_raw_files(runtime, {"制度/差旅.md": "# 差旅\n\n住宿 600 元。", "制度/年假.md": "# 年假"})
    kb_id = _first_kb_id(runtime)
    outcome = enqueue_files(
        runtime.settings, runtime.db,
        rel_paths=["制度/差旅.md", "制度/年假.md"],
        kb_id=kb_id, level=1, dept_ids=["102"], created_by=1,
    )
    assert len(outcome["enqueued"]) == 2
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT doc_id, status, title, source_path, level, visible_dept_ids, size_bytes "
            "FROM ekbs_document ORDER BY doc_id DESC LIMIT 2"
        ).fetchall()
    assert all(r["status"] == "pending" for r in rows), "登记后必须是 pending，解析交给 worker"
    assert all(r["size_bytes"] for r in rows), "size_bytes 要落库（解析侧另有体积闸）"
    assert all(str(r["source_path"]).startswith(str(raw_root(runtime.settings))) for r in rows)
    assert {r["title"] for r in rows} == {"差旅", "年假"}
    assert '"102"' in str(rows[0]["visible_dept_ids"])


def test_enqueue_skips_duplicates_and_rejects_unsupported(seeded) -> None:
    runtime, _ = seeded
    open_compliance(runtime)
    make_raw_files(runtime, {"a.md": "同一份", "b.md": "同一份", "c.csv": "x"})
    kb_id = _first_kb_id(runtime)
    first = enqueue_files(runtime.settings, runtime.db, rel_paths=["a.md"], kb_id=kb_id, level=1)
    assert len(first["enqueued"]) == 1
    # a.md 已入队（pending 也算），再导 a.md 应跳过；b.md 与本批 a.md 内容相同也跳过
    second = enqueue_files(
        runtime.settings, runtime.db, rel_paths=["a.md", "b.md", "c.csv"], kb_id=kb_id, level=1
    )
    assert second["enqueued"] == []
    assert len(second["skipped"]) == 2
    assert len(second["failed"]) == 1
    assert "不支持的格式" in second["failed"][0]["error"]


def test_enqueue_blocks_when_compliance_gate_closed(seeded) -> None:
    runtime, _ = seeded
    close_compliance_gate(runtime)
    make_raw_files(runtime, {"a.md": "x"})
    kb_id = _first_kb_id(runtime)
    with pytest.raises(ConfigError) as excinfo:
        enqueue_files(runtime.settings, runtime.db, rel_paths=["a.md"], kb_id=kb_id, level=1)
    assert "合规闸" in str(excinfo.value)


def test_enqueue_refuses_instead_of_silently_truncating(seeded, monkeypatch) -> None:
    runtime, _ = seeded
    """超过单次上限时**明确报错**，不能静默只导一部分。"""
    open_compliance(runtime)
    monkeypatch.setattr("ekbs.ingest.dir_import.ENQUEUE_LIMIT", 1)
    make_raw_files(runtime, {"a.md": "a", "b.md": "b"})
    kb_id = _first_kb_id(runtime)
    with pytest.raises(ConfigError) as excinfo:
        enqueue_files(
            runtime.settings, runtime.db, rel_paths=["a.md", "b.md"], kb_id=kb_id, level=1
        )
    assert "最多登记" in str(excinfo.value)
    with runtime.db.sync_connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM ekbs_document").fetchone()[0]
    assert n == 3, "报错时不该已经写进去一半"


def test_enqueue_path_escape_is_recorded_as_failure(seeded) -> None:
    runtime, _ = seeded
    open_compliance(runtime)
    kb_id = _first_kb_id(runtime)
    outcome = enqueue_files(
        runtime.settings, runtime.db, rel_paths=["../etc/passwd"], kb_id=kb_id, level=1
    )
    assert outcome["enqueued"] == []
    assert len(outcome["failed"]) == 1


async def test_enqueued_document_is_picked_up_by_worker(seeded) -> None:
    runtime, _ = seeded
    """**端到端**：登记 → worker 解析 → published（目录导入的整条闭环）。"""
    open_compliance(runtime)
    make_raw_files(runtime, {"制度/差旅.md": "# 差旅费管理办法\n\n## 住宿标准\n\n一线城市每晚 600 元。"})
    kb_id = _first_kb_id(runtime)
    outcome = enqueue_files(
        runtime.settings, runtime.db, rel_paths=["制度/差旅.md"], kb_id=kb_id, level=1
    )
    doc_id = outcome["enqueued"][0]["doc_id"]

    from ekbs.ingest.pipeline import IngestPipeline
    from ekbs.worker import process_once

    pipeline = IngestPipeline(
        db=runtime.db, settings=runtime.settings, providers=runtime.providers,
        shards=runtime.shards, fts=runtime.fts,
    )
    assert await process_once(runtime, pipeline) is True
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT status, checksum, title FROM ekbs_document WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        chunks = conn.execute(
            "SELECT COUNT(*) FROM ekbs_chunk WHERE doc_id = ?", (doc_id,)
        ).fetchone()[0]
    assert row["status"] == "published"
    assert chunks > 0, "worker 解析后应有分块"
    # **checksum 必须仍是「文件内容哈希」，不能被 markdown 内容哈希覆盖**
    # （worker 不传 checksum 时 pipeline 会用 markdown 哈希回写，秒传/去重口径当场失真）
    import hashlib

    expected = hashlib.sha256(
        (raw_root(runtime.settings) / "制度/差旅.md").read_bytes()
    ).hexdigest()
    assert row["checksum"] == expected, "文档 checksum 应等于文件内容 SHA-256"


# ---------------------------------------------------------------------------
# 5. HTTP 层：权限与错误语义
# ---------------------------------------------------------------------------


async def test_import_endpoints_require_permission(client, seeded) -> None:
    runtime, _ = seeded
    # end_user 没有管理权限点 —— 用它验证导入接口确实按 doc:document:create 把关
    reader = grant_role(runtime, "demo_alice", "end_user")
    for method, path, body in (
        ("get", "/api/v1/import/meta", None),
        ("get", "/api/v1/import/dirs", None),
        ("get", "/api/v1/import/queue", None),
        ("post", "/api/v1/import/scan", {"path": "", "level": 1}),
        ("post", "/api/v1/import/run", {"path": "", "kb_id": _first_kb_id(runtime), "level": 1}),
    ):
        resp = await getattr(client, method)(
            path, headers=headers_for(reader), **({"json": body} if body is not None else {})
        )
        assert resp.status_code in (401, 403), f"{path} 应要求 doc:document:create，实得 {resp.status_code}"


async def test_import_meta_exposes_two_lists(client, seeded) -> None:
    """界面要先知道「目录导入能收 63 种、上传只能收 11 种」，否则用户挑完文件才吃 422。"""
    runtime, _ = seeded
    admin = grant_role(runtime, "demo_alice", "super_admin")
    resp = await client.get("/api/v1/import/meta", headers=headers_for(admin))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["dir_extensions"]) == 63
    assert len(data["upload_extensions"]) == 11
    assert "py" in data["dir_extensions"] and "py" not in data["upload_extensions"]
    assert set(data["upload_blocked"]) == {"html", "ppt"}
    assert data["knowledge_bases"], "要能选知识库"
    assert str(data["raw_root"]).endswith("raw")
    # 目录导入页要把「密级」默认成该库的 default_level，**必须由 meta 直接给**：
    # 否则界面得另调 `/kb/knowledge-bases`（要 `kb:knowledge_base:read`），
    # 只有 `doc:document:create` 的角色拿不到 → 密级悄悄回落 L1（比预期更公开）。
    assert "default_level" in data["knowledge_bases"][0]


async def test_import_scan_blocks_escape_with_403(client, seeded) -> None:
    runtime, _ = seeded
    admin = grant_role(runtime, "demo_alice", "super_admin")
    resp = await client.post(
        "/api/v1/import/scan", headers=headers_for(admin), json={"path": "../..", "level": 1}
    )
    assert resp.status_code == 403, resp.text


async def test_import_run_returns_409_when_gate_closed(client, seeded) -> None:
    runtime, _ = seeded
    admin = grant_role(runtime, "demo_alice", "super_admin")
    close_compliance_gate(runtime)
    make_raw_files(runtime, {"a.md": "x"})
    resp = await client.post(
        "/api/v1/import/run",
        headers=headers_for(admin),
        json={"path": "", "kb_id": _first_kb_id(runtime), "level": 1},
    )
    assert resp.status_code == 409, resp.text
    assert "合规闸" in resp.json()["detail"]


async def test_import_run_end_to_end_via_http(client, seeded) -> None:
    """管理员从界面走完一条：看目录 → 扫描 → 执行 → 队列里出现 pending。"""
    runtime, _ = seeded
    open_compliance(runtime)
    admin = grant_role(runtime, "demo_alice", "super_admin")
    make_raw_files(runtime, {"人事制度/考勤/考勤办法.md": "# 考勤\n\n迟到扣款。"})

    dirs = await client.get("/api/v1/import/dirs", headers=headers_for(admin))
    assert dirs.status_code == 200
    assert {d["name"] for d in dirs.json()["dirs"]} == {"人事制度"}

    scan = await client.post(
        "/api/v1/import/scan", headers=headers_for(admin), json={"path": "", "level": 1}
    )
    assert scan.status_code == 200, scan.text
    assert scan.json()["importable"] == 1
    assert scan.json()["blockers"] == []

    run = await client.post(
        "/api/v1/import/run",
        headers=headers_for(admin),
        json={
            "path": "人事制度", "kb_id": _first_kb_id(runtime), "level": 1, "dept_ids": ["102"],
        },
    )
    assert run.status_code == 200, run.text
    assert run.json()["queued"] == 1

    queue = await client.get("/api/v1/import/queue", headers=headers_for(admin))
    assert queue.json()["counts"].get("pending") == 1
    assert "考勤办法" in queue.json()["recent"][0]["title"]


def _first_kb_id(runtime) -> int:
    with runtime.db.sync_connect() as conn:
        return int(conn.execute("SELECT kb_id FROM ekbs_kb ORDER BY kb_id LIMIT 1").fetchone()["kb_id"])
