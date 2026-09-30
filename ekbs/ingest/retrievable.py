"""「哪些块要进向量/进 FTS/被检索」的**单一权威**。

历史上这条规则被写死成 ``chunk_type = 'child'`` 散落在 8 个文件里，后果是：
每当分块器引入一种新的块类型，写入侧改了、重建/换级/巡检/统计侧没改，
就会出现「存了但搜不到」或「向量与库对不上」的静默故障。

规则只有一条
------------
**叶子块（没有任何块以它为父）才是可检索单元。**

* Markdown：``parent`` 是上下文容器，有 ``child`` 指向它 → 不入向量（避免同一段文字
  既是父又是子、被检索两次）；
* 代码：``parent`` 就是**符号本身**（顶层 class/def 没有子块）→ 必须入向量，
  否则整个代码知识库「存了但搜不到」；超长符号被切成 ``child`` 后，
  它的 ``parent`` 才退化成纯容器 → 由子块入向量；
* 图片：``figure`` 没有子块 → 入向量，图上的文字才检索得到；
* ``table``：有子块时是容器，无子块时是内容 —— 同一条规则自然覆盖。

调用方一律用 :data:`RETRIEVABLE_PREDICATE`（表别名必须是 ``c``），不要再手写
``chunk_type = 'child'``。
"""

from __future__ import annotations

#: 参与检索的块类型白名单。``summary`` 故意不在其中：文档摘要块是展示用的，
#: 让它进向量会在语义检索里与正文抢名额。
RETRIEVABLE_TYPES: tuple[str, ...] = ("parent", "child", "table", "figure")

_TYPES_SQL = ", ".join(f"'{t}'" for t in RETRIEVABLE_TYPES)

#: SQL 谓词，**要求** ``ekbs_chunk`` 的别名是 ``c``。
#: 用法：``f"SELECT c.chunk_id FROM ekbs_chunk c WHERE c.doc_id = ? AND {RETRIEVABLE_PREDICATE}"``
RETRIEVABLE_PREDICATE = (
    f"c.chunk_type IN ({_TYPES_SQL}) "
    "AND NOT EXISTS (SELECT 1 FROM ekbs_chunk _kid WHERE _kid.parent_id = c.chunk_id)"
)


def is_retrievable(chunk_type: str, *, has_children: bool) -> bool:
    """纯 Python 版判定，供不查库的调用方使用（与 SQL 版口径一致）。"""
    return chunk_type in RETRIEVABLE_TYPES and not has_children


def retrievable_chunk_ids(conn, *, doc_id: int | None = None, shard_id: str | None = None) -> list[int]:
    """按 ``seq`` 取可检索块 id；``doc_id`` / ``shard_id`` 至少给一个。"""
    clauses: list[str] = [RETRIEVABLE_PREDICATE]
    params: list[object] = []
    if doc_id is not None:
        clauses.append("c.doc_id = ?")
        params.append(doc_id)
    if shard_id is not None:
        clauses.append("c.shard_id = ?")
        params.append(shard_id)
    if doc_id is None and shard_id is None:
        raise ValueError("retrievable_chunk_ids 需要 doc_id 或 shard_id")
    sql = (
        "SELECT c.chunk_id FROM ekbs_chunk c WHERE "
        + " AND ".join(clauses)
        + " ORDER BY c.seq"
    )
    return [int(r["chunk_id"]) for r in conn.execute(sql, params).fetchall()]
