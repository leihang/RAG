"""SQLite FTS5 全文检索（BM25）—— `01` §5.2、`03` §5.3、`docs/spec/10` §8。

三个易错点，都在本文件里处理：

1. **预分词**：写入前用 jieba 切好、空格连接存入 ``content_tok``，
   查询侧用同一分词口径；FTS5 的 ``tokenize='unicode61'`` 只做二次切分。
2. **权限必须 SQL 下推**：``shard_id IN (…)`` 是第一道，其次密级/状态/时效/部门。
   取回后再过滤等于把无权内容读进了进程（`03` §5.3）。
3. **同 chunk 去重**：doc2query 会让同一 ``chunk_id`` 有多行，必须
   ``GROUP BY chunk_id`` 取 ``MIN(bm25(...))``，否则 RRF 会把一行当多个候选（`06` §5.4）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from ekbs.db import Database
from ekbs.logging import get_logger

log = get_logger(component="fts")

#: **用户词典**（部署物，不是源码）：把语料里的编号/术语加成整词。
#: 实测动机：`XJWY-GC-01-01` 被切成 `['xjwy','-','gc','-','01','-','01']`（7 个碎片）、
#: `XJ-01-11-1F02` 同样 7 片 —— 碎片进倒排索引等于精确匹配失效。
#: 候选用 `ekbs.tools.build_userdict` 挖（含噪声过滤 + 人工评审），再由 `ekbs.tools.rebuild_fts`
#: 重建 `content_tok`（**只重建 FTS，不动向量**）。
_USERDICT_ENV = "EKBS_USERDICT_PATH"
_USERDICT_STATE: str | None = None


def userdict_path() -> Path | None:
    """用户词典路径：环境变量 → ``<repo>/var/dict/userdict.txt`` → ``~/.ekbs/userdict.txt``。"""
    explicit = os.environ.get(_USERDICT_ENV)
    candidates = (
        [Path(explicit)]
        if explicit
        else [
            Path(__file__).resolve().parents[2] / "var/dict/userdict.txt",
            Path.home() / ".ekbs" / "userdict.txt",
        ]
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


def ensure_userdict(*, force: bool = False) -> str:
    """惰性加载 jieba 用户词典，返回状态串（供 ``/health/deps.fts_userdict`` 与日志）。

    词典是**可选增强**：缺失或格式坏掉都**不能拖垮检索链路**（与 Qwen 词表同样的取舍），
    但状态必须能看见 —— 否则"以为加了词典、其实没生效"。
    """
    global _USERDICT_STATE
    if _USERDICT_STATE is not None and not force:
        return _USERDICT_STATE
    path = userdict_path()
    if path is None:
        _USERDICT_STATE = "none"
        return _USERDICT_STATE
    try:
        import jieba

        words = sum(
            1
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        jieba.load_userdict(str(path))
        _USERDICT_STATE = f"{path} ({words} 词)"
        log.info("fts_userdict_loaded", path=str(path), words=words)
    except Exception as exc:  # noqa: BLE001
        _USERDICT_STATE = f"error:{exc}"
        log.warning("fts_userdict_failed", path=str(path), error=str(exc))
    return _USERDICT_STATE


def reset_userdict_state() -> None:
    """清掉"已加载"标记（测试用；注意 jieba 全局词典本身无法卸载）。"""
    global _USERDICT_STATE
    _USERDICT_STATE = None


def tokenize(text: str) -> list[str]:
    """jieba 分词（写入与查询两侧共用，口径必须一致）。"""
    import jieba

    ensure_userdict()
    return [token.strip().lower() for token in jieba.lcut(text or "") if token.strip()]


def tokenize_for_fts(text: str) -> str:
    """写入 FTS5 的 ``content_tok``：空格连接的 jieba 词串。"""
    return " ".join(tokenize(text))


def to_match_query(text: str) -> str:
    """构造 FTS5 MATCH 查询串：每个词加双引号（防语法注入），词间 OR。"""
    tokens = tokenize(text)
    if not tokens:
        return ""
    return " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)


@dataclass(frozen=True)
class FtsHit:
    chunk_id: int
    shard_id: str
    score: float  # bm25()：**越小越相关**


class FtsIndex:
    """``ekbs_chunk_fts`` 的读写封装。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    def upsert_chunks(self, rows: Sequence[tuple[int, str, str, str | None]]) -> int:
        """写入 ``[(chunk_id, shard_id, content, heading_path), …]``。

        与 ``ekbs_chunk`` 写入放同一事务由调用方负责（`01` §5.2 的一致性要求）。
        """
        if not rows:
            return 0
        payload = [
            (tokenize_for_fts(content), tokenize_for_fts(heading or ""), str(chunk_id), shard_id)
            for chunk_id, shard_id, content, heading in rows
        ]
        with self.db.sync_connect() as conn:
            conn.executemany(
                "INSERT INTO ekbs_chunk_fts(content_tok, heading_path_tok, chunk_id, shard_id) "
                "VALUES (?, ?, ?, ?)",
                payload,
            )
            conn.commit()
        return len(payload)

    def upsert_retrievable_with_doc2query(
        self,
        rows: Sequence[tuple[int, str, str, str | None, str]],
        *,
        enabled: bool = True,
        max_questions: int = 4,
    ) -> int:
        """写原文 FTS 行，并按需追加 Doc2Query 假设问句行。

        ``rows``: ``(chunk_id, shard_id, content, heading_path, chunk_type)``
        """
        base = [
            (int(chunk_id), shard_id, content, heading)
            for chunk_id, shard_id, content, heading, _ctype in rows
        ]
        written = self.upsert_chunks(base)
        if not enabled or not rows:
            return written
        from ekbs.ingest.doc2query import fts_doc2query_rows

        extra = fts_doc2query_rows(rows, max_questions=max_questions)
        if extra:
            written += self.upsert_chunks(extra)
        return written

    def delete_chunk_ids(self, chunk_ids: Iterable[int]) -> int:
        ids = [str(int(c)) for c in chunk_ids]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self.db.sync_connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM ekbs_chunk_fts WHERE chunk_id IN ({placeholders})", tuple(ids)
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    def count(self) -> int:
        with self.db.sync_connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM ekbs_chunk_fts").fetchone()[0])

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        shard_ids: Sequence[str],
        clearance: int,
        dept_chain: Sequence[str],
        top_k: int,
    ) -> list[FtsHit]:
        """BM25 召回；权限条件全部下推到 SQL（`03` §5.3）。"""
        match_query = to_match_query(query)
        if not match_query or not shard_ids:
            return []
        import json

        sql = """
            SELECT c.chunk_id           AS chunk_id,
                   c.shard_id           AS shard_id,
                   bm25(ekbs_chunk_fts) AS score
              FROM ekbs_chunk_fts
              JOIN ekbs_chunk    c ON c.chunk_id = CAST(ekbs_chunk_fts.chunk_id AS INTEGER)
              JOIN ekbs_document d ON d.doc_id   = c.doc_id
             WHERE ekbs_chunk_fts MATCH :match_query
               AND c.shard_id IN (SELECT value FROM json_each(:shard_ids))
               AND d.level <= :clearance
               AND d.status = 'published'
               AND (d.effective_date IS NULL OR d.effective_date <= date('now'))
               AND (d.expire_date    IS NULL OR d.expire_date    >  date('now'))
               AND (json_array_length(d.visible_dept_ids) = 0
                    OR EXISTS (SELECT 1 FROM json_each(d.visible_dept_ids) v
                                WHERE v.value IN (SELECT value FROM json_each(:dept_chain))))
             ORDER BY score ASC
             LIMIT :fetch_limit
        """
        params = {
            "match_query": match_query,
            "shard_ids": json.dumps(list(shard_ids), ensure_ascii=False),
            "clearance": int(clearance),
            "dept_chain": json.dumps([str(d) for d in dept_chain], ensure_ascii=False),
            # 多取几倍再在 Python 里去重：同一 chunk 可能有多行（doc2query 生成的问题），
            # 去重会减少有效候选数，多取可避免召回被挤掉。
            "fetch_limit": max(int(top_k) * 4, int(top_k) + 20),
        }
        with self.db.sync_connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        # 按 chunk_id 去重，保留 bm25 最小（最相关）的那行，并保持整体有序。
        # ⚠️ 这里**不能**用 SQL 的 `GROUP BY chunk_id ... MIN(bm25(...))`：SQLite 的 FTS5
        #    辅助函数在**任何聚合上下文**下都会报
        #    "unable to use function bm25 in the requested context"
        #    （窗口函数 ROW_NUMBER() 同样不行，已实测）。`docs/spec/10` §8 给的是聚合写法，
        #    落地时按本实现改写；语义（同 chunk 只算一个候选、取最相关分数）保持一致。
        best: dict[int, FtsHit] = {}
        order: list[int] = []
        for row in rows:
            chunk_id = int(row["chunk_id"])
            if chunk_id in best:
                continue
            best[chunk_id] = FtsHit(
                chunk_id=chunk_id, shard_id=str(row["shard_id"]), score=float(row["score"])
            )
            order.append(chunk_id)
            if len(order) >= int(top_k):
                break
        return [best[chunk_id] for chunk_id in order]
