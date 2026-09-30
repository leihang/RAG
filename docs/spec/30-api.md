# 30 · ekbs-api 一期实现规格（问答链路 / 健康检查 / 权限上下文）

> 范围：只覆盖**一期**（问答链路 + 健康检查 + 权限上下文 + 引用回源），从 `方案/` 提炼可直接编码的接口契约。
> 文档优先级：以 `00-架构定稿与决策台账.md` 为准（来源：00 §9.2）。本文只提炼，不新增口径；冲突与不足逐条列入 §7，**不在本文拍板**。

---

## 0. 通用约定

- **服务与监听**：`ekbs-api` = FastAPI + `uvicorn ekbs.main:app --host 127.0.0.1 --port 8000 --workers 1`，由 launchd label `com.ekbs.api` 常驻，密钥经 `zsh -lc` 包装 `source ~/.ekbs/secrets.env` 注入（来源：01 §8.1、00 §1 D13）。`--workers 1` 是硬约束：权限缓存 L1 靠同进程失效，多 worker 会出现 ≤300s 旧权限窗口（来源：01 §7）。
- **网关**：Caddy 只做传输层，**不做鉴权**；401/403 一律由应用层返回；SSE 路由必须 `flush_interval -1`（来源：05 §3.3、05 §6.7、01 §9）。
- **检索阻塞**：FAISS `search` 是同步 C 调用，必须用 `asyncio.to_thread` 包住，否则阻塞唯一的事件循环（来源：04 §1.5）。
- **唯一出网点**：embed / rerank / LLM 都经统一网关出网，白名单见 04 §6.5（来源：04 §1.5）。
- **响应包络**：文档未统一。05 §6.3 给管理台约定 `{code, message, message_key, data, trace_id}`，但 01 §14 冒烟脚本对问答端点断言的是**裸字段**（`.answer`、`.citations[0].chunk_id`、`.citations[].page_check`），04 §7.3 的健康端点也返回**裸对象**。见 §7-C1。
- **分页体**（若使用）：`{items, total, page, page_size}`（来源：05 §6.3）。

### 0.1 三类鉴权（一期出现的全部形态）

| 形态 | 载体 | 适用端点 | 来源 |
|---|---|---|---|
| **A. 免登 JWT** | `Authorization: Bearer <access_token>`；载荷 `sub`(=ekbs `user_id`)、`clearance`、`dept_chain`、`policy_version`、`perm_fp`、`jti`；access 30m / refresh 7d | `/api/v1/qa/*`、`/api/v1/retrieve`、`/api/v1/assets/*`、`/api/v1/auth/me`、`/api/v1/perm/me` | 04 §3.3、§3.6 |
| **B. 内部回环 token** | 静态头 `EKBS_INTERNAL_TOKEN`，仅 `127.0.0.1`，适配器→api；connect 1s / read 30s（流式 180s） | `POST /api/v1/qa/chat`、`POST /api/v1/qa/chat/stream`（钉钉链路） | 04 §1.5 跳(3)、§3.4 |
| **C. 无需鉴权** | — | `/livez`、`/healthz`、`/readyz`、`/health/deps`；`POST /api/v1/auth/dingtalk/login`（凭 authCode+nonce） | 04 §7.3（health 路由无依赖）、04 §3.6 |

- 身份**只**取 JWT `sub` 或回环 token 对应的服务端身份；请求体里的 `user_id` / `clearance` / `perm_fp` 一律不作为身份（来源：04 §3.4、§6.2）。

### 0.2 权限上下文的取得方式（红线）

1. 请求进入 → 解析 JWT 得 `user_id`（来源：03 §4.1）。
2. 查 L1（进程内 LRU `perm:user:<user_id>`）→ L2（`ekbs_cache` 表键 `ekbs:perm:user:<user_id>`，TTL 300s）→ 未命中才查库（来源：03 §4.1、04 §3.3）。
3. 算出 `visible_shard_ids` 四步：**(a)** `kb_all_L1` 恒可见；**(b)** `kb_<dept>_L*` 需 `dept_chain` 命中且 `clearance` 达标；**(c)** ACL `allow` 显式并入；**(d)** 任一 ACL `deny` 命中即移除（deny 优先）（来源：03 §4.1）。
4. **权限指纹由服务端计算**：`sha256(user_id + sorted(visible_shard_ids) + clearance + sorted(dept_chain) + policy_version)`，五项顺序/分隔符/排序/类型都是契约（`clearance` 序列化为 `"1".."4"`，禁止 `"L2"`）（来源：00 §3.4，落地实现见 03 §4.3 / 04 §4.6）。
5. 指纹**绝不作为入参**：让调用方声明自己权限是一条不该存在的信任路径；`ekbs_qa_session.perm_fingerprint` 必须由 API 计算（来源：04 §5.5）。
6. 指纹用途：缓存分区键（`ekbs:perm:fp:<fp>`、检索缓存、答案缓存），键必须含指纹；严禁全局共享语义缓存（来源：00 §3.4、01 §7）。
7. `clearance` 只从服务端 `ekbs_user` 读；JWT 中该字段由服务端签名（来源：04 §6.2）。
8. 检索期即强制过滤：只加载可见分片，FTS5 带 `shard_id` 条件（来源：00 C1、04 §6.1(H)）。

---

## 1. 健康检查四端点（一期骨架必需，全部无需鉴权）

| 端点 | 方法 | 检查什么 | 出网 | 成功 | 失败 |
|---|---|---|---|---|---|
| `/livez` | GET | **什么都不查**，仅证明进程活着 | 否 | `{"status":"ok"}` 200 | — |
| `/healthz` | GET | SQLite 可读（2s 超时）+ CacheStore ping + `app.data_dir` 可写 | 否 | `{"status":"ok","checks":{...}}` 200 | 任一项非 `ok` → `{"status":"degraded","checks":{...}}` **503** |
| `/readyz` | GET | SQLite 可读 **且** Stream 状态已知（`stream_state.started_at is not None`），再用 `connected` 表达在线/断线 | 否 | `{"status":"ready","checks":{"sqlite":"ok","stream":"connected\|disconnected","reconnect_count":...}}` 200 | SQLite 非 ok 或 `started_at is None` → `{"status":"not_ready",...}` **503** |
| `/health/deps` | GET | SQLite / cache / `probe_all()` 探测云端 llm·embed·rerank / `dingtalk_stream` / 活跃分片数 `faiss_shards` 与向量数 `faiss_vectors` / `embedding_fingerprints` 去重数 / `fingerprint_match=(fps<=1)` / `disk_used_pct` | **是**（真实调用云端三模型） | 上述字段 200 | `sqlite != ok` 或 `fingerprint_match == false` → **503** |

（来源：04 §7.3；`/livez` 与 `/readyz` 命名以 04 为准，05 §8.1 写的是 `/health/live` 见 §7-C4）

- **`/health/deps` 绝不能作为 readiness 探针**：它会真实出网，探针只用 `/readyz`（来源：04 §7.3、05 §8.1）。
- **为什么没有 K8s 三探针**：macOS 原生部署下进程存活由 launchd 的「退出码 + `KeepAlive`（`SuccessfulExit=false`，`ThrottleInterval` 10s）」判定；`/healthz`、`/readyz` 是给人和脚本看的（来源：04 §7.3、01 §8.1）。
- **巡检期望**：`/healthz` 每 5 分钟巡检，失败只告警**不自动重启**；`/readyz` 连续 3 次 not_ready 触发 P1（来源：04 §7.3）。

### 1.1 冒烟脚本（01 §14）对四个端点的期望

| 断言 | 期望 | 备注 |
|---|---|---|
| `curl -sf $API/readyz \| jq -e '.status=="ready"'` | 必须通过 | 冷启动头几秒会 not_ready，脚本需容忍重试（来源：04 §7.3） |
| `curl -sf $API/health/deps \| jq -e '.faiss_shards>0'` | 至少一个 active 分片 | 索引未加载属「拒绝服务并告警」（来源：00 §2.3） |
| `.embed==true` / `.llm==true` / `.rerank==true` / `.dingtalk_stream==true` | 全部为 true | `probe_all()` 的三项 + Stream 连接态（来源：01 §14） |
| `.fingerprint_match==true` | 必须 true | `embedding_fingerprints ≤ 1`，防不同模型向量混用（来源：00 C3、04 §7.3） |
| `/livez`、`/healthz`、`disk_used_pct` | **冒烟脚本未覆盖** | 见 §7-C16 |

---

## 2. 问答链路（一期骨架必需）

| 端点 | 方法 | 用途 | 流式 | 鉴权 |
|---|---|---|---|---|
| `/api/v1/qa/chat` | POST | 同步问答，一次返回答案 + 引用（钉钉非流式回退、脚本、H5） | 否 | A 或 B |
| `/api/v1/qa/chat/stream` | POST | SSE 流式问答（钉钉卡片、H5 逐字） | **是** | A 或 B |
| `/api/v1/retrieve` | POST | 只做检索，返回分片召回 + RRF + rerank 明细 | 否 | A |

（来源：05 §8.1、01 §14；04 里同一动作为 `/v1/qa/ask`，见 §7-C2 / §7-C3）

### 2.1 请求 / 响应（按冒烟脚本与调用方可反推的最小契约）

| 端点 | 请求关键字段 | 响应关键字段 |
|---|---|---|
| `/api/v1/qa/chat` | `question`（必填）、`stream:false`、`conversation_id`、`rewritten_question`、`source`、`channel` | `answer`（string）、`citations[]`（每项至少含 `chunk_id`、`page_check`）、`refused`、`trace_id` |
| `/api/v1/qa/chat/stream` | 同上 | SSE 事件：`{type:"delta", content}` / `{type:"citations", items:[...]}` / `{type:"error", message}` |
| `/api/v1/retrieve` | `query`（必填）、`top_k`、密级/时间过滤 | `{items:[{chunk_id, score, heading_path, ...}]}` |

（来源：01 §14、04 §5.7 消费端 `ask_stream`）
适配器侧调用参数固定为 `user_id, dingtalk_userid, conversation_id, question, rewritten_question, source="dingtalk", channel="robot"`，**不传 `session_id`、不传 `perm_fp`**（来源：04 §5.7）。

### 2.2 链路参数基线（一期必须按此实现）

| 环节 | 基线 | 来源 |
|---|---|---|
| 查询改写 | 多轮对话启用；无指代词且长度充足时透传；失败回落「上一轮问题 + 当前问题」拼接 | 00 §2.1、04 §5.6 |
| FAISS 分片稠密召回 | topK 30，内积（向量已归一化） | 00 §2.1 |
| FTS5 BM25 召回 | topK 30，jieba 预分词写入 | 00 §2.1 |
| RRF | k = 60 | 00 §2.1 |
| 云端 rerank | 输入 15 → 输出 5，`relevance_score ≥ 阈值` | 00 §2.1（阈值数值见 §7-C10） |
| **C5 文档 ACL 闸门** | rerank 之后 → Prompt 之前，对每个候选 chunk 执行 `can_read_document`；deny/未授权不得进上下文、答案缓存或日志正文 | 00 §1 C5 |
| 父块扩展 | 命中子块 → 取父块全文；扩展后的父块仍须过 C5 | 00 §2.1 |
| 生成 | temperature 0.1 / top_p 0.8 / max_tokens 2048 | 00 §2.1、01 §7 |
| 拒答 | rerank 最高分 < 阈值 或 上下文无有效内容 | 00 §2.1 |
| 降级 | rerank 不可用 → 跳过精排取 RRF Top-5；查询期 embed 不可用 → 明确拒答，**禁止**降级为只走 FTS5 | 00 §2.3 |

### 2.3 引用与可溯源性（一期必须实现）

- 每个进 LLM 上下文的 chunk 分配 `[1][2]…`，编号→`chunk_id` 映射随请求保存在请求上下文（来源：00 §2.2）。
- 答案中的引用编号不在映射表内 ⇒ **幻觉引用**，剔除该引用或降级为「未找到可靠依据」（来源：00 §2.2）。
- 引用后置校验两件事：① 该 chunk 对当前用户仍可见（防长对话期间权限变更）；② 页码落在 `page_from~page_to` 区间（来源：00 §2.2）。
- 判决写入 `ekbs_qa_citation.acl_result`（`pass|revoked|denied`）与 `page_check`（`pass|out_of_range`）（来源：01 §5.4、04 §6.3）。
- 引用只存 `snippet_hash`，不存片段全文；`figure_id` / `image_path` 随引用落库，**让引用自足**（渲染卡片无需回查 `ekbs_chunk`）（来源：01 §5.4、04 §6.3）。

### 2.4 耗时埋点与审计写入时机

**逐列落库**到 `ekbs_qa_message`，是 `00` §4 SLO 的唯一取证来源（来源：04 §6.3、01 §5.4）：

| 字段 | 含义 / 写入时机 |
|---|---|
| `retrieve_ms` | 分片召回（FAISS + FTS5）阶段耗时；检索段结束时记 |
| `rerank_ms` | 云端 rerank 阶段耗时；rerank 返回时记 |
| `generate_ms` | LLM 生成阶段耗时；生成结束时记 |
| `ttft_ms` | 首 token 延迟；收到首个 delta 时记（同步路径则为首包时间） |
| `total_ms` | 整轮耗时；收尾时记（SLO：TTFT P95 ≤5s、完整回答 P95 ≤12s） |
| `candidate_count` | 进 rerank 的候选数 |
| `rerank_top_score` | rerank 最高分，拒答判定依据 |
| `refused` | 是否拒答（0/1） |
| `model` / `prompt_tokens` / `completion_tokens` | 生成用量 |
| `question` / `rewritten_query` / `answer` / `trace_id` / `turn` / `role` | 内容与关联 |

**写入顺序**（来源：04 §6.1(L)、§5.5、§6.3）：

1. 组装权限上下文并算指纹后，先取/建 `ekbs_qa_session`（键 `conversation_id + user_id + channel + ended_at IS NULL + started_at > TTL`；**指纹变化不影响会话复用**——会话是对话历史不是权限凭据，权限每轮重算）（来源：04 §5.5）。
2. 写 `ekbs_qa_message` 的 `role='user'` 行（含 `rewritten_query`）。
3. 生成收尾后写 `role='assistant'` 行，一次性写全 §2.4 上表所有列。**逐列落库，不是只写 `total_ms`**。
4. 引用后置校验完成后逐条写 `ekbs_qa_citation`。
5. 回源/取图命中写 `ekbs_doc_access_log`。
6. **审计表只写不改**：`ekbs_qa_message` / `ekbs_qa_citation` / `ekbs_doc_access_log` / `ekbs_perm_change_log` / `ekbs_security_event` 由触发器硬拒 UPDATE/DELETE（12 个触发器）；应用层只提供 INSERT 路径。唯一例外是 `ekbs_qa_session.ended_at` / `message_count` 的收尾更新（来源：01 §5.6、04 §6.3）。
7. 超时/放弃路径也必须留痕：ACCEPT 后未完成的在途任务要写 `ekbs_security_event`，否则用户看到「没有回复」而无从复盘（来源：04 §7.4）。
8. 留存 ≥180 天，由调度器按月归档为 `audit-YYYYMM.jsonl.gz`（来源：00 §3.5、04 §6.3）。

---

## 3. 引用回源 / 图片资源端点（一期骨架必需）

`GET /api/v1/assets/figure/{chunk_id}` — 鉴权 A，且**必须做可见分片复检**；这是**唯一允许读取 `/data/ekbs/parse/` 的入口**（来源：04 §6.6、06 §4.11.3）。

### 3.1 ACL 复检流程（五步，一步不可省）

1. `SELECT doc_id, shard_id, chunk_type, figure_id, image_path FROM ekbs_chunk WHERE chunk_id = ?`；取不到，或 `chunk_type ∉ ('figure','table')`，或 `image_path` 为空 → **404**（来源：04 §6.6）。
2. 用**服务端查到的 `shard_id`** 去比对当前用户的 `visible_shard_ids`；**不信任 `chunk_id` 来自谁**（它只是入参）→ 不在其中则写 `ekbs_security_event(event_type='figure_access_denied')` 并 **404**（来源：04 §6.6、06 §4.11.3）。
3. 统一用 **404 而不是 403**：403 会泄露「这个 chunk 存在，只是你没权限」，即文档存在性（来源：04 §6.6、03 §4.5、06 §4.11.3）。
4. 路径穿越防护：`(PARSE_ROOT / image_path).resolve()` 后断言以 `PARSE_ROOT + "/"` 为前缀（比字符串 `startswith` 可靠，能吃掉 `../` 与符号链接）且文件存在；否则写 `figure_path_escape` 事件并 404（来源：04 §6.6）。
5. 命中后写 `ekbs_doc_access_log(action='view', channel='citation', result='ok')`，返回文件并带 `Cache-Control: private, max-age=300`（私有缓存，不进共享缓存）（来源：04 §6.6、06 §4.11.3）。

三条检查各自对应的真实失败模式：缺第 1 条 → 端点变成「任意 chunk_id 都能触发文件读取」的探测器；缺第 2 条 → 图片绕过 ACL；缺第 4 条 → `.image_path` 被污染即可读任意文件（来源：04 §6.6）。

### 3.2 为什么禁止 Caddy 静态托管 `/data/ekbs/parse`

一句话：静态路径一旦可猜（`/data/ekbs/parse/<sha>/images/fig1.png`），任何能访问内网入口的人都能取走 L3/L4 文档里的图，而图里往往正是金额、组织架构、审批流程——等于**在资源层把刚做对的权限又开一个口子**（来源：06 §4.11.3、04 §6.6、00 §2.2）。

- Caddyfile 两条路由里**没有**指向 `/data/ekbs/parse` 的 `file_server`；「配置里没有」和「验证过没有」是两回事，由 `02` TC-IMG-003 验证（来源：04 §6.6）。
- 图表引用在卡片上**懒加载 + 默认折叠**：钉钉卡片对图片数量与总大小有限制（来源：06 §4.11.4、05 §4.6）。
- 指标：`/v1/assets/*` 越权尝试次数正常应为 0，需记录并告警（来源：06 §4.11.5）。

---

## 4. 一期端点总清单（15 条）

**骨架必需（12 条）**：`/livez`、`/healthz`、`/readyz`、`/health/deps`、`POST /api/v1/auth/dingtalk/login`、`GET /api/v1/auth/me`、`GET /api/v1/perm/me`、`POST /api/v1/qa/chat`、`POST /api/v1/qa/chat/stream`、`POST /api/v1/retrieve`、`GET /api/v1/assets/figure/{chunk_id}`、`POST /api/v1/auth/refresh`。
（来源：04 §7.3、04 §3.6、05 §8.1；`/api/v1/perm/me` 返回可见分片列表、密级、指纹，见 §7-C7）

**可后置（3 条）**：`POST /api/v1/auth/logout`（jti 进黑名单，`ekbs:jwt:blacklist:{jti}` TTL 到 `exp`）、`GET /api/v1/system/health`、`GET /v1/internal/users/{userid}`（见 §7-C13）。

**二期**：其余 **68 条**属管理台 CRUD 与运维（知识库/文档/分块/解析/权限/评测/审计/看板/系统配置），二期实现。位置：`05-管理台UI设计方案.md` §8.1「API 接口清单（v3.0）」（该清单共 80 行，一期引用其中 12 行；`/livez` 对应其 `/health/live`，`/healthz`、`/v1/internal/users/{userid}` 未在该清单登记）。

---

## 5. 统一错误码（一期相关子集）

| 错误码 | HTTP | 场景 | 来源 |
|---|---|---|---|
| `AUTH_TOKEN_EXPIRED` | 401 | access token 过期，前端尝试 refresh 一次 | 05 §5.8 |
| `DINGTALK_AUTH_CODE_INVALID` | 401 | 免登 authCode 无效/过期 | 05 §5.8、04 §3.6 |
| `REPLAY_DETECTED` | 401 | `ts` 超 ±300s 或 nonce 已消费 | 04 §3.6 |
| `AUTH_CODE_USED` | 409 | authCode 一次性消费失败 | 04 §3.6 |
| `DINGTALK_UNAVAILABLE` | 502 | 换 authCode 时钉钉不可达 | 04 §3.6 |
| `PERM_DENIED` | 403 | 权限点不足 | 05 §5.8 |
| `PERM_POLICY_CHANGED` | 409 | 服务端发现 `policy_version` 已变 | 05 §6.3 |
| `USER_NOT_SYNCED` / `USER_DISABLED` | 403 | 账号未同步 / 已停用离职 | 04 §6.1 |
| `RATE_LIMITED` | 429 | 三级限流 | 04 §6.1 |
| `CONTENT_BLOCKED` | 400 | 敏感词 / 注入检测拦截 | 04 §6.1 |
| `NO_PERMITTED_RESULT` | 200 语义 | 检索期过滤后无召回（提示「未找到您有权限访问的相关内容」） | 04 §6.1 |
| `PERM_CHANGED` | 403 | 回源二次鉴权时权限已变更 | 04 §6.1 |
| `SHARD_UNAVAILABLE` / `FTS5_UNAVAILABLE` | 503 | 分片加载失败 / 全文检索不可用 | 05 §5.8 |
| `MODEL_SERVICE_UNAVAILABLE` | 503 | 云端模型不可用（已路由备用） | 05 §5.8 |
| `INTERNAL_ERROR` | 500 | 未分类异常，响应含 `trace_id` | 05 §5.8 |

- 后端返回 `code` + `message_key`，前端优先按 `message_key` 查语言包、回退 `message`（来源：05 §5.10）。
- `/api/v1/qa/chat` 无凭证必须非 200（冒烟第 4 组「权限红线」）（来源：01 §14）。

---

## 6. 一期内不实现（明确排除）

- 本地模型 / 本地 reranker / 多模态 embedding：一期图片走「图注 + 图内 OCR 文本进索引、原图可回源」，**唯独不进向量库**（来源：06 §4.11、06 §5.3）。
- Redis / Celery / PostgreSQL / Milvus / ES / K8s / Docker：v3.0 已全部替换为 `CacheStore` + `ekbs_scheduler` + SQLite + 本地目录 + launchd（来源：00 §1、00 §9.1）。
- L4 语料入库、未落地 C5 前的真实 L3 入库（`permission.l4_enabled=false`、`l3_ingest_enabled=false`）（来源：00 §3.2、01 §7）。
- 合规闸未开（`permission.corpus_egress_ack=false`）时，真实语料入库任务必须被识别并拒绝（来源：00 §7）。
- 管理台写接口（知识库配置、模型参数、Prompt 模板不在 API 暴露写接口，它们是 `/opt/ekbs/etc/*.yaml`，走 Git 评审）（来源：05 §8.1）。
- `/v1/leader/*` 主备租约、mTLS、Service JWT：v3.0 已删除（来源：04 §7.3、§7.5）。

---

## 7. 实现待确认清单

以下为文档互相矛盾或不足以直接编码之处，**本文不代为决策**。

| # | 事项 | 冲突/缺口 |
|---|---|---|
| C1 | **响应包络** | 05 §6.3 约定 `{code,message,message_key,data,trace_id}`；01 §14 冒烟对问答断言裸字段（`.answer`、`.citations[0].chunk_id`、`.citations[].page_check`）；04 §7.3 健康端点返回裸对象。三处不一致，需定「哪些端点走包络」。 |
| C2 | **路径前缀** | 04 全篇用 `/v1/...`（§6.6 `/v1/assets/figure/{chunk_id}`、§1.5 `/v1/qa/ask`）；01 §14 与 05 §8.1 用 `/api/v1/...`。且 01 §9 的 Caddy `handle /api/*` **不剥前缀**（未用 `handle_path`），上游会看到 `/api/v1/...`。 |
| C3 | **问答端点命名** | 04 §1.5/§5.7 `/v1/qa/ask`（SSE）vs 05 §8.1 `/api/v1/qa/chat` + `/api/v1/qa/chat/stream`；冒烟只用同步的 `/api/v1/qa/chat`。 |
| C4 | **健康端点命名** | 05 §8.1 登记的是 `/health/live`，且**未登记 `/healthz`**；04 §7.3 是 `/livez` + `/healthz`。本期按 04 实现，05 表格需同步。 |
| C5 | **健康端点鉴权** | 04 §7.3 代码无任何 `Depends`，即完全免鉴权；但 `/health/deps` 会出网且暴露分片数、向量数、模型可用性与磁盘水位。未规定是否限回环或加内部 token。 |
| C6 | **内部回环 token 的校验与身份映射** | 04 §1.5 规定适配器→api 用 `EKBS_INTERNAL_TOKEN`；§5.7 调用时把 `user_id` 放进请求体。但 04 §3.4/§6.2 规定「任何接口都不接受调用方传入的 userid 作为身份」。哪些路径接受内部 token、如何换成身份，全文无定义。 |
| C7 | **指纹与 `perm/me` 的下发边界** | 05 §8.1 规定 `GET /api/v1/perm/me` 返回「可见分片列表、密级、指纹」；06 §7.3 第 5 条要求**删除请求头 `X-Perm-Fingerprint`**（状态仍为 ⏳），而 05 §6.3 表格仍写「权限指纹每个请求自动透传」。两者对「指纹能否离开服务端」口径相反。 |
| C8 | **同步问答响应字段** | 只有冒烟脚本反推出 `answer` / `citations[].chunk_id` / `citations[].page_check`；05 §8.1 只给路径与权限，无正式字段表。 |
| C9 | **SSE 事件契约** | 仅由消费端反推 `type ∈ {delta, citations, error}`（04 §5.7）；`event:` 行名、心跳、正常结束事件、`retry` 均未定义。 |
| C10 | **rerank 阈值数值** | 01 §7 `retrieval.rerank.min_score: 0.30`；05 §4.3/§4.7 默认 `0.35`；00 §2.1 只写「≥ 阈值」未给值。 |
| C11 | **rerank 候选数** | 00 §2.1 与 01 §7 为 15→5；04 §6.1(I) 写「rerank Top-20 → 过滤 → Top-5」；05 §4.7 线框图展示「30→15」与「截断至 20」。四处不一致。 |
| C12 | **清单条数** | 05 §8.1 实测 80 行（文档头亦称 80 条）；委托口径为 79 条。以哪个为准未定。 |
| C13 | **`GET /v1/internal/users/{userid}`** | 04 §3.1 免登时序第 5 步列了它，但 §3.6 的实现是进程内 `resolve_by_dingtalk_userid(db, ...)`，未走 HTTP。该端点是否一期需要未定。 |
| C14 | **配置键缺失** | 04 §5.5 用 `settings.session["context_ttl_seconds"]`（会话复用 TTL），§7.4 用 `adapter.graceful_shutdown_seconds`；01 §7 的 `application.yaml` 模板里**都没有**这两个键。 |
| C15 | **Caddyfile 两份** | 04 §6.6 描述的是双域名（`kb.内网域名` 静态 + `api-kb.内网域名` 反代）；01 §9 给的是单域名 `kb.公司域名.com` + `handle /api/*`。直接影响 C2 的前缀结论与 SSE `flush_interval` 落点。 |
| C16 | **冒烟覆盖缺口** | 01 §14 未校验 `/livez`、`/healthz`，也未校验 `/health/deps.disk_used_pct`（磁盘水位 >85% 应停止入库，00 §2.3）。 |
| C17 | **`_disk_used_pct` 实现** | 04 §7.3 用 `os.statvfs`（Unix 可用）；实现时需确认 macOS 上返回值与 `/data/ekbs` 所在卷一致，且该调用是阻塞 IO，是否应包 `asyncio.to_thread`。 |
| C18 | **同步路径的 TTFT 语义** | `ttft_ms` 在非流式路径上无「首 token」可测；04 §6.3 要求逐列落库但未说明同步路径如何取值（留空 / 记首包时间）。 |
