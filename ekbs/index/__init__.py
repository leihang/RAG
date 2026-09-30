"""索引层：FAISS 分片（稠密）+ SQLite FTS5（稀疏）。"""

from ekbs.index.faiss_store import ShardCheck, ShardIndex, ShardIndexManager, normalize
from ekbs.index.fts import FtsHit, FtsIndex, to_match_query, tokenize, tokenize_for_fts
from ekbs.index.shard import (
    PUBLIC_DEPT_CODE,
    SHARD_ID_RE,
    ShardPlacement,
    dept_code_from_ref,
    make_shard_id,
    parse_shard_id,
    resolve_shard_for_document,
    validate_dept_code,
)

__all__ = [
    "PUBLIC_DEPT_CODE",
    "SHARD_ID_RE",
    "FtsHit",
    "FtsIndex",
    "ShardCheck",
    "ShardIndex",
    "ShardIndexManager",
    "ShardPlacement",
    "dept_code_from_ref",
    "make_shard_id",
    "normalize",
    "parse_shard_id",
    "resolve_shard_for_document",
    "to_match_query",
    "tokenize",
    "tokenize_for_fts",
    "validate_dept_code",
]
