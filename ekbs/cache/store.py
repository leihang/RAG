"""CacheStore —— 替代 Redis 的缓存抽象（`01` §5.5、`03` §4.4）。

两层结构：

* **L1**：进程内 LRU（``retrieval.cache.l1_size``，默认 2000 条）——
  这也是 ``server.workers`` 必须为 1 的原因：多 worker 会让 L1 失效不同步，
  出现 ≤300s 的旧权限窗口（`04` §1.5 / §7.2）。
* **L2**：SQLite 表 ``ekbs_cache(cache_key PK, value TEXT, expires_at INTEGER)``。

键的纪律（**写缓存时最容易犯的错**）：

| 用途 | 键 | 是否含权限指纹 |
|---|---|---|
| 检索结果 | ``ekbs:cache:retrieve:<fingerprint>:<qhash>`` | **必须** |
| 答案 | ``ekbs:cache:answer:<fingerprint>:<qhash>`` | **必须** |
| 查询嵌入 | ``ekbs:cache:embed:<text_sha256>`` | 不需要（向量与权限无关，可全局共享） |

:meth:`CacheStore.retrieve_key` / :meth:`CacheStore.answer_key` 把指纹作为**必填参数**，
从签名上就让「忘记带指纹」写不出来。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from collections import OrderedDict
from typing import Any, Callable, Awaitable

from ekbs.config import Settings
from ekbs.db import Database
from ekbs.logging import get_logger

log = get_logger(component="cache")

#: 键前缀（`03` §4.4 的权威口径）
PREFIX_PERM_USER = "ekbs:perm:user:"
PREFIX_PERM_FP = "ekbs:perm:fp:"
PREFIX_RETRIEVE = "ekbs:cache:retrieve:"
PREFIX_ANSWER = "ekbs:cache:answer:"
PREFIX_EMBED = "ekbs:cache:embed:"
#: 多模态（图片旁路）的查询向量。**必须与文本嵌入分开**：两者维度不同
#: （1152 vs 1024），共用一个命名空间会把错误的向量喂给错误的索引。
PREFIX_MM_EMBED = "ekbs:cache:mm_embed:"
PREFIX_DOC_META = "ekbs:meta:doc:"
PREFIX_POLICY_VERSION = "ekbs:perm:policy_version"


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CacheStore:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self._l1: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._l1_size = max(16, int(settings.retrieval.cache.l1_size))
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # 键构造（指纹必填）
    # ------------------------------------------------------------------

    @staticmethod
    def perm_user_key(user_id: int) -> str:
        return f"{PREFIX_PERM_USER}{user_id}"

    @staticmethod
    def perm_fingerprint_key(fingerprint: str) -> str:
        return f"{PREFIX_PERM_FP}{fingerprint}"

    @staticmethod
    def retrieve_key(fingerprint: str, query: str) -> str:
        """检索结果键 —— **必须**含权限指纹（`00` C2）。"""
        if not fingerprint:
            raise ValueError("检索缓存键必须包含 perm_fingerprint（00 C2）")
        return f"{PREFIX_RETRIEVE}{fingerprint}:{text_sha256(query)}"

    @staticmethod
    def answer_key(fingerprint: str, question: str) -> str:
        """答案键 —— **必须**含权限指纹（`00` C2）。"""
        if not fingerprint:
            raise ValueError("答案缓存键必须包含 perm_fingerprint（00 C2）")
        return f"{PREFIX_ANSWER}{fingerprint}:{text_sha256(question)}"

    @staticmethod
    def embed_key(text: str) -> str:
        return f"{PREFIX_EMBED}{text_sha256(text)}"

    @staticmethod
    def mm_embed_key(text: str) -> str:
        """多模态查询向量的键（与 :meth:`embed_key` 分开命名空间，见 PREFIX_MM_EMBED）。"""
        return f"{PREFIX_MM_EMBED}{text_sha256(text)}"

    @staticmethod
    def doc_meta_key(doc_id: int) -> str:
        return f"{PREFIX_DOC_META}{doc_id}"

    # ------------------------------------------------------------------
    # L1
    # ------------------------------------------------------------------

    def l1_get(self, key: str) -> Any | None:
        item = self._l1.get(key)
        if item is None:
            return None
        value, expires_at = item
        if expires_at < time.time():
            self._l1.pop(key, None)
            return None
        self._l1.move_to_end(key)
        return value

    def l1_set(self, key: str, value: Any, ttl: int) -> None:
        # TTL 抖动 ±10%：防同一时刻批量过期造成击穿（03 §4.5）
        jitter = ttl * random.uniform(-0.1, 0.1)
        self._l1[key] = (value, time.time() + ttl + jitter)
        self._l1.move_to_end(key)
        while len(self._l1) > self._l1_size:
            self._l1.popitem(last=False)

    def invalidate(self, prefix: str) -> int:
        """按前缀清 L1（权限变更时由调度器/管理台调用）。返回清理条数。"""
        keys = [k for k in self._l1 if k.startswith(prefix)]
        for key in keys:
            self._l1.pop(key, None)
        return len(keys)

    # ------------------------------------------------------------------
    # L2（SQLite）
    # ------------------------------------------------------------------

    def _l2_get(self, key: str) -> Any | None:
        with self.db.sync_connect() as conn:
            row = conn.execute(
                "SELECT value, expires_at FROM ekbs_cache WHERE cache_key = ?", (key,)
            ).fetchone()
        if row is None or int(row["expires_at"]) < int(time.time()):
            return None
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return None

    def _l2_set(self, key: str, value: Any, ttl: int) -> None:
        expires_at = int(time.time()) + ttl
        payload = json.dumps(value, ensure_ascii=False)
        with self.db.sync_connect() as conn:
            conn.execute(
                "INSERT INTO ekbs_cache(cache_key, value, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT(cache_key) DO UPDATE SET value = excluded.value, "
                "expires_at = excluded.expires_at",
                (key, payload, expires_at),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # 异步门面
    # ------------------------------------------------------------------

    async def get(self, key: str) -> Any | None:
        cached = self.l1_get(key)
        if cached is not None:
            return cached
        value = await asyncio.to_thread(self._l2_get, key)
        if value is not None:
            self.l1_set(key, value, ttl=60)  # L2 命中后只在 L1 短驻，避免权限变更滞留
        return value

    async def set(self, key: str, value: Any, ttl: int) -> None:
        self.l1_set(key, value, ttl)
        await asyncio.to_thread(self._l2_set, key, value, ttl)

    async def get_or_set(
        self, key: str, ttl: int, factory: Callable[[], Awaitable[Any]]
    ) -> Any:
        """单飞（singleflight）：同一 key 并发只算一次（`03` §4.5）。"""
        cached = await self.get(key)
        if cached is not None:
            return cached
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = await self.get(key)
            if cached is not None:
                return cached
            value = await factory()
            if value is not None:
                await self.set(key, value, ttl)
            return value

    async def delete(self, key: str) -> None:
        self._l1.pop(key, None)

        def _delete() -> None:
            with self.db.sync_connect() as conn:
                conn.execute("DELETE FROM ekbs_cache WHERE cache_key = ?", (key,))
                conn.commit()

        await asyncio.to_thread(_delete)

    async def cleanup_expired(self) -> int:
        """清理过期行（由 ``com.ekbs.scheduler`` 定时调用）。"""

        def _cleanup() -> int:
            with self.db.sync_connect() as conn:
                cursor = conn.execute(
                    "DELETE FROM ekbs_cache WHERE expires_at < ?", (int(time.time()),)
                )
                conn.commit()
                return int(cursor.rowcount or 0)

        removed = await asyncio.to_thread(_cleanup)
        if removed:
            log.info("cache_cleanup", removed=removed)
        return removed
