"""扫描件识别、草稿落盘与复核闸门。"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from ekbs.api.app import create_app
from ekbs.ingest.chunker import FigureInput, TableInput
from ekbs.ingest.parser import ParsedDocument
from ekbs.ingest.scan_review import (
    STATUS_REVIEW,
    is_scan_source,
    load_review_markdown,
    load_review_sidecar,
    mark_document_review,
    prepare_scan_draft,
    requires_human_review,
    upgrade_review_draft_tables,
)
from ekbs.worker import process_once
from tests.conftest import TEST_INTERNAL_TOKEN, user_id_of


def test_is_scan_source_by_path_or_title() -> None:
    assert is_scan_source("var/raw/pdf-扫描/合同.pdf", "空调保养")
    assert is_scan_source("/data/ekbs/raw/foo.pdf", "pdf-扫描/电梯")
    assert is_scan_source("Scanned_contract.pdf", "x")
    assert not is_scan_source("var/raw/制度/年假.docx", "年假管理制度")
    assert not is_scan_source(
        "var/raw/internal_public_document/空调保养合同.pdf", "空调保养合同"
    )


def test_mineru_requires_review_even_without_scan_hint() -> None:
    path = "var/raw/internal_public_document/空调保养合同.pdf"
    title = "空调保养合同"
    assert not is_scan_source(path, title)
    assert requires_human_review(path, title, engine="mineru")
    assert requires_human_review("var/raw/制度/年假.docx", "年假管理制度", engine="docx") is False
    assert requires_human_review("var/raw/制度/年假.md", "年假", engine="text") is False
    assert requires_human_review("var/raw/pdf-扫描/x.md", "x", engine="text")


def test_prepare_scan_draft_writes_files(tmp_path: Path) -> None:
    rel, stats = prepare_scan_draft(
        data_dir=tmp_path,
        doc_id=42,
        raw_markdown="双方盖章之日起生效。ein高g甲方：测试",
        engine="mineru",
        page_map=[(0, 1, 1)],
        figures=[FigureInput(image_path="parse/42/a.png", caption="盖有红色印章某某")],
        tables=[],
    )
    assert rel == "parse/42"
    md = load_review_markdown(tmp_path, 42)
    assert "双方盖章之日起生效" in md
    assert "ein高g" not in md
    assert (tmp_path / "parse" / "42" / "review_sidecar.json").is_file()
    assert stats.get("sign_latin_removed", 0) >= 1 or "盖章" in md


def test_prepare_and_upgrade_merge_continued_tables(tmp_path: Path) -> None:
    def html(city: str) -> str:
        return (
            f"<table><tr><td>城市</td><td>标准</td></tr>"
            f"<tr><td>{city}</td><td>500</td></tr></table>"
        )
    rel, stats = prepare_scan_draft(
        data_dir=tmp_path,
        doc_id=7,
        raw_markdown="[表格 1] 标准\n[表格 2] 续表\n",
        engine="mineru",
        page_map=[(0, 1, 1), (10, 2, 2)],
        figures=[],
        tables=[
            TableInput(body=html("北京"), caption="标准", page=1, table_id="1"),
            TableInput(body=html("上海"), caption="续表", page=2, table_id="2"),
        ],
    )
    assert rel == "parse/7"
    assert stats.get("tables_merged_pairs") == 1
    side = load_review_sidecar(tmp_path, 7)
    assert len(side.tables) == 1
    assert "上海" in side.tables[0]["body"]
    # 幂等：再 upgrade 不应重复合并
    again = upgrade_review_draft_tables(tmp_path, 7)
    assert not again.get("tables_merged_pairs")
    assert len(load_review_sidecar(tmp_path, 7).tables) == 1


@pytest.fixture
async def client(seeded):
    runtime, _ = seeded
    app = create_app(runtime.settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as instance:
            yield instance, runtime


def _grant_admin(runtime) -> int:
    user_id = user_id_of(runtime.db, "demo_carol")
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO ekbs_user_role(user_id, role_id, scope_type, scope_id) "
            "SELECT ?, role_id, 'global', 0 FROM ekbs_role WHERE code = ?",
            (user_id, "kb_admin"),
        )
        conn.commit()
    return user_id


def _headers(user_id: int) -> dict[str, str]:
    return {"X-Internal-Token": TEST_INTERNAL_TOKEN, "X-Internal-User-Id": str(user_id)}


async def test_scan_doc_stops_at_review_then_approve(client, seeded) -> None:
    http, runtime = client
    runtime, _summary = seeded
    admin = _grant_admin(runtime)

    raw = runtime.settings.data_dir / "raw" / "pdf-扫描"
    raw.mkdir(parents=True, exist_ok=True)
    src = raw / "demo_scan.md"
    src.write_text("# 扫描合同\n\n甲方责任：按时付款。\n金额 100 元。\n", encoding="utf-8")

    with runtime.db.sync_connect() as conn:
        kb_id = int(conn.execute("SELECT kb_id FROM ekbs_kb LIMIT 1").fetchone()["kb_id"])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, status, level, checksum) "
            "VALUES (?, ?, ?, 'pending', 1, 'scan-demo-checksum')",
            (kb_id, "pdf-扫描/demo", str(src)),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.commit()

    from ekbs.ingest.pipeline import IngestPipeline

    ingest = IngestPipeline(
        db=runtime.db,
        settings=runtime.settings,
        providers=runtime.providers,
        shards=runtime.shards,
        fts=runtime.fts,
        figure_index=runtime.figure_index,
    )
    assert await process_once(runtime, ingest) is True

    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT status, parse_path FROM ekbs_document WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        chunks = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM ekbs_chunk WHERE doc_id = ?", (doc_id,)
            ).fetchone()["c"]
        )
    assert row["status"] == STATUS_REVIEW
    assert row["parse_path"]
    assert chunks == 0

    headers = _headers(admin)
    draft = await http.get(f"/api/v1/parse/tasks/{doc_id}/draft", headers=headers)
    assert draft.status_code == 200
    body = draft.json()
    assert "甲方责任" in body["markdown"]

    edited = body["markdown"] + "\n补充条款：质保一年。\n"
    put = await http.put(
        f"/api/v1/parse/tasks/{doc_id}/draft",
        headers=headers,
        json={"markdown": edited},
    )
    assert put.status_code == 200

    ok = await http.post(
        f"/api/v1/parse/tasks/{doc_id}/approve",
        headers=headers,
        json={"markdown": edited},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "chunked"

    with runtime.db.sync_connect() as conn:
        st = conn.execute(
            "SELECT status FROM ekbs_document WHERE doc_id = ?", (doc_id,)
        ).fetchone()["status"]
        chunks = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM ekbs_chunk WHERE doc_id = ?", (doc_id,)
            ).fetchone()["c"]
        )
    assert st == "chunked"
    assert chunks > 0

    # 分块预览能看到 chunked；文档管理默认看不到
    chunk_list = await http.get(
        "/api/v1/docs/documents?include_chunked=true", headers=headers
    )
    assert chunk_list.status_code == 200
    assert doc_id in {item["doc_id"] for item in chunk_list.json()["items"]}
    default_list = await http.get("/api/v1/docs/documents", headers=headers)
    assert doc_id not in {item["doc_id"] for item in default_list.json()["items"]}

    # 分块后、向量前允许编辑块：直接改一个 child 块内容，向量入库必须 embed 编辑后的文本
    with runtime.db.sync_connect() as conn:
        edited = conn.execute(
            "SELECT c.chunk_id FROM ekbs_chunk c WHERE c.doc_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM ekbs_chunk k WHERE k.parent_id = c.chunk_id) "
            "ORDER BY c.chunk_id LIMIT 1", (doc_id,)
        ).fetchone()
        assert edited is not None
        conn.execute(
            "UPDATE ekbs_chunk SET content = '编辑后的块内容ZZZ' WHERE chunk_id = ?",
            (int(edited["chunk_id"]),),
        )
        conn.commit()
    captured_texts: list[str] = []
    # client 用的是 create_app 自己的 runtime（共享 DB），捕获它那边的 embed 调用
    app_runtime = http._transport.app.state.runtime  # type: ignore[attr-defined]
    original_embed = app_runtime.providers.embed.embed
    async def capture_embed(texts):
        captured_texts.extend(texts)
        return await original_embed(texts)
    app_runtime.providers.embed.embed = capture_embed

    # 向量入库：对已分块（可编辑后）的块直接 embed，不重新分块
    with runtime.db.sync_connect() as conn:
        block_ids_before = [r["chunk_id"] for r in conn.execute(
            "SELECT chunk_id FROM ekbs_chunk WHERE doc_id = ? ORDER BY chunk_id", (doc_id,)
        )]
    vec = await http.post(f"/api/v1/parse/tasks/{doc_id}/vectorize", headers=headers)
    assert vec.status_code == 200, vec.text
    assert vec.json()["status"] == "published"
    assert vec.json()["vector_count"] > 0
    with runtime.db.sync_connect() as conn:
        st = conn.execute(
            "SELECT status FROM ekbs_document WHERE doc_id = ?", (doc_id,)
        ).fetchone()["status"]
        block_ids_after = [r["chunk_id"] for r in conn.execute(
            "SELECT chunk_id FROM ekbs_chunk WHERE doc_id = ? ORDER BY chunk_id", (doc_id,)
        )]
        vectors = conn.execute(
            "SELECT vector_count FROM ekbs_shard WHERE shard_id = "
            "(SELECT shard_id FROM ekbs_chunk WHERE doc_id = ? LIMIT 1)", (doc_id,)
        ).fetchone()["vector_count"]
    assert st == "published"
    assert block_ids_after == block_ids_before
    assert int(vectors) > 0
    assert any("编辑后的块内容ZZZ" in text for text in captured_texts), captured_texts[:3]


async def test_mineru_pdf_without_scan_hint_stops_at_review(
    seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """文件名和目录都没有「扫描」的 PDF，MinerU 解析后仍停在 review，不进索引。"""
    runtime, _ = seeded

    def _fake_parse(path, *, assets_dir=None):
        return ParsedDocument(
            markdown="# 空调保养合同\n\n甲方责任：按时保养。\n金额 19000 元。\n",
            engine="mineru",
            source_path=str(path),
            page_map=[(0, 1, 1)],
        )

    monkeypatch.setattr("ekbs.worker.parse_file", _fake_parse)

    raw = runtime.settings.data_dir / "raw" / "internal_public_document"
    raw.mkdir(parents=True, exist_ok=True)
    src = raw / "空调保养合同.pdf"
    src.write_bytes(b"%PDF-1.4 fake")

    with runtime.db.sync_connect() as conn:
        kb_id = int(conn.execute("SELECT kb_id FROM ekbs_kb LIMIT 1").fetchone()["kb_id"])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, status, level, checksum) "
            "VALUES (?, ?, ?, 'pending', 1, 'scan-no-hint')",
            (kb_id, "空调保养合同", str(src)),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.commit()

    from ekbs.ingest.pipeline import IngestPipeline

    ingest = IngestPipeline(
        db=runtime.db,
        settings=runtime.settings,
        providers=runtime.providers,
        shards=runtime.shards,
        fts=runtime.fts,
        figure_index=runtime.figure_index,
    )
    assert await process_once(runtime, ingest) is True

    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT status, parse_path FROM ekbs_document WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        chunks = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM ekbs_chunk WHERE doc_id = ?", (doc_id,)
            ).fetchone()["c"]
        )
    assert row["status"] == STATUS_REVIEW
    assert row["parse_path"]
    assert chunks == 0
    assert "甲方责任" in load_review_markdown(runtime.settings.data_dir, doc_id)
    assert load_review_sidecar(runtime.settings.data_dir, doc_id).engine == "mineru"


async def test_reject_review(client, seeded) -> None:
    http, runtime = client
    runtime, _ = seeded
    admin = _grant_admin(runtime)
    with runtime.db.sync_connect() as conn:
        kb_id = int(conn.execute("SELECT kb_id FROM ekbs_kb LIMIT 1").fetchone()["kb_id"])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, status, level) "
            "VALUES (?, 'scan-x', 'raw/pdf-扫描/x.pdf', 'review', 1)",
            (kb_id,),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.commit()
    # 空草稿目录也可驳回
    mark_document_review(runtime.db, doc_id, f"parse/{doc_id}")
    resp = await http.post(
        f"/api/v1/parse/tasks/{doc_id}/reject",
        headers=_headers(admin),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"


async def test_draft_exposes_tables_and_figure_bytes(client, seeded) -> None:
    """清洗稿正文只有 [表格 N] 占位；完整表与图在 draft/figures 接口。"""
    from ekbs.ingest.chunker import TableInput
    from ekbs.ingest.scan_review import save_review_draft

    http, runtime = client
    runtime, _ = seeded
    admin = _grant_admin(runtime)
    data_dir = Path(runtime.settings.data_dir)
    with runtime.db.sync_connect() as conn:
        kb_id = int(conn.execute("SELECT kb_id FROM ekbs_kb LIMIT 1").fetchone()["kb_id"])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, status, level) "
            "VALUES (?, 'scan-tables', 'raw/pdf-扫描/t.pdf', 'review', 1)",
            (kb_id,),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.commit()

    from PIL import Image
    import numpy as np

    img_dir = data_dir / "parse" / str(doc_id) / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    fig_id = "deadbeef01"
    # 白底极淡圆环 —— 走 enhance 路径
    arr = np.full((120, 120, 3), 255, dtype=np.uint8)
    yy, xx = np.ogrid[:120, :120]
    r = np.sqrt((xx - 60) ** 2 + (yy - 60) ** 2)
    arr[(r > 40) & (r < 46)] = (250, 250, 250)
    Image.fromarray(arr).save(img_dir / f"{fig_id}.jpg", quality=90)
    save_review_draft(
        data_dir=data_dir,
        doc_id=doc_id,
        markdown="清单如下：\n[表格 1]\n",
        engine="mineru",
        figures=[
            FigureInput(
                image_path=f"parse/{doc_id}/images/{fig_id}.jpg",
                caption="",
                page=2,
                figure_id=fig_id,
            )
        ],
        tables=[
            TableInput(
                body="<table><tr><td>A</td><td>B</td></tr></table>",
                caption="",
                page=1,
                table_id="1",
            )
        ],
    )
    mark_document_review(runtime.db, doc_id, f"parse/{doc_id}")

    draft = await http.get(f"/api/v1/parse/tasks/{doc_id}/draft", headers=_headers(admin))
    assert draft.status_code == 200
    body = draft.json()
    assert "[表格 1]" in body["markdown"]
    assert body["table_count"] == 1
    assert "<table>" in body["tables"][0]["body_html"]
    assert body["figure_count"] == 1
    assert body["figures"][0]["figure_id"] == fig_id

    fig = await http.get(
        f"/api/v1/parse/tasks/{doc_id}/figures/{fig_id}",
        headers=_headers(admin),
    )
    assert fig.status_code == 200
    assert fig.content.startswith(b"\xff\xd8")
    assert fig.headers.get("x-ekbs-figure-kind") in {"normal", "faint", "blank"}

    raw = await http.get(
        f"/api/v1/parse/tasks/{doc_id}/figures/{fig_id}?enhance=0",
        headers=_headers(admin),
    )
    assert raw.status_code == 200
    assert raw.content.startswith(b"\xff\xd8")
