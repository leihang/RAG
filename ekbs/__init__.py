"""EKBS —— 企业知识中枢（部门级 RAG 试点）。

工程结构与文档的对应关系（`方案/` 为唯一权威）：

| 包 | 职责 | 权威来源 |
|---|---|---|
| ``ekbs.config`` | 配置加载与密钥纪律反向检查 | `01` §7、`04` §2.6 |
| ``ekbs.db`` | SQLite 连接、PRAGMA、Schema 应用与自检 | `01` §5.0/§5.7 |
| ``ekbs.provider`` | 模型 provider 抽象 + 嵌入指纹 + mock | `00` C3、§3.1 |
| ``ekbs.permission`` | 分片路由、权限指纹、C5 文档闸门 | `00` §3.2/§3.4、C5、`03` |
| ``ekbs.index`` | FAISS 分片索引 + FTS5 | `01` §6、`06` §2/§4 |
| ``ekbs.retrieval`` | RRF → rerank → C5 → 父块扩展 → 引用 | `00` §2.1/§2.2 |
| ``ekbs.cache`` | L1 LRU + L2 ``ekbs_cache``（替代 Redis） | `01` §5.5 |
| ``ekbs.api`` | FastAPI 应用与健康端点 | `04` §7.3、`05` |
"""

__version__ = "0.1.0"
