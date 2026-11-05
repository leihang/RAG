"""入库时写 ``table_row_index``（表格行结构化索引）。

分片文本仍保留给检索/LLM；这里额外把“指标行 × 年份 × 月份”落成结构化行，
查询规划器（table_query）直接读它，不再从命中 chunk 文本临时拼。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from ekbs.logging import get_logger
from ekbs.retrieval.table_struct import parse_table_rows

log = get_logger(component="ingest.table_index")


def replace_document_table_rows(db, doc_id: int) -> int:
    """重建一篇文档的表格行索引；返回写入行数。"""
    try:
        with db.sync_connect() as conn:
            records = conn.execute(
                "SELECT c.chunk_id, c.doc_id, c.content, c.heading_path, d.title AS doc_title "
                "FROM ekbs_chunk c JOIN ekbs_document d ON d.doc_id = c.doc_id "
                "WHERE c.doc_id = ? AND c.chunk_type = 'table' ORDER BY c.seq",
                (doc_id,),
            ).fetchall()
            chunks = [
                SimpleNamespace(
                    chunk_id=int(row["chunk_id"]),
                    doc_id=int(row["doc_id"]),
                    content=str(row["content"] or ""),
                    heading_path=str(row["heading_path"] or ""),
                    doc_title=str(row["doc_title"] or ""),
                )
                for row in records
            ]
            parsed = parse_table_rows(chunks)
            conn.execute("DELETE FROM table_row_index WHERE doc_id = ?", (doc_id,))
            for row in parsed:
                conn.execute(
                    "INSERT INTO table_row_index("
                    "doc_id, chunk_id, caption, unit, subject, year, months_json, annual_total"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        doc_id,
                        row.chunk_id,
                        row.caption,
                        row.unit,
                        row.subject,
                        row.year,
                        json.dumps({str(k): v for k, v in sorted(row.months.items())}),
                        row.annual_total,
                    ),
                )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 —— 索引失败不能把已成功的分块入库打回 failed
        log.warning("table_row_index_failed", doc_id=doc_id, error=str(exc)[:240])
        return 0
    log.info("table_row_indexed", doc_id=doc_id, rows=len(parsed))
    return len(parsed)


def delete_document_table_rows(db, doc_id: int) -> None:
    try:
        with db.sync_connect() as conn:
            conn.execute("DELETE FROM table_row_index WHERE doc_id = ?", (doc_id,))
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("table_row_index_delete_failed", doc_id=doc_id, error=str(exc)[:240])
