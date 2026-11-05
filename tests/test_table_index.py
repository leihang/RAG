"""入库表级索引：写入 table_row_index 后查询不再依赖 chunk 文本重建。"""

from __future__ import annotations

from types import SimpleNamespace

from ekbs.ingest.table_index import replace_document_table_rows
from ekbs.retrieval.table_query import compute_table_comparison
from ekbs.retrieval.table_struct import load_table_rows

CONTENT = """表名: 年用水费统计对比（元）
相关表头: 日期 | 年       月份 | 1月 | 2月 | 3月 | 4月 | 5月 | 6月 | 7月 | 8月 | 9月 | 10月 | 11月 | 12月 | 年度合计
日期=公司承担水费; 年       月份=2025; 1月=8520.86; 2月=24444.10; 3月=12278.35; 4月=36500.71; 5月=14332.74; 6月=35569.18; 7月=26610.49; 8月=37169.85; 9月=16086.45; 10月=42512.16; 11月=16740.39; 12月=59856.67; 年度合计=330621.95
日期=公司承担水费; 年       月份=2026; 1月=52679.50; 2月=80913.80; 3月=76906.30; 4月=9227.15; 5月=8241.60; 6月=17404.15; 7月=23606.97; 8月=8518.63; 9月=286.25; 10月=0.00; 11月=0.00; 12月=0.00; 年度合计=277784.35
"""


def _make_doc_chunk(db) -> tuple[int, int]:
    with db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_kb(code, name) VALUES ('test_kb', '测试库')"
        )
        kb_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, visible_dept_ids, status) "
            "VALUES (?, '能耗分析', '/tmp/能耗分析.xlsx', 1, '[]', 'published')",
            (kb_id,),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.execute(
            "INSERT INTO ekbs_chunk(doc_id, chunk_type, seq, content, token_count, heading_path, shard_id) "
            "VALUES (?, 'table', 1, ?, 10, '用水分析', 'kb_test_L1')",
            (doc_id, CONTENT),
        )
        chunk_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        conn.commit()
    return doc_id, chunk_id


def test_ingest_index_is_read_by_query(db) -> None:
    doc_id, chunk_id = _make_doc_chunk(db)
    assert replace_document_table_rows(db, doc_id) == 2
    rows = load_table_rows(db, [doc_id])
    assert len(rows) == 2
    assert rows[0].subject == "公司承担水费"
    assert rows[0].chunk_id is not None

    chunk = SimpleNamespace(
        chunk_id=chunk_id,
        doc_id=doc_id,
        chunk_type="table",
        content=CONTENT,
        heading_path="用水分析",
        doc_title="能耗分析",
    )
    result = compute_table_comparison(
        "公司承担水费，2026年1-9月较2025年1-9月是增长还是减少",
        [chunk],
        db=db,
    )
    assert result is not None
    assert result.totals[2026] == 277784.35
    assert "增长" in result.render()


def test_reindex_is_idempotent(db) -> None:
    doc_id, _chunk_id = _make_doc_chunk(db)
    replace_document_table_rows(db, doc_id)
    replace_document_table_rows(db, doc_id)
    with db.sync_connect() as conn:
        count = conn.execute(
            "SELECT count(*) AS c FROM table_row_index WHERE doc_id = ?", (doc_id,)
        ).fetchone()["c"]
    assert count == 2
