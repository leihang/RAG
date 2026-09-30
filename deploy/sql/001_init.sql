-- =============================================================================
-- EKBS SQLite 初始化脚本 —— 全目录唯一权威 Schema 的可执行落地
-- -----------------------------------------------------------------------------
-- 权威来源：`01-实施步骤与本地部署.md` §5（§5.0 设计约定 + §5.1 ~ §5.5 DDL）
--           本文件的每条 DDL 均逐字取自该节，未做任何字段级改动。
--           查看设计意图请回到 `01` §5；本文件只负责可执行形态。
--
-- 【用途】在空库上一次性建立 EKBS 的全部表、索引与 FTS5 虚拟表。
--         不包含业务种子数据（除 §5.3 的 ekbs_policy_version 单行初始化，
--         该 INSERT 是 `01` §5.3 原文的一部分）。
--
-- 【执行方式】
--         mkdir -p /data/ekbs/index
--         sqlite3 /data/ekbs/index/ekbs.db < /opt/ekbs/app/sql/001_init.sql
--         # 幂等：全部对象使用 IF NOT EXISTS，可重复执行
--
-- 【幂等说明】
--         CREATE TABLE / INDEX / VIRTUAL TABLE 均为 IF NOT EXISTS；
--         `INSERT OR IGNORE INTO ekbs_policy_version` 亦为幂等写入。
--         重复执行不会报错，也不会覆盖已有数据。
--         注意：IF NOT EXISTS 不会「升级」已存在的旧表结构 —— 已上线的库若要改
--         字段，必须新增迁移脚本，不要改本文件后重跑。
--
-- 【SQLite 版本要求】≥ 3.35
--         本 DDL 用到：部分索引（partial index，3.8+）、FTS5（3.9+）、
--         `datetime('now')` 默认值、`CHECK` 约束；
--         应用层写入路径用到 `ON CONFLICT ... DO UPDATE`（3.24+）与 `RETURNING`（3.35+），
--         故整条链路的最低版本按 3.35 计。
--         另需 FTS5 与 JSON1 扩展（macOS 自带 sqlite3 与 Homebrew 的 Python 均已编译进来）。
--         Python 侧确认命令见 `01` §5.6 / §3。
--
-- 【连接参数】不在本文件内设置
--         `PRAGMA journal_mode=WAL; synchronous=NORMAL; foreign_keys=ON; busy_timeout=5000`
--         属于 `01` §5.0 的「连接参数」约定，由 shell 脚本（deploy/scripts/install.sh 等）
--         与 `ekbs/db.py` 在**每次建连时**显式设置 —— 因为 `foreign_keys` 是连接级开关，
--         `journal_mode` 是持久化到库文件的开关，两者都不适合写进一次性初始化脚本。
--
-- 【表清单】共 29 张（不含 sqlite_* 内部表；ekbs_chunk_fts 为 FTS5 虚拟表，计入）
--         §5.1 组织与身份（5）
--           1  ekbs_dept             部门
--           2  ekbs_user             用户
--           3  ekbs_user_dept        用户-部门（多对多）
--           4  ekbs_dept_closure     部门闭包（含自身 depth=0）
--           5  ekbs_sync_log         同步日志与对账
--         §5.2 知识库、文档与分片（5）
--           6  ekbs_kb               知识库
--           7  ekbs_document         文档
--           8  ekbs_chunk            分片（父块 / 子块）
--           9  ekbs_shard            FAISS 分片元数据
--          10  ekbs_chunk_fts        FTS5 全文索引（虚拟表）
--         §5.3 权限 RBAC + ACL + 密级（8）
--          11  ekbs_role             角色
--          12  ekbs_permission       权限点
--          13  ekbs_role_permission  角色-权限点
--          14  ekbs_user_role        用户-角色（含 scope）
--          15  ekbs_user_group       用户组
--          16  ekbs_group_member     组-成员
--          17  ekbs_acl_entry        ACL 条目（deny 优先）
--          18  ekbs_policy_version   全局策略版本（单行，含种子 INSERT）
--         §5.4 审计（6，留存要求见 `00` §3.5）
--          19  ekbs_qa_session       问答会话
--          20  ekbs_qa_message       问答消息与耗时拆解
--          21  ekbs_qa_citation      引用（只存 hash，不存片段全文）
--          22  ekbs_doc_access_log   文档访问日志
--          23  ekbs_perm_change_log  权限与密级变更日志
--          24  ekbs_security_event   安全事件
--         §5.5 缓存与运维（5，替代 Redis）
--          25  ekbs_cache            通用缓存（键必须含 perm_fingerprint）
--          26  ekbs_msg_dedup        钉钉消息幂等
--          27  ekbs_rate_limit       限流计数
--          28  ekbs_session_ctx      钉钉多轮会话上下文（键含用户维度）
--          29  ekbs_metric           运行指标（每 60s 写入）
--
-- 【自检】本文件末尾的 `PRAGMA user_version = 1;` 供应用层判断是否已初始化。
--         正确表数统计（排除 FTS5 的 5 张影子表，见下方说明）：
--           sqlite3 ekbs.db "SELECT COUNT(*) FROM sqlite_master m
--             WHERE m.type IN ('table','view') AND m.name LIKE 'ekbs%'
--               AND m.name NOT IN ('ekbs_chunk_fts_data','ekbs_chunk_fts_idx',
--                                  'ekbs_chunk_fts_content','ekbs_chunk_fts_docsize',
--                                  'ekbs_chunk_fts_config');"   -- 期望 29
--         ⚠️ `01` §6 给出的计数语句未排除 FTS5 影子表，实测返回 34（29 + 5 张影子表），
--            因此 deploy/scripts/smoke.sh 使用上面的完整排除写法。详见交付说明的表数勘误。
-- =============================================================================

-- =============================================================================
-- §5.1 组织与身份（钉钉同步）
-- =============================================================================

-- ---------- 部门 ----------
CREATE TABLE IF NOT EXISTS ekbs_dept (
    dept_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    dingtalk_dept_id        TEXT    NOT NULL UNIQUE,
    name                    TEXT    NOT NULL,
    parent_dingtalk_dept_id TEXT,                                  -- NULL 表示根
    parent_dept_id          INTEGER REFERENCES ekbs_dept(dept_id) ON DELETE SET NULL,
    dept_path               TEXT    NOT NULL DEFAULT '/',           -- 物化路径 '/1/102/10203/'
    depth                   INTEGER NOT NULL DEFAULT 0,             -- 根为 0，逐层 +1
    order_no                INTEGER NOT NULL DEFAULT 0,
    is_root                 INTEGER NOT NULL DEFAULT 0,
    status                  TEXT    NOT NULL DEFAULT 'active',      -- active | deleted
    dingtalk_updated_at     TEXT,
    synced_at               TEXT    NOT NULL DEFAULT (datetime('now')),
    deleted_at              TEXT
);
CREATE INDEX IF NOT EXISTS idx_dept_parent ON ekbs_dept(parent_dept_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_dept_path   ON ekbs_dept(dept_path);   -- 支持 LIKE '/1/102/%' 前缀查

-- ---------- 用户 ----------
CREATE TABLE IF NOT EXISTS ekbs_user (
    user_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    dingtalk_userid   TEXT    NOT NULL UNIQUE,
    unionid           TEXT,
    nick              TEXT    NOT NULL,
    job_number        TEXT,
    title             TEXT,
    mobile_masked     TEXT,                                        -- 脱敏存储，如 138****8888
    email             TEXT,
    avatar_url        TEXT,
    clearance         INTEGER NOT NULL DEFAULT 2 CHECK (clearance BETWEEN 1 AND 4),
    -- 部门链缓存：本部门 + 全部祖先部门的 dingtalk_dept_id（JSON 数组），由同步任务刷新
    dept_chain        TEXT    NOT NULL DEFAULT '[]',
    primary_dept_id   INTEGER REFERENCES ekbs_dept(dept_id) ON DELETE SET NULL,
    status            TEXT    NOT NULL DEFAULT 'active',           -- active | left | disabled
    hired_at          TEXT,
    left_at           TEXT,
    auth_source       TEXT    NOT NULL DEFAULT 'dingtalk',
    perm_version      INTEGER NOT NULL DEFAULT 1,                  -- 用户级版本，仅用于失效该用户缓存
    synced_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    deleted_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_status ON ekbs_user(status) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_user_unionid ON ekbs_user(unionid);

-- ---------- 用户-部门（多对多）----------
CREATE TABLE IF NOT EXISTS ekbs_user_dept (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES ekbs_user(user_id) ON DELETE CASCADE,
    dept_id     INTEGER NOT NULL REFERENCES ekbs_dept(dept_id) ON DELETE CASCADE,
    is_primary  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (user_id, dept_id)
);

-- ---------- 部门闭包（含自身 depth=0）----------
CREATE TABLE IF NOT EXISTS ekbs_dept_closure (
    dept_id     INTEGER NOT NULL,
    ancestor_id INTEGER NOT NULL,
    depth       INTEGER NOT NULL,
    PRIMARY KEY (dept_id, ancestor_id)
);

-- ---------- 同步日志与对账 ----------
CREATE TABLE IF NOT EXISTS ekbs_sync_log (
    log_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT    NOT NULL,                 -- dept | user | event
    mode        TEXT    NOT NULL,                 -- full | delta | reconcile
    started_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    ended_at    TEXT,
    created     INTEGER NOT NULL DEFAULT 0,
    updated     INTEGER NOT NULL DEFAULT 0,
    deleted     INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    detail      TEXT                              -- JSON 摘要
);
CREATE INDEX IF NOT EXISTS idx_sync_log_time ON ekbs_sync_log(started_at DESC);

-- =============================================================================
-- §5.2 知识库、文档与分片
-- =============================================================================

CREATE TABLE IF NOT EXISTS ekbs_kb (
    kb_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT    NOT NULL UNIQUE,
    name            TEXT    NOT NULL,
    description     TEXT,
    default_level   INTEGER NOT NULL DEFAULT 2 CHECK (default_level BETWEEN 1 AND 4),
    retrieval_config TEXT   NOT NULL DEFAULT '{}',      -- JSON
    status          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ekbs_document (
    doc_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kb_id           INTEGER NOT NULL REFERENCES ekbs_kb(kb_id),
    title           TEXT    NOT NULL,
    source_path     TEXT    NOT NULL,                    -- /data/ekbs/raw/...
    parse_path      TEXT,                                -- /data/ekbs/parse/<sha>/...
    mime            TEXT,
    size_bytes      INTEGER,
    page_count      INTEGER,
    doc_version     TEXT    NOT NULL DEFAULT 'v1',
    level           INTEGER NOT NULL DEFAULT 2 CHECK (level BETWEEN 1 AND 4),
    visible_dept_ids TEXT   NOT NULL DEFAULT '[]',       -- JSON 数组，空表示不限部门
    effective_date  TEXT,
    expire_date     TEXT,
    status          TEXT    NOT NULL DEFAULT 'pending',  -- pending|parsing|indexing|published|failed|offline
    checksum        TEXT,                                -- SHA-256，用于秒传与去重
    created_by      INTEGER,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_doc_kb_status ON ekbs_document(kb_id, status);
CREATE INDEX IF NOT EXISTS idx_doc_level     ON ekbs_document(level, status);
CREATE INDEX IF NOT EXISTS idx_doc_effective ON ekbs_document(effective_date, expire_date);
CREATE INDEX IF NOT EXISTS idx_doc_checksum  ON ekbs_document(checksum);

CREATE TABLE IF NOT EXISTS ekbs_chunk (
    chunk_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id          INTEGER NOT NULL REFERENCES ekbs_document(doc_id) ON DELETE CASCADE,
    parent_id       INTEGER,
    chunk_type      TEXT    NOT NULL DEFAULT 'child',    -- parent|child|table|figure|summary
    seq             INTEGER NOT NULL,
    content         TEXT    NOT NULL,
    token_count     INTEGER,
    heading_path    TEXT,
    page_from       INTEGER,
    page_to         INTEGER,
    -- 图表回源（chunk_type='figure'/'table' 时非空）：见 06 §4.11
    --   figure_id  图表编号（如「图 1」「表 2」），用于在引用里显示
    --   image_path 图片文件相对 data_dir 的路径，如 parse/<sha>/images/fig1.png；
    --              **不存绝对路径**，避免换机器/换目录时全部失效
    figure_id       TEXT,
    image_path      TEXT,
    shard_id        TEXT    NOT NULL,                    -- 所属 FAISS 分片，如 kb_hr_L2
    embedding_fingerprint TEXT,                          -- 生成该向量的模型指纹
    faiss_row       INTEGER,                             -- 在分片中的行号
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chunk_doc     ON ekbs_chunk(doc_id, seq);
CREATE INDEX IF NOT EXISTS idx_chunk_shard   ON ekbs_chunk(shard_id);
CREATE INDEX IF NOT EXISTS idx_chunk_parent  ON ekbs_chunk(parent_id);
-- 图片资源端点按 shard_id 做 ACL 复检，再按 doc_id+figure_id 取路径（06 §4.11）
CREATE INDEX IF NOT EXISTS idx_chunk_figure  ON ekbs_chunk(doc_id, figure_id) WHERE figure_id IS NOT NULL;

-- 分片元数据（FAISS 索引文件的登记与自检依据）
CREATE TABLE IF NOT EXISTS ekbs_shard (
    shard_id                TEXT PRIMARY KEY,            -- kb_hr_L2
    dept_code               TEXT NOT NULL,               -- all | hr | finance ...（与 shard_id 中段一致）
    level                   INTEGER NOT NULL CHECK (level BETWEEN 1 AND 4),
    faiss_path              TEXT NOT NULL,
    dim                     INTEGER NOT NULL,
    embedding_fingerprint   TEXT NOT NULL,               -- 该分片全部向量的模型指纹
    vector_count            INTEGER NOT NULL DEFAULT 0,
    index_type              TEXT NOT NULL DEFAULT 'flat',-- flat | ivf
    status                  TEXT NOT NULL DEFAULT 'active', -- active|building|retired
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

-- FTS5 全文索引（content 由应用层用 jieba 预分词后写入 content_tok）
-- 路由查询的主索引：WHERE status='active' AND level<=:clearance AND dept_code IN (...)
CREATE INDEX IF NOT EXISTS idx_shard_route ON ekbs_shard(status, level, dept_code);

CREATE VIRTUAL TABLE IF NOT EXISTS ekbs_chunk_fts USING fts5(
    content_tok      ,
    heading_path_tok ,
    chunk_id         UNINDEXED,
    shard_id         UNINDEXED,
    tokenize = 'unicode61'
);

-- =============================================================================
-- §5.3 权限（RBAC + ACL + 密级）
-- =============================================================================

CREATE TABLE IF NOT EXISTS ekbs_role (
    role_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    NOT NULL UNIQUE,
    name        TEXT    NOT NULL,
    description TEXT,
    is_builtin  INTEGER NOT NULL DEFAULT 0,
    status      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ekbs_permission (
    perm_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    NOT NULL UNIQUE,        -- kb:knowledge_base:create
    name        TEXT    NOT NULL,
    module      TEXT    NOT NULL,
    description TEXT
);

CREATE TABLE IF NOT EXISTS ekbs_role_permission (
    role_id INTEGER NOT NULL REFERENCES ekbs_role(role_id) ON DELETE CASCADE,
    perm_id INTEGER NOT NULL REFERENCES ekbs_permission(perm_id) ON DELETE CASCADE,
    PRIMARY KEY (role_id, perm_id)
);

CREATE TABLE IF NOT EXISTS ekbs_user_role (
    user_id    INTEGER NOT NULL REFERENCES ekbs_user(user_id) ON DELETE CASCADE,
    role_id    INTEGER NOT NULL REFERENCES ekbs_role(role_id) ON DELETE CASCADE,
    scope_type TEXT    NOT NULL DEFAULT 'global',   -- global | kb
    scope_id   INTEGER NOT NULL DEFAULT 0,          -- scope_type='kb' 时为 kb_id
    granted_by INTEGER,
    granted_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, role_id, scope_type, scope_id)
);

CREATE TABLE IF NOT EXISTS ekbs_user_group (
    group_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    NOT NULL UNIQUE,
    name        TEXT    NOT NULL,
    description TEXT,
    created_by  INTEGER,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ekbs_group_member (
    group_id INTEGER NOT NULL REFERENCES ekbs_user_group(group_id) ON DELETE CASCADE,
    user_id  INTEGER NOT NULL REFERENCES ekbs_user(user_id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, user_id)
);

-- ACL：主体 × 对象 × 权限；effect='deny' 优先
CREATE TABLE IF NOT EXISTS ekbs_acl_entry (
    acl_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    object_type  TEXT    NOT NULL,                -- kb | folder | document | shard
    object_id    TEXT    NOT NULL,                -- 分片时为 shard_id
    subject_type TEXT    NOT NULL,                -- user | dept | role | group
    subject_id   TEXT    NOT NULL,
    permission   TEXT    NOT NULL,                -- read | write | manage | audit
    effect       TEXT    NOT NULL DEFAULT 'allow' CHECK (effect IN ('allow','deny')),
    granted_by   INTEGER,
    expire_at    TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (object_type, object_id, subject_type, subject_id, permission)
);
CREATE INDEX IF NOT EXISTS idx_acl_lookup ON ekbs_acl_entry(object_type, object_id, permission);

-- 全局策略版本（单行）；任一权限变更即 +1，使所有指纹缓存自然失效
CREATE TABLE IF NOT EXISTS ekbs_policy_version (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    version    INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO ekbs_policy_version(id, version) VALUES (1, 1);

-- =============================================================================
-- §5.4 审计（留存要求见 00 §3.5）
-- =============================================================================

CREATE TABLE IF NOT EXISTS ekbs_qa_session (
    session_id       TEXT    PRIMARY KEY,          -- 应用层生成 UUID
    user_id          INTEGER NOT NULL,
    dt_userid        TEXT,
    channel          TEXT    NOT NULL,             -- dingtalk_robot | dingtalk_h5 | web | api
    conversation_id  TEXT,
    perm_fingerprint TEXT    NOT NULL,
    clearance_at     INTEGER NOT NULL,
    started_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    ended_at         TEXT,
    message_count    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_qa_session_user ON ekbs_qa_session(user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_qa_session_time ON ekbs_qa_session(started_at DESC);

CREATE TABLE IF NOT EXISTS ekbs_qa_message (
    message_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        TEXT    NOT NULL REFERENCES ekbs_qa_session(session_id) ON DELETE CASCADE,
    turn              INTEGER NOT NULL,
    role              TEXT    NOT NULL,            -- user | assistant
    question          TEXT,
    rewritten_query   TEXT,
    answer            TEXT,
    model             TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    ttft_ms           INTEGER,
    total_ms          INTEGER,
    retrieve_ms       INTEGER,
    rerank_ms         INTEGER,
    generate_ms       INTEGER,
    candidate_count   INTEGER,
    rerank_top_score  REAL,
    refused           INTEGER NOT NULL DEFAULT 0,
    trace_id          TEXT,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_qa_message_session ON ekbs_qa_message(session_id, turn);
CREATE INDEX IF NOT EXISTS idx_qa_message_time    ON ekbs_qa_message(created_at DESC);

-- 引用：只存 hash，不存片段全文（防审计库被越权访问时泄露内容）
CREATE TABLE IF NOT EXISTS ekbs_qa_citation (
    citation_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id   INTEGER NOT NULL REFERENCES ekbs_qa_message(message_id) ON DELETE CASCADE,
    cite_index   INTEGER,
    chunk_id     INTEGER,
    doc_id       INTEGER,
    doc_version  TEXT,
    heading_path TEXT,
    page_from    INTEGER,
    page_to      INTEGER,
    rerank_score REAL,
    acl_result   TEXT,                             -- pass | revoked | denied
    page_check   TEXT,                             -- pass | out_of_range
    snippet_hash TEXT,
    -- 图表引用随引用一起落库，**让引用自足**：渲染引用卡片时无需回查 ekbs_chunk
    -- （回查会让「历史引用的可追溯性」依赖于当时的分块是否还在，见 06 §4.11）
    figure_id    TEXT,
    image_path   TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_qa_citation_msg ON ekbs_qa_citation(message_id);
CREATE INDEX IF NOT EXISTS idx_qa_citation_doc ON ekbs_qa_citation(doc_id);

CREATE TABLE IF NOT EXISTS ekbs_doc_access_log (
    log_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    doc_id     INTEGER,
    action     TEXT,                               -- view | download | cite_click | upload
    channel    TEXT,
    ip         TEXT,
    ua         TEXT,
    result     TEXT,                               -- ok | denied | not_found
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_doc_access_time ON ekbs_doc_access_log(created_at DESC);

CREATE TABLE IF NOT EXISTS ekbs_perm_change_log (
    change_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    operator_id           INTEGER,
    object_type           TEXT,
    object_id             TEXT,
    change_type           TEXT,                    -- level | dept | acl_grant | acl_revoke | role | clearance
    field                 TEXT,
    old_value             TEXT,
    new_value             TEXT,
    reason                TEXT,
    policy_version_before INTEGER,
    policy_version_after  INTEGER,
    ip                    TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_perm_change_obj  ON ekbs_perm_change_log(object_type, object_id);
CREATE INDEX IF NOT EXISTS idx_perm_change_time ON ekbs_perm_change_log(created_at DESC);

CREATE TABLE IF NOT EXISTS ekbs_security_event (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT    NOT NULL,                   -- access_denied | injection_suspected | hallucinated_citation
    user_id    INTEGER,
    severity   TEXT    NOT NULL,                   -- low | medium | high
    detail     TEXT,                               -- JSON
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_security_event_time ON ekbs_security_event(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_security_event_type ON ekbs_security_event(event_type, created_at DESC);

-- =============================================================================
-- §5.5 缓存与运维（替代 Redis）
-- =============================================================================

-- 通用缓存（检索/答案/权限上下文）；检索与答案缓存的键**必须含 perm_fingerprint**
CREATE TABLE IF NOT EXISTS ekbs_cache (
    cache_key  TEXT    PRIMARY KEY,
    value      TEXT    NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_exp ON ekbs_cache(expires_at);

-- 钉钉消息幂等（替代 Redis 去重）
CREATE TABLE IF NOT EXISTS ekbs_msg_dedup (
    msg_id     TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_msg_dedup_time ON ekbs_msg_dedup(created_at);

-- 限流计数（替代 Redis 滑动窗口）；bucket 形如 global:- / user:10231 / conv:cidXXXX
CREATE TABLE IF NOT EXISTS ekbs_rate_limit (
    bucket       TEXT    NOT NULL,
    window_start INTEGER NOT NULL,
    count        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bucket, window_start)
);

-- 钉钉多轮会话上下文（替代 Redis Hash；键含用户维度，防群聊会话越权）
CREATE TABLE IF NOT EXISTS ekbs_session_ctx (
    conversation_id TEXT    NOT NULL,
    dingtalk_userid TEXT    NOT NULL,          -- 必须含用户维度：群聊 conversation_id 是共享的
    turn            INTEGER NOT NULL DEFAULT 0,
    payload         TEXT    NOT NULL DEFAULT '[]',  -- JSON：[{role, content}, ...]
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (conversation_id, dingtalk_userid)
);
CREATE INDEX IF NOT EXISTS idx_session_ctx_time ON ekbs_session_ctx(updated_at);

-- 运行指标（替代 Prometheus；由采集任务每 60s 写入，管理台读取）
CREATE TABLE IF NOT EXISTS ekbs_metric (
    metric_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    value      REAL    NOT NULL,
    labels     TEXT,                               -- JSON
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_metric_name_time ON ekbs_metric(name, created_at DESC);

-- =============================================================================
-- 初始化标记：应用层据此判断 Schema 是否已建立
-- =============================================================================
PRAGMA user_version = 1;

-- ---------- 审计表「只写不改」（append-only）----------
-- 为什么必须用触发器：SQLite 没有 GRANT / REVOKE，应用账号就是数据库文件属主，
-- 无法靠数据库权限体系限制自己。只能在库内用触发器硬拒（见 04 §6.3、02 §4.1 TC-ENV-010）。
--
-- 两条规则：
--   ① UPDATE 一律禁止，**永不放开** —— 篡改已记录的问答/引用/审计内容是首要风险；
--   ② DELETE 默认禁止，**唯一例外**是归档作业：它在一个事务内 DROP 掉 DELETE 触发器 →
--      删除超期行 → 立刻重建触发器，并在删除前把「归档了多少行」写入 ekbs_perm_change_log。
--      这条路径由 com.ekbs.scheduler 独占执行（单进程 + 单写者，事务内无并发窗口）。
--      见 04 §6.3 的归档作业实现。

-- ① ekbs_qa_message：答案与耗时，最核心的审计对象
CREATE TRIGGER IF NOT EXISTS trg_qa_message_no_update
BEFORE UPDATE ON ekbs_qa_message
BEGIN SELECT RAISE(ABORT, 'audit table ekbs_qa_message is append-only (UPDATE denied)'); END;

CREATE TRIGGER IF NOT EXISTS trg_qa_message_no_delete
BEFORE DELETE ON ekbs_qa_message
BEGIN SELECT RAISE(ABORT, 'audit table ekbs_qa_message is append-only (DELETE denied)'); END;

-- ② ekbs_qa_citation：引用与后置校验结果（page_check / acl_result）
CREATE TRIGGER IF NOT EXISTS trg_qa_citation_no_update
BEFORE UPDATE ON ekbs_qa_citation
BEGIN SELECT RAISE(ABORT, 'audit table ekbs_qa_citation is append-only (UPDATE denied)'); END;

CREATE TRIGGER IF NOT EXISTS trg_qa_citation_no_delete
BEFORE DELETE ON ekbs_qa_citation
BEGIN SELECT RAISE(ABORT, 'audit table ekbs_qa_citation is append-only (DELETE denied)'); END;

-- ③ ekbs_doc_access_log：文档访问留痕
CREATE TRIGGER IF NOT EXISTS trg_doc_access_log_no_update
BEFORE UPDATE ON ekbs_doc_access_log
BEGIN SELECT RAISE(ABORT, 'audit table ekbs_doc_access_log is append-only (UPDATE denied)'); END;

CREATE TRIGGER IF NOT EXISTS trg_doc_access_log_no_delete
BEFORE DELETE ON ekbs_doc_access_log
BEGIN SELECT RAISE(ABORT, 'audit table ekbs_doc_access_log is append-only (DELETE denied)'); END;

-- ④ ekbs_perm_change_log：权限变更留痕（含 policy_version 前后值）
CREATE TRIGGER IF NOT EXISTS trg_perm_change_log_no_update
BEFORE UPDATE ON ekbs_perm_change_log
BEGIN SELECT RAISE(ABORT, 'audit table ekbs_perm_change_log is append-only (UPDATE denied)'); END;

CREATE TRIGGER IF NOT EXISTS trg_perm_change_log_no_delete
BEFORE DELETE ON ekbs_perm_change_log
BEGIN SELECT RAISE(ABORT, 'audit table ekbs_perm_change_log is append-only (DELETE denied)'); END;

-- ⑤ ekbs_security_event：安全事件（越权尝试、告警命中）
CREATE TRIGGER IF NOT EXISTS trg_security_event_no_update
BEFORE UPDATE ON ekbs_security_event
BEGIN SELECT RAISE(ABORT, 'audit table ekbs_security_event is append-only (UPDATE denied)'); END;

CREATE TRIGGER IF NOT EXISTS trg_security_event_no_delete
BEFORE DELETE ON ekbs_security_event
BEGIN SELECT RAISE(ABORT, 'audit table ekbs_security_event is append-only (DELETE denied)'); END;

-- ⑥ ekbs_qa_session：**不能**完全 append-only —— 会话结束时需要写 ended_at 与 message_count，
--    因此只加 DELETE 触发器，UPDATE 用「白名单排除法」：除这两个字段外任一字段变化即拒绝。
--    注意用 `IS NOT` 而不是 `<>`：`<>` 在任一侧为 NULL 时结果为 NULL，WHEN 子句会当成假，
--    「从 NULL 改成有值」这种篡改就漏过去了。
CREATE TRIGGER IF NOT EXISTS trg_qa_session_no_delete
BEFORE DELETE ON ekbs_qa_session
BEGIN SELECT RAISE(ABORT, 'audit table ekbs_qa_session is append-only (DELETE denied)'); END;

CREATE TRIGGER IF NOT EXISTS trg_qa_session_update_guard
BEFORE UPDATE ON ekbs_qa_session
WHEN NEW.session_id       IS NOT OLD.session_id
  OR NEW.user_id          IS NOT OLD.user_id
  OR NEW.dt_userid        IS NOT OLD.dt_userid
  OR NEW.channel          IS NOT OLD.channel
  OR NEW.conversation_id  IS NOT OLD.conversation_id
  OR NEW.perm_fingerprint IS NOT OLD.perm_fingerprint
  OR NEW.clearance_at     IS NOT OLD.clearance_at
  OR NEW.started_at       IS NOT OLD.started_at
BEGIN
  SELECT RAISE(ABORT, 'ekbs_qa_session: only ended_at / message_count may be updated');
END;
