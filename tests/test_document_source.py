"""文档原件回源 ``GET /docs/documents/{id}/source``（复核左栏）。"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from ekbs.api.app import create_app
from tests.conftest import TEST_INTERNAL_TOKEN, user_id_of
from tests.test_admin_api import grant_role, headers_for


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


def _plant_source(runtime, doc_id: int, *, name: str = "合同.pdf", body: bytes = b"%PDF-1.4 mock") -> str:
    raw = runtime.settings.data_paths()["raw"]
    path = raw / "复核原件" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_document SET source_path = ? WHERE doc_id = ?",
            (str(path), doc_id),
        )
        conn.commit()
    return str(path)


async def test_document_source_ok(client, seeded) -> None:
    runtime, _ = seeded
    admin_id = grant_role(runtime, "demo_alice", "kb_admin")
    headers = headers_for(admin_id)
    listing = (await client.get("/api/v1/docs/documents", headers=headers)).json()
    doc_id = listing["items"][0]["doc_id"]
    _plant_source(runtime, doc_id, body=b"%PDF-1.4 hello")

    resp = await client.get(f"/api/v1/docs/documents/{doc_id}/source", headers=headers)
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF-1.4")
    ctype = resp.headers.get("content-type", "")
    assert "pdf" in ctype.lower() or "octet-stream" in ctype.lower()


async def test_document_source_denied_higher_clearance(client, seeded) -> None:
    """低密级即使用户有 document:read，也看不到更高密级原件（404）。"""
    runtime, _ = seeded
    admin_id = grant_role(runtime, "demo_carol", "kb_admin")
    headers = headers_for(admin_id)
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT doc_id, level FROM ekbs_document WHERE level >= 2 ORDER BY level DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    doc_id = int(row["doc_id"])
    _plant_source(runtime, doc_id)

    resp = await client.get(f"/api/v1/docs/documents/{doc_id}/source", headers=headers)
    assert resp.status_code == 404


async def test_document_source_path_escape_404(client, seeded, tmp_path) -> None:
    runtime, _ = seeded
    admin_id = grant_role(runtime, "demo_alice", "kb_admin")
    headers = headers_for(admin_id)
    listing = (await client.get("/api/v1/docs/documents", headers=headers)).json()
    doc_id = listing["items"][0]["doc_id"]
    outside = tmp_path / "escape.pdf"
    outside.write_bytes(b"%PDF-escape")
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_document SET source_path = ? WHERE doc_id = ?",
            (str(outside), doc_id),
        )
        conn.commit()

    resp = await client.get(f"/api/v1/docs/documents/{doc_id}/source", headers=headers)
    assert resp.status_code == 404
    assert outside.is_file()


async def test_document_source_missing_file_404(client, seeded) -> None:
    runtime, _ = seeded
    admin_id = grant_role(runtime, "demo_alice", "kb_admin")
    headers = headers_for(admin_id)
    listing = (await client.get("/api/v1/docs/documents", headers=headers)).json()
    doc_id = listing["items"][0]["doc_id"]
    raw = runtime.settings.data_paths()["raw"]
    ghost = raw / "不存在" / "gone.pdf"
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_document SET source_path = ? WHERE doc_id = ?",
            (str(ghost), doc_id),
        )
        conn.commit()

    resp = await client.get(f"/api/v1/docs/documents/{doc_id}/source", headers=headers)
    assert resp.status_code == 404


async def test_document_source_requires_auth(client) -> None:
    resp = await client.get("/api/v1/docs/documents/1/source")
    assert resp.status_code == 401


async def test_document_source_no_perm_403(client, seeded) -> None:
    runtime, _ = seeded
    user_id = user_id_of(runtime.db, "demo_alice")
    resp = await client.get(
        "/api/v1/docs/documents/1/source",
        headers=headers_for(user_id),
    )
    assert resp.status_code == 403
    assert TEST_INTERNAL_TOKEN  # 模块级常量被引用，避免误删 import


def _docx_bytes(text: str) -> bytes:
    import docx
    from io import BytesIO

    document = docx.Document()
    document.add_paragraph(text)
    buf = BytesIO()
    document.save(buf)
    return buf.getvalue()


async def test_source_preview_renders_docx(client, seeded) -> None:
    runtime, _ = seeded
    admin_id = grant_role(runtime, "demo_alice", "kb_admin")
    headers = headers_for(admin_id)
    listing = (await client.get("/api/v1/docs/documents", headers=headers)).json()
    doc_id = listing["items"][0]["doc_id"]
    _plant_source(runtime, doc_id, name="作业指导.docx", body=_docx_bytes("设备编号由四部分组成"))

    resp = await client.get(f"/api/v1/docs/documents/{doc_id}/source-preview", headers=headers)
    assert resp.status_code == 200
    assert "html" in resp.headers.get("content-type", "").lower()
    assert "设备编号由四部分组成" in resp.text
    assert "<script" not in resp.text.lower()


async def test_source_preview_rejects_pdf(client, seeded) -> None:
    runtime, _ = seeded
    admin_id = grant_role(runtime, "demo_alice", "kb_admin")
    headers = headers_for(admin_id)
    listing = (await client.get("/api/v1/docs/documents", headers=headers)).json()
    doc_id = listing["items"][0]["doc_id"]
    _plant_source(runtime, doc_id, name="合同.pdf", body=b"%PDF-1.4")

    resp = await client.get(f"/api/v1/docs/documents/{doc_id}/source-preview", headers=headers)
    assert resp.status_code == 415
