"""输出契约：chunk 结构、schema 版本、**稳定 chunk_id**。

为什么需要稳定 chunk_id：生产入库要求「同一文件重复解析 → 同一批 id」，
这样才能做幂等 upsert（重入库不产生重复分块/向量）。
"""

from __future__ import annotations

import hashlib
from typing import Any

#: 输出契约版本；metadata 字段变更时递增，消费方据此兼容
SCHEMA_VERSION = "1.0"


def compute_chunk_id(source: str, sheet_name: str, row_range: str,
                     chunk_type: str, text: str) -> str:
    """确定性命中：同输入永远同 id。"""
    raw = "\x1f".join((source, sheet_name, row_range, chunk_type, text))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def make_chunk(
    *,
    chunk_text: str,
    source: str,
    sheet_name: str,
    chunk_type: str,
    row_range: str,
    retrieval: str,
    section_index: int = 0,
    columns: list[str] | None = None,
    table_title: str | None = None,
    record_count: int | None = None,
    slice_index: int | None = None,
    row_count: int | None = None,
    note_position: str | None = None,
    purpose: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造统一结构的 chunk：``{chunk_id, chunk_text, metadata}``。"""
    metadata: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source_file": source,
        "sheet_name": sheet_name,
        "chunk_type": chunk_type,
        "row_range": row_range,
        "retrieval": retrieval,
        "section_index": section_index,
    }
    if columns is not None:
        metadata["columns"] = list(columns)
    if table_title is not None:
        metadata["table_title"] = table_title
    if record_count is not None:
        metadata["record_count"] = record_count
    if slice_index is not None:
        metadata["slice_index"] = slice_index
    if row_count is not None:
        metadata["row_count"] = row_count
    if note_position is not None:
        metadata["note_position"] = note_position
    if purpose is not None:
        metadata["purpose"] = purpose
    if extra:
        metadata.update(extra)

    return {
        "chunk_id": compute_chunk_id(source, sheet_name, row_range, chunk_type, chunk_text),
        "chunk_text": chunk_text,
        "metadata": metadata,
    }
