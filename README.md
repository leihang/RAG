# EKBS 应用仓库（开发机骨架）

> **当前状态：一期骨架可运行 + 云端代码路径已就绪**
> 入库 → 分片 → 混合检索 → RRF → 精排 → C5 闸门 → 生成 → 引用校验 整条链已跑通；
> 离线（mock 模型）与**云端代码路径**（respx 按真实契约模拟百炼三端点）都有端到端用例；
> `402` 个 pytest 用例全绿。**云端三模型已选定并用真实 key 跑通全链路**（`qwen3.7-text-embedding-flash@1024` + `qwen3.7-text-rerank` + `qwen-max`），见 §三。
>
> 权威设计文档在 [`方案/`](方案/)（8 份）。本文件只讲**代码怎么跑、已做到哪、还差什么**；
> 与 `方案/` 冲突时以 `方案/` 为准，并请把本文件与代码一起改掉。

---

## 一、五分钟跑起来

```bash
cd /Users/lh/Documents/工作学习/AI项目/RAG
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt   # 首次

export EKBS_CONFIG=config/application.dev.yaml
export EKBS_INTERNAL_TOKEN=dev-internal-token      # 联调用；生产放 ~/.ekbs/secrets.env

.venv/bin/python -m ekbs.tools.init_db             # 建库（29 表 / 12 触发器 / WAL 自检）
.venv/bin/python -m ekbs.tools.seed_demo           # 合成语料：3 部门 / 3 用户 / 3 文档

# 起服务（开发机版「四服务」，与 launchd 同一套入口命令）
bash scripts/dev_services.sh start all
bash scripts/dev_services.sh status
bash scripts/dev_services.sh logs api

# 或只起 API：
# .venv/bin/uvicorn ekbs.main:app --host 127.0.0.1 --port 8000 --workers 1
```

另开一个终端：

```bash
ALICE=$(sqlite3 var/index/ekbs.db "SELECT user_id FROM ekbs_user WHERE dingtalk_userid='demo_alice';")
curl -s localhost:8000/readyz
curl -s -X POST localhost:8000/api/v1/qa/chat -H 'Content-Type: application/json' \
  -H 'X-Internal-Token: dev-internal-token' -H "X-Internal-User-Id: $ALICE" \
  -d '{"question":"年假有多少天","stream":false}' | python3 -m json.tool
```

跑测试：

```bash
.venv/bin/python -m pytest tests/ -q          # 402 passed；不出网、不花钱
```

跑**开发机冒烟**（与 `deploy/scripts/smoke.sh` 同口径，但适配开发布局）：

```bash
bash scripts/dev_smoke.sh --start --with-qa --redline
# PASS=23 FAIL=0 SKIP=0 —— Schema/分片/四服务/健康端点/无凭证红线/真实问答/越权红线逐条过
```

---

## 二、代码与文档的对应关系

| 包 / 文件 | 职责 | 权威来源 |
|---|---|---|
| `ekbs/config.py` | 配置加载 + **密钥反向检查** + 合规闸/L3 闸 | `01` §7、`04` §2.6、`00` §7 |
| `ekbs/db.py` | SQLite 连接、PRAGMA、Schema 应用与自检 | `01` §5.0/§5.7 |
| `ekbs/provider/` | 模型抽象 + **嵌入指纹**（C3）+ 离线 mock + **百炼云端实现** | `00` C3、§3.1、`docs/spec/40` |
| `ekbs/provider/http_client.py` | 错误归一化 + 带抖动的指数退避（按已核实错误码分流） | `docs/spec/40` §5 |
| `ekbs/provider/dashscope.py` | 嵌入（兼容模式）/ 重排（**原生 HTTP**）/ 生成（兼容 + fallback + SSE） | `docs/spec/40` §2/§3/§4 |
| `ekbs/permission/fingerprint.py` | `perm_fingerprint` **全仓库唯一实现** | `00` §3.4 |
| `ekbs/permission/router.py` | `visible_shard_ids` **唯一入口** + `bump_policy_version` | `03` §4.1、`06` §2.5 |
| `ekbs/permission/acl.py` | ACL 判定 + **C5 闸门** `can_read_document` | `00` C5、`03` §6.3 |
| `ekbs/permission/changes.py` | **权限变更服务**：数据更新 + **分片迁移** + 缓存失效 + 审计（四件事同事务） | `03` §6.4 |
| `ekbs/ratelimit.py` | 三维限流（全局/用户/会话），落 `ekbs_rate_limit` | `04` §5.2 |
| `ekbs/archive.py` | **审计归档**：留存期到期 → 导出 JSONL → 临时摘除法触发器删除 → 立刻复原 | `00` §3.5、`01` §5.6 |
| `ekbs/session.py` | 多轮会话上下文（`ekbs_session_ctx`，键含用户）+ **查询改写** | `00` §2.1、`04` §5.5 |
| `ekbs/security.py` | **内容安全四层**：L0 PII 打码 / L1 敏感词 AC 自动机（热加载）/ L3 注入降级 / L4 输出侧泄漏 | `04` §6.4 |
| `ekbs/idempotency.py` | 消息幂等守卫（`ekbs_msg_dedup`；失败可重投） | `04` §5.4 |
| `ekbs/observability/` | **指标登记表**（计数器/仪表量/直方图，快照语义同 Prometheus）+ **17 条阈值告警**（唯一阈值定义处） | `04` §7.7 |
| `ekbs/dingtalk/token.py` | 钉钉 access_token 客户端（新版 `/v1.0/oauth2/accessToken`、按应用维度缓存、过期前刷新） | `docs/spec/50` §1 |
| `ekbs/dingtalk/client.py` | OpenAPI 客户端：单聊/群聊发消息、卡片创建与投放、**AI 卡片流式**（`isFull=true`） | `docs/spec/50` §3 |
| `ekbs/dingtalk/handler.py` | Stream 消息处理：幂等 → 限流 → 身份（`senderStaffId`）→ 回环调 API → 卡片/消息回复 | `docs/spec/50` §2、`04` §5 |
| `ekbs/dingtalk/auth.py` | H5 免登：`authCode` 换身份 + 一次性消费防重放 | `docs/spec/50` §4 |
| `ekbs/dingtalk/directory.py` | 通讯录客户端：部门树**递归**、成员**游标分页**、离职记录 | `docs/spec/50` §5 |
| `ekbs/dingtalk/sync.py` | **组织同步**：全量同步 + 离职同步 + 对账 + 权限回收滞后指标 | `04` §4 |
| `ekbs/index/shard.py` | 分片命名与归属判定 | `00` §3.1、`06` §2.2 |
| `ekbs/index/faiss_store.py` | FAISS 分片索引（创建/加载/追加/自检/原子重建） | `06` §2.3/§2.4 |
| `ekbs/index/fts.py` | FTS5 + jieba 预分词 + **权限 SQL 下推** | `03` §5.3 |
| `ekbs/retrieval/rrf.py` | RRF 融合（k=60） | `00` §2.1 |
| `ekbs/retrieval/pipeline.py` | 链路编排：召回 → RRF → rerank → C5 → 父块 → 引用校验 | `00` §2.1/§2.2 |
| `ekbs/cache/store.py` | L1 LRU + L2 `ekbs_cache`（替代 Redis） | `01` §5.5、`03` §4.4 |
| `ekbs/ingest/` | 解析（可插拔）/ 四层分块 / 入库编排 | `06` §3、`docs/spec/20` |
| `ekbs/audit.py` | 问答审计三表（append-only） | `01` §5.4/§5.6 |
| `ekbs/api/` | FastAPI 应用、鉴权、健康端点、问答、图片回源、**管理台只读接口** | `04` §7.3、`05` §8.1/§8.2 |
| `ekbs/authz.py` | RBAC 判定（角色 → 权限点；``super_admin`` 通配） | `01` §5.3、`05` §3.4 |
| `ekbs/api/docs.py` | **文档写接口**：登记/编辑（含分片迁移）/发布/下线/删除/批量/上传（秒传）/索引重建 | `05` §8.2 |
| `ekbs/api/parse_tasks.py` | **解析任务中心**：任务列表（含成功率）、详情、结果预览、重跑、取消 | `05` §8.2 |
| `ekbs/api/playground.py` | **检索调试台**：改写/逐环节召回/完整链路/权限模拟/路由解释 | `05` §8.2 |
| `ekbs/api/chunks.py` | **分块预览与编辑**：父子块树/详情/导出/编辑（换向量+重写 FTS）/拆分/合并/重嵌入 | `05` §8.2 |
| `ekbs/api/perm_admin.py` | **权限管理页**：角色 CRUD/分配、用户密级与状态、部门视图、ACL 增删、权限模拟、变更审计 | `05` §8.2 |
| `ekbs/rbac_catalog.py` | 权限点目录 + 角色授权矩阵（**单一来源**，seed 与迁移共用） | `05` §3.4/§8.2 |
| `ekbs/tools/migrate_permissions.py` | **权限数据迁移**：补点 / 合并历史命名 / 按矩阵重放授权 / 留痕 | — |
| `ekbs/main.py` | uvicorn 入口 `ekbs.main:app` | `01` §8.1 |
| `ekbs/worker.py` | `python -m ekbs.worker` 解析 worker | `00` D13 |
| `ekbs/scheduler.py` | `python -m ekbs.scheduler` APScheduler 单实例 | `00` D13、`04` §7.1.4 |
| `ekbs/stream.py` | `python -m ekbs.stream` 钉钉适配器（`/livez` `/readyz`） | `04` §7.3 |
| `ekbs/tools/` | `init_db` / `new_shard` / `rebuild_shard` / `sync_org` / `seed_demo` / **`backup`（含恢复演练）** / **`import`（批量入库）** | `01` §5.7/§6.3/§12/§13/§16、`04` §4 |
| `scripts/dev_services.sh` | 开发机四服务管理（与 launchd 同一套入口，无 root） | `01` §8 |
| `scripts/dev_smoke.sh` | **开发机冒烟**（与生产 `smoke.sh` 同口径）：Schema/分片/四服务/健康端点/两条红线 | `01` §14、`04` §7.3 |
| `scripts/acceptance.sh` | **上线接收检查**：把 DoD 逐项 + 迁移闸门 + 配置断言 + 契约测试 + 冒烟 + 备份演练汇总成**一次命令的结论**（READY / NOT-READY + 待外部条件清单） | `01` §0.3 |
| `docs/spec/` | 四份**实现规格**（从 `方案/` 提炼，含待确认清单） | — |

`sql/001_init.sql` 是 `deploy/sql/001_init.sql` 的逐字副本（部署时它落在 `$APP_DIR/sql/`）。

---

## 三、已实现 / 未实现（诚实清单）

### ✅ 已实现并验证

| 项 | 验证方式 |
|---|---|
| Schema 29 表 / 12 审计触发器 / WAL / integrity | `init_db` 自检 + `tests/test_schema.py` |
| 审计表 append-only（含 `IS NOT` 防 NULL 篡改） | `tests/test_schema.py` |
| 权限指纹逐字契约（含 `"L2"` 等违约写法报错） | `tests/test_fingerprint.py`（硬编码摘要比对） |
| 分片路由：密级 × 部门 × ACL allow/deny，**ACL 不能突破 clearance** | `tests/test_permission.py` |
| C5 闸门：状态/时效→密级→kb→deny→allow→(跨部门 shard allow)→L4→部门 | `tests/test_permission.py` |
| **越权 0 泄漏**：HR 用户召不回财务 L3 内容 | `tests/test_retrieval.py` + 真实服务 curl 验证 |
| 混合检索：FAISS 分片 + FTS5（权限 SQL 下推）+ RRF(60) + rerank + 父块扩展 | `tests/test_retrieval.py` |
| 嵌入不可用 → **明确拒答**（不降级为只走 FTS5） | `tests/test_retrieval.py` |
| 引用后置校验：编号存在 + ACL 二次确认 + 页码区间 + figure 字段 | `tests/test_retrieval.py` |
| 缓存按指纹分区（答案/检索键必含指纹） | `tests/test_retrieval.py` |
| 健康端点四个 + 无凭证 401 + `/v1` 别名 | `tests/test_api.py` + 真实服务 curl |
| 入库编排：四层分块、分片归属、批量嵌入、FTS 写入、多部门 shard ACL 授权 | `seed_demo` + `worker --once` |
| 定时作业 9 项（缓存清理/指标/对账/会话清理…） | `scheduler --once`，三方对账 `diff_ratio=0.0` |
| 图片回源 ACL 复检 + 路径穿越防护 | `tests/test_api.py` |
| **百炼云端 provider**：嵌入（兼容模式、≤10 条、按 index 还原、维度断言）、重排（原生 HTTP）、生成（fallback + SSE） | `tests/test_dashscope_provider.py`（17 用例，respx 离线） |
| **云端代码路径端到端**：入库→检索→生成→引用校验全走 dashscope provider | `tests/test_cloud_integration.py` |
| **部署契约**：四 plist 入口/环境/密钥纪律、preflight 硬断言、smoke 探针路由、两份 SQL 未漂移 | `tests/test_deployment_contract.py`（18 用例） |
| 开发机四服务起停与探针 | `bash scripts/dev_services.sh start all` + `status` |
| **审计归档**：180/365 天留存、删前导出 JSONL、触发器摘除后**必须复原**、出错整体回滚 | `tests/test_archive.py`（6 用例） |
| **备份与恢复演练**：SQLite 在线备份 API（非 cp）、manifest 带 sha256、恢复后逐项核对分片 | `tests/test_backup_restore.py`（6 用例）+ 真实 CLI 演练 |

> **DoD 对照**（`01` §0.3；可用 `bash scripts/acceptance.sh` 一次判定）：
> ✅ 备份已执行且可恢复（`ekbs.tools.backup` 实测）；✅ launchd 四服务（开发机版同入口脚本 + 生产 plist 契约测试）；
> ✅ 越权 0 泄漏（`dev_smoke.sh --redline` + 用例）；✅ 合规闸与 C5 就位；✅ 冒烟全绿。
> ⏳ 首批 200 份真实制度入库、真机 RPO/RTO 数字：需真机与合规确认。
| **权限变更服务**：调密级/改部门 → chunk 迁到目标分片（重嵌入 + 重写 FTS5 + 回写 faiss_row）+ policy_version 同事务自增 + 审计 | `tests/test_permission_changes.py`（10 用例） |
| 三维限流（全局/用户/会话），超限 429 + `Retry-After` | `tests/test_ratelimit.py`（8 用例） |
| 多轮会话上下文（群聊按用户隔离、TTL、轮数裁剪）+ 查询改写（启发式/LLM/off）+ 审计记 `rewritten_query` | `tests/test_session.py`（15 用例） |
| **内容安全四层**：敏感词拦截(400+审计)、PII 打码后继续、注入降级为纯检索、**输出侧未授权文档名替换** | `tests/test_content_security.py`（17 用例）+ 真实服务 curl 验证 |
| 消息幂等：首次受理放行、重复拒绝、失败可重投、过期清理 | `tests/test_idempotency.py`（7 用例） |
| **批量入库工具**（`01` §12 的 `--dir/--kb-code/--level/--dept-ids/--effective-date/--shard-id/--parallel`）：闸门前置快速失败、checksum 秒传去重、解析成功率验收线 | `tests/test_import_tool.py`（12 用例）+ 真实 CLI 跑通 |
| 嵌入/精排文本含小节标题（**口径唯一** `chunker.retrieval_text`）：修掉「关键词只在小节标题里就召不回」 | `tests/test_retrieval.py` 的 2 个回归用例 |
| **指标与告警**：快照语义（计数器清零/仪表量保留）、17 条规则（P1×2/P2×10/P3×5）、业务时段规则、按标签分组、P1/P2 落 `ekbs_security_event` | `tests/test_observability.py`（16 用例） |
| 埋点接入真实路径：限流命中、幂等命中、C5 丢弃、问答延迟/首字延迟、Stream 连接状态 | 同上（含采集作业端到端用例） |
| **钉钉令牌客户端**：按应用缓存（第二次不发请求）、TTL 略小于 7200、密钥错为不可重试的配置错误、剩余有效期仪表量 | `tests/test_dingtalk_token.py`（12 用例，respx 离线） |
| **H5 免登**：`authCode` → `topapi/v2/user/getuserinfo`（**查询参数**传 token）→ 映射用户 → 签发 JWT；authCode 一次性防重放；未映射用户 401 | `tests/test_dingtalk_token.py` + `tests/test_api.py` |
| **开发机冒烟脚本**：23 项断言全 PASS（含无凭证 401 与越权红线） | `bash scripts/dev_smoke.sh --start --with-qa --redline` |
| **组织同步**：部门树递归、成员游标分页、手机号脱敏、闭包与 `dept_chain` 重建、离职置 left + 缓存失效 + `PERM_REVOKE_LAG`、对账不写库、失败计数 | `tests/test_dingtalk_sync.py`（21 用例） |
| **同步不得覆盖密级**（否则等于悄悄提权/降权）、**范围外用户不得被标离职** | 同上（两条安全性质断言） |
| **管理台只读接口**（8 条，路径与权限点逐条取自 `05` §8.2）：问答会话与详情、安全事件、访问日志、概览看板、系统健康、分片清单、告警阈值、文档列表与详情 | `tests/test_admin_api.py`（11 用例）+ 真实服务验证 |
| **RBAC**：401（未登录）与 403（权限不足）语义分开、`super_admin` 通配、`/auth/me` 回 `permissions[]` 供前端菜单过滤 | 同上 |
| **文档写接口**：改密级/可见部门**走权限变更服务**（分片迁移已由 HTTP 端到端钉住）、发布需有分块、下线/删除立刻停召回 + 策略版本自增、批量按动作分别鉴权、上传秒传与闸门、索引重建 | `tests/test_admin_write_api.py`（16 用例）+ 真实进程生命周期验证 |
| **实时验证**：上传 → worker 解析 → 检索命中 → 改 L3 迁移分片 → L2 用户拒答 → 提密级后恢复召回 → 补部门隔离不越界 → 删除清理 | curl 实测 |
| **权限数据迁移**：旧库（自造命名）→ 权威命名，授权搬到新点、废弃点清理、按矩阵重放、留痕；幂等与演练均有用例 | `tests/test_permission_migration.py`（5 用例） |
| **解析任务中心**：成功率对照 98% 验收线、失败任务指向日志（不编造 error 字段）、结果预览、重跑/取消的状态机边界 | `tests/test_parse_tasks_api.py`（9 用例）+ curl 实测 |
| **检索调试台**：逐分片稠密召回 / FTS / RRF / rerank / C5 明细、参数覆盖（**并发安全**，不改全局配置）、阈值边界语义、模拟检索落安全事件 | `tests/test_playground_api.py`（12 用例）+ curl 实测 |
| **真实云端全链路**（`00` §11 的 M2/M3）：`qwen3.7-text-embedding-flash@1024` + `qwen3.7-text-rerank` + `qwen-max`，3 文档 / 8 向量入库 1.41s；问答 `embed 250 / recall 228 / rerank 218 / 展开 10 → 检索 706ms`，**含生成的整请求 1.3~2.1s**（SLO 首字 ≤5s、整答 ≤12s）；拒答、越权红线、限流 429 均实测正确 | `config/application.cloud.yaml` + curl 实测 |
| **M3 阈值三带校准**：语料内可答 `0.926~0.991` ／ 域内无答案 `0.108~0.391` ／ 域外 `0.002~0.020`，两段空隙几何中点 `0.601`，据此把 `min_score` 从 `0.30` 改 `0.60` —— 误答 2/6 → **0/6**，误拒 0/4 | `tests/test_retrieval.py` + 上述实测 |
| **MinerU 已实现并实测**（`01` §4 的 M1 / `docs/spec/20` 的 T8 关闭）：独立 venv（`mineru[core]==2.1.10`，2.0 GB，不污染主 venv）；真实 PDF 解析 **15.6 s/页**（模型缓存后；首次含下载 105 s）；块列表→Markdown/页码映射按**实测契约**实现（`page_idx` 0 基 +1、`text_level`→标题、table_body/img_path）；6 条离线契约用例 + 真实解析验证 | `tests/test_parser_mineru.py` + `ekbs/ingest/parser.py::_parse_mineru` |
| **嵌入指纹安全网**（C3/C4，本轮靠它避险）：换嵌入模型后旧分片会让进程**拒绝启动**，逐个分片报出「登记 X ≠ 当前 Y，需按新模型重建」 | `tests/test_retrieval.py::test_switching_embedding_model_refuses_to_load_old_shards` |
| **生产布局安装演练**（`scripts/install_rehearsal.sh`）：真跑 `install.sh → release.sh → preflight/smoke`，只重写前缀。结果 **PASS=47 / FAIL=0** —— 目录树、venv（Python 3.12.14）、库 29 表+WAL+integrity、plist、secrets 600、FTS5、`pip check` 全过；**用生产配置 + 真实云端嵌入**入库（3 分片/8 向量）；`app → releases/<ts>` 符号链接正确；`/readyz → ready`；`/health/deps` **embed/llm/rerank 三项真实云端依赖全可达**；`deploy/smoke.sh` PASS=13（唯一未过项是适配器 `/readyz`，无钉钉凭据时应为 503） | `scripts/install_rehearsal.sh` + 4 条回归测试 |
| **四服务真实 launchd 演练**（`scripts/deploy_rehearsal.sh`）：把四份 plist 逐字搬进本机 `gui/<uid>` 域跑一遍再拆掉 —— 四服务全部 bootstrap 成功、api `/readyz → ready`、四个 label 出现在 `launchctl list`、启动标志进日志、日志落点正确；`kill -TERM` 后 **6734ms 自愈**（`02` TC-ENV-022 要求 ≤15s）；`bootout` 后保持停止 | **PASS=38 FAIL=0** + `tests/test_deployment_contract.py::test_launchd_semantics_are_per_service`（4 条） |
| **分块编辑**：改子块 → 追加新向量 + 新 `faiss_row` + **FTS5 重写**；父块编辑不碰向量；拆分/合并只支持子块（父块会牵连全部子块）；整篇重向量化复用分片重建 | `tests/test_chunks_api.py`（11 用例）+ curl 实测 |
| **跨进程索引可见性**（回归用例）：一个 Runtime 写入 → 另一个 Runtime 无需重启即可见 | `tests/test_retrieval.py::test_shard_index_reloads_after_external_writer` |
| **权限管理页**：角色 CRUD（内置角色禁删）、角色分配（幂等）、改密级与改 ACL **走权限变更服务**（策略版本+缓存+审计一体）、权限模拟落安全事件、变更审计 | `tests/test_perm_admin_api.py`（10 用例）+ curl 实测闭环 |
| **四服务同时运行**实测：api/dingtalk/worker/scheduler 共享同一 SQLite，探针全绿，问答链路正常 | `bash scripts/dev_services.sh start all` + curl |
| **钉钉 OpenAPI 客户端**：鉴权头 `x-acs-dingtalk-access-token`、`msgParam` JSON 字符串与 20 人上限、卡片 `openSpaceId` 构造、流式全量帧与长度上限、`flowControlledStaffIdList` 检查 | `tests/test_dingtalk_client.py`（17 用例） |
| **Stream 消息处理器**：未知用户不调 API、重复 msgId 不重复回答、API 5xx 释放幂等行、内容拦截 ACK 带原因、卡片失败回落普通消息、20001 安全跳过、群聊走群接口 | `tests/test_dingtalk_handler.py`（15 用例） |
| **适配器不谎报就绪**（D3 修复）：只有真的收到回调才 `connected`；缺凭据时 `/readyz` 503 并列出缺哪几个 | `tests/test_deployment_contract.py` + 真实进程验证 |

### ◐ 部分实现 / 待条件

| 项 | 现状 | 缺什么 |
|---|---|---|
| 云端模型真实连通性 | ✅ **已用真实 key 跑通**（embed/rerank/LLM 全部可用、延迟与价格已核实，见 `docs/spec/40`、`60`） | 仅剩**真实语料**下的阈值重校准（demo 语料只有 2 文档，带宽偏乐观） |
| MinerU 解析 | `parse_file` 对 PDF/图片**明确报错**，不假装成功 | `pip install mineru==2.1.10`（2–3 GB 模型）+ 实测 `structure.json` 字段（T8） |
| 钉钉 Stream 接入 | 适配器进程与 `/readyz` 已就绪 | 企业应用凭据（AppKey/AppSecret/RobotCode/CorpId） |
| 钉钉免登 / JWT 签发 | `dev-token` 可用；`dingtalk-login` 返回 501 | 免登流程实现（`04` §3） |
| 审计归档 | 明确跳过并告警 | 事务内 DROP 触发器 → 删除 → 重建（`04` §6.3） |
| 组织同步 | `--fixture` 导入 + 闭包/`dept_chain` 重建可用 | 钉钉通讯录同步 |

---

## 四、实现过程中的发现（与 `方案/` 不一致或文档未覆盖）

> **钉钉那 14 条单独列在 §四之二**（我按官方文档核实过，其中有 5 条会导致功能不正确）。

**这些不改文档就不该改代码**，因此逐条列出，供你拍板：

| # | 发现 | 影响 | 处理 |
|---|---|---|---|
| 1 | `text-embedding-v4` 单请求上限是 **10 条**，`01` §7 写 `batch_size: 20` | 传 20 直接 400 | dev 配置已改 10，**`01` §7 待同步** |
| 2 | **`gte-rerank-v2` 官方公告 2026-05-30 下线**，官方推荐 `qwen3-rerank`（契约完全不同） | 依赖它上线会踩空 | **方案级决策，需你定**（见 §五） |
| 3 | rerank **不能**走 OpenAI 兼容协议（`deploy/requirements.txt` 第 54 行注释有误），且 `relevance_score` 是**请求内相对分**，`min_score=0.30` 作跨请求阈值语义不稳 | 客户端实现与拒答阈值 | 已接 key 实测校准，见第 29 条 |
| 4 | `GROUP BY chunk_id ... MIN(bm25())` 在 SQLite 里**直接报错**（FTS5 辅助函数不允许出现在任何聚合/窗口上下文） | `docs/spec/10` §8 的 SQL 写法不可用 | 已改为「排序取一批 + Python 去重」，语义不变 |
| 5 | 指纹契约用 `,`/`|` 拼接且**无转义**，对含逗号的输入有歧义；靠值域（分片 ID / 数字部门 ID）保证不可达 | 需守住 `dept_code` 正则 | 已加值域校验与用例 |
| 6 | `01` §7 写 `JWT_SECRET=`，`deploy/config/secrets.env.example` 写 `EKBS_JWT_SECRET=` | 按文档填了却读不到 | `Secrets` 两个名字都认 |
| 7 | `UNIQUE(object_type, object_id, subject_type, subject_id, permission)` **不含 effect** → 同一主体对同一对象只能有一个效果，「deny 压 allow」发生在**不同主体之间** | 片内 deny 的建模方式 | 已按此实现并写用例 |
| 8 | `visible_dept_ids` 装的是**部门标识**，与 `dept_code` 是两个命名空间；若先认字面量，纯数字部门 ID（`102` 恰好合法）会被当 dept_code，产出 `kb_102_L2` | 分片命名与路由不一致 | 已改为**先查映射**再回退字面量 |
| 9 | `dept_code` 映射只放在进程内存里时，入库进程与路由进程会解析出**不同分片名** | 跨进程不一致 | 已落到 dev 配置；`O6-1` 定稿后迁权威位置 |
| 10 | `Schema` 没有 `ekbs_document.error` 列，解析失败只能落 `status='failed'` | 故障不可自述 | 原因只进日志；如需落库要改 Schema |
| 11 | **调高文档密级时若只改 `ekbs_document` 而不迁 `ekbs_chunk.shard_id`，低密级用户仍能从旧分片召回它**（我早期实现即如此） | **越权（红线）** | 已由 `ekbs/permission/changes.py` 修掉；`03` §6.4 的「四件事」必须一起做 |
| 12 | `conversation_id` 被当成 `qa_session.session_id` 查询 ⇒ 每轮新建会话、`turn` 恒为 1 | 按会话回溯失效 | 已改为按 `(user_id, conversation_id)` 找回会话 |
| 13 | C5 里 `if perm.visible_kb_ids and ...` 把「可见 kb 集合为空」当成无限制 | kb 级 deny 被绕过 | 已改为严格成员判断（K3 冲突见下） |
| 14 | 我早期的 `archive(dry_run=True)` 仍会执行删除（演练把数据删了） | 演练反而造成数据丢失 | 已修；`dry_run` 现在只统计，用例覆盖 |
| 15 | `01` §13 的备份若用 `cp` 复制 WAL 模式的库，会拿到「主库 + 未合并 WAL」的半截状态 | 备份不可恢复 | 已用 `sqlite3.Connection.backup()`；恢复演练断言 `integrity_check=ok` 且审计行数一致 |
| 16 | `04` §6.4 的**示例代码**把「提示词注入」也判为 `blocked`，但同节的**表格**写的是「降级为纯检索模式（不给生成）」 | 两处语义不一致 | 代码以**表格**为准（`retrieve_only`）：拦截会白白丢掉一次可用的检索 |
| 17 | `schema_sql_path` 若只按 `repo_root/sql` 推，在生产会得到 `/opt/ekbs/sql/001_init.sql` —— 而 `install.sh` 只创建 `$APP_DIR/sql` 与 `$ETC/sql` | **真机上找不到建表脚本** | 已改为多候选解析（`app/sql` → `etc/sql` → `sql/`），并用模拟生产布局的用例锁住 |
| 18 | 关键限定词只出现在**小节标题**时（「第二条 住宿标准」+ 正文「一线城市每晚不超过 600 元…」），若嵌入文本只用正文，查询与正文**没有共享词** → 向量内积为 0 → 文件入库了却查不到 | 稠密召回漏检（影响 Recall@10） | 已把标题路径并入嵌入/精排文本（口径唯一），入库/迁移/重建三处一致；**这是对文档的补充决策，见下** |
| 19 | 钉钉卡片流式的官方描述自相矛盾：全量模式（markdown 强制 `isFull=true`）下每次携带累计全文，「单次 ≤1KB」与「总量 ≤3KB」无法同时成立 | 流式分块策略无权威依据 | 本实现取可自洽解释（增量 ≤1KB、累计 ≤3KB），并**标注需真机核实** |
| 22 | `05` **自身冲突**：§8.1 汇总表写 `/api/v1/docs/documents/{id}/unpublish`，§8.2 全量清单写 `/…/offline` | 前端按哪个写都可能 404 | 以 §8.2（全量清单）为准实现，同时把 `/unpublish` 挂成别名 |
| 28 | **`ekbs-worker` 追加向量后，`ekbs-api` 的内存索引不会自动更新** —— 表现为「刚入库的文档搜不到」（实测踩到）。`06` §2.4 提到 reload 机制，但没规定谁触发 | 新文档检索不到 | 已在 `ShardIndexManager.get()` 按 `(mtime_ns, size)` 检测外部变更并自动重载；这类"两个进程共用一个 SQLite"的失效面在 `04` §7.1.2 里值得补一句 |
| 27 | `00` §2.1 规定父块扩展后进 LLM 上下文的是**父块**，但 `05` 的分块编辑器允许只改子块 —— 此时答案仍引用旧父块内容 | 编辑"看起来生效、答案没变" | 响应里回传 `parent_id` 与 `context_hint`，明确提示需同时编辑父块；**建议 `05` §分块预览页补一句交互说明** |
| 26 | Schema 的 29 张表里**没有检索调试台的数据表**（历史/参数模板/反馈），而 `05` §8.2 列了 `GET /rag/debug/history`、`/rag/param-presets`、`POST /rag/debug/feedback` | 这三类数据无处落 | 历史改用 `ekbs_cache` 环形缓冲（TTL 7 天 / 50 条，**临时排障数据**）；模板与反馈**不做**（新增表须先改 `01` §5 与 `00` §9.1）——**需你确认**是否要加表 |
| 24 | Schema 的状态机只有 `pending\|parsing\|indexing\|published\|failed\|offline`，**没有 cancelled**，而 `05` §8.2 有 `POST /parse/tasks/{id}/cancel` | 取消语义无处落 | 取消 `pending` 映射为 `failed` 并在响应里说明；解析中的任务不可中断（worker 同步解析） |
| 25 | `05` §8.2 列了 `PUT /api/v1/parse/config`（**写入 YAML**），但同文档 §8.1 又说配置走 Git 评审、不暴露写接口 | 由 API 改 YAML 会造成线上配置与 Git 版本漂移 | 本实现**只提供只读** `GET /parse/config`，并在响应里写明 `writable_via_api: false`；**需你确认**是否要按 §8.2 提供写接口 |
| 23 | 我自己的 `scripts/dev_services.sh` 只取第二个参数 → `start api worker` **静默漏掉** worker（实测：worker 根本没起，日志文件都没生成） | 排查方向被误导 | 已改为接受多个服务名，并加回归测试 |
| 21 | 我最初的 `seed_demo` **自造**了权限点命名（`kb:document:upload`、`system:model:config`），与 `05` §8.2 的权威命名（`doc:document:read`、`sys:health:read`、`sys:dashboard:read`…）不一致 | 接口 ↔ 权限点对不上 ⇒ 管理台菜单过滤与后端校验漂移 | 已按 `05` §8.2 逐条对齐（13 个点位），并按 §3.4 的角色矩阵补授权；完整 57 点集属二期 |
| 20 | `04` §4 未规定「本轮没拉到的用户」怎么处理。若拿库里全部 active 用户比对，钉钉**可见范围**之外的人会被整批误判离职（测试里一次就误标了 3 个） | 误把在职员工停权 | 范围收紧为「本次同步到的部门内」；默认关闭，且 docstring 写明「含根部门时开启要格外小心」；推荐走官方离职记录接口 |

| 29 | **`01` §7 的 `rerank.min_score: 0.30` 落在"域内无答案"带里，会把语料里根本没有的问题放进生成**。实测（2026-09-27，alice 视角，demo 语料 2 文档 / 8 向量，`qwen3.7-text-rerank`）分三带：① 语料内可答 `0.926~0.991`；② **域内但语料无答案**（报销/绩效/合同/竞业/股权/财务报表）`0.108~0.391`；③ 完全域外 `0.002~0.020`。拒答只取决于 ①② 的界，`0.30` 在 ② **内部** ⇒「报销标准」(0.391)、「合同审批」(0.321) 都能过阈值 | 阈值直接决定误答/误拒率 | 已改 `min_score: 0.60`（①② 空隙几何中点，两侧各留 1.5 倍余量）并附实测注释；**`01` §7 待同步**。⚠️ demo 语料只有 2 个文档、① 类问题近乎逐字命中（0.93+），**带宽偏乐观，真实语料必须重做三带校准** |
| 30 | **我自己先按"相关/无关"两带做出的 0.10 校准是错的**：把「试用期多久」(0.229)、「财务报销」(0.336)、「加班费」(0.322) 标成了"相关问题"，实际**语料里根本没有这些内容**（模型回「未找到可靠依据」即为证）。按错标注算出的可分区间 `[0.020, 0.229]` 导致阈值调到 0.10，反而把 6/6 个"域内无答案"问题全放了进来 | 校准结论完全反向 | 改为**三带**（可答 / 域内无答案 / 域外）后重校；教训：**标注必须用"语料里到底有没有答案"来定，不能用"问题听起来相不相关"** |
| 31 | **模型自答「未找到可靠依据」时，接口返回 `refused=false` + 5 条引用** —— 前端照 `refused` 渲染，把「没答」显示成「答了并给了出处」。提示词（`00` §2.1）要求模型主动回这句话，但生成分支只在**没有候选**时置 `refused`，**没有识别模型自己的拒答文案** | API 契约自相矛盾，前端展示错 | 已加 `REFUSE_NO_BASIS`：模型拒答或答案被清空时统一 `refused=true / citations=[]`，并记 `no_basis_in_context` 安全事件；只在**开头**比对文案，避免误判正文引用 |
| 32 | **答案缓存键只含 `(权限指纹, 问题, 历史, 生成开关)`，不含任何检索配置** ⇒ 改 `min_score`、换精排/嵌入模型、开关 rerank 之后，**旧答案继续被服务到 TTL 到期**。实测踩到：阈值 0.30→0.60 后同一问题仍在 3ms 内返回旧语义答案，排查时极易误判成"改的代码没生效"。另：命中缓存时 `timings_ms` 缺 `total`（调用方 `timings_ms["total"]` 直接 KeyError），且**跳过会话轮次写入**（多轮追问丢当前轮） | 配置改了行为没变 + 接口形状不稳 | 缓存键加入 `retrieval_cache_revision()`（嵌入/精排/LLM 标识 + 阈值/topN/keep + rrf/父块/改写策略 + `PROMPT_REVISION`）；命中时补齐 timings 形状、标 `answer_cache_hit`、并补写会话轮次 |

| 33 | **嵌入单请求条数上限被硬编码成全局 10**（`EMBED_MAX_BATCH`），换 `qwen3.7-text-embedding-flash`（官方与实测均支持 **20**）后，配置写 `batch_size: 20` 会被**静默夹回 10**，并打出指名 `text-embedding-v4` 的误导性警告 —— 入库吞吐白丢一半且不报错 | 换模型后性能莫名下降 | 已改为 `EMBED_MAX_BATCH_BY_MODEL`（v4/v3=10、flash=20，未列入的模型回退 10），并用用例锁住「12 条应当一次发完」 |
| 34 | **`00` D2 原选 `text-embedding-v4`，本轮按实测改选 `qwen3.7-text-embedding-flash`**（你拍板）。理由：115ms vs 183ms、0.000125 vs 0.0005 元/千 token（**1/4 价**）、1024 维（与本地 `bge-m3` 迁移路径兼容）、RPM 24000 vs 1800、上下文 128K、单请求 20 条 | 影响 `00` D2、`01` §7 与全量重嵌入成本 | 配置已切；**`00` D2 / `01` §7 待同步**。⚠️ 切换必须**重建分片**（指纹变了，进程会拒绝启动，见 §三），故真实语料入库前定下最省事 |
| 35 | **精排分数与嵌入模型无关**：换成 flash 后三带分数与 v4 那次**逐位相同**（0.9458/0.9915/0.9263/0.9445 与 0.1080/0.3905/…）。因为 cross-encoder 只看（问题, 文档原文），不看向量 | 换嵌入模型是否要重校阈值 | `min_score` 可跨嵌入模型沿用；但真实语料候选集不同，仍需按 §三 方法重做三带校准 |

| 36 | **`launchctl stop` 在 macOS 26 上是空操作**：实测 `launchctl print` 显示 pid 不变、`runs` 不变、`last exit code = (never exited)` —— 进程根本没被终止。后果是 `02` 里以 `launchctl stop` 注入故障的用例（**TC-ENV-023 / F02 / F04 / TC-DT-046**）"通过"了却什么都没测。另：`04 §4` 说「进程退出码 0 且不被自动重启」，但 uvicorn 收 SIGTERM 的退出码实测是 **143（非 0）**，即便 stop 生效也会被 `SuccessfulExit=false` 拉起 | **验收用例失效**（假通过） | `scripts/deploy_rehearsal.sh` 用 `kill -TERM <pid>` 注入故障并实测自愈；**`02` 的注入命令需改**（建议 `kill -TERM` 或 `launchctl kickstart -k`），停机改用 `launchctl bootout`（已实测有效） |
| 37 | **`01 §8.2` 表格把 dingtalk 入口写成 `ekbs.dingtalk.stream`**，但仓库里没有这个模块（实际是 `ekbs.stream`）；`04 §51`（「代码包 `ekbs/stream`」）与 `02 §581`（`pkill -f "python -m ekbs.stream"`）都写 `ekbs.stream` | 按 §8.2 表格手敲命令会 `No module named` | plist（§8.1 模板，权威）用的是 `ekbs.stream`，契约测试按此锁定；**§8.2 表格待改** |
| 38 | **`04 §4` 的信号处理表说 dingtalk 的 `KeepAlive` 是 `SuccessfulExit=false`，与实际 plist 相反** —— 实际是 `KeepAlive: true`，且 `01 §8.2` 明确写「**必须 true**」（Stream 长连接必须重连） | `02` TC-ENV-023 期望「不被自动重启」在现 plist 下**不可达** | 已按「按服务区分」固化进契约测试（api/worker/scheduler=false，dingtalk=true）；**`04 §4` 或 `02` TC-ENV-023 需改一处** |

| 39 | **`install.sh` 与 `release.sh` 组合出的符号链接陷阱**（真机演练实测）：`install.sh §1` 把 `/opt/ekbs/app` 建成**真实目录**，`release.sh` 再 `ln -sfn "$NEW" /opt/ekbs/app` 时**不会替换目录**，而是把链接建到目录**里面**（`app/<时间戳> -> releases/<时间戳>`）—— `04 §7.6` 的 `app -> releases/<ts>` 布局**静默失效**，每次发布/回滚再套一层，`WorkingDirectory` 也不是代码目录（服务靠 `pip install -e` 才照常起来，因此不报错） | 版本切换/回滚布局失真，且不报错 | 已修 `release.sh`（先收掉真实目录并保留其中的 `sql/`）、同步 `04 §7.6` 示例代码，并加**回归测试**（断言「先判断后建链接」的顺序，含文档示例） |
| 40 | **launchd 起的进程读不到 `~/Documents` 下的文件**（macOS TCC）：实测 `zsh:source:1: operation not permitted`，同一文件在 `$HOME` 下正常；表现为 **`ConfigError: 缺少必需密钥：DASHSCOPE_API_KEY`** 这种极具误导性的报错 | 部署位置若落在 `~/Documents`/`~/Desktop`/`~/Downloads`，服务会因读不到密钥/文件而起不来，且报错指向错误方向 | 生产路径 `/opt/ekbs` + `~/.ekbs` 不受影响；**两个演练脚本的根目录已移出仓库**（改到 `$HOME/.ekbs-rehearsal-*`）并加测试锁住；运维须知：**不要把部署目录放在受 TCC 保护的目录下** |
| 41 | **`pip install mineru==2.1.10` 装出来的 CLI 直接崩**：`NameError: name 'torch' is not defined`（`mineru/cli/client.py` 的 `get_device()` 里调 `torch.cuda.is_available()`，而基础包不拉 torch）。`01 §4` 与 `deploy/requirements.txt` 给的都只是这个基础包 | 按文档装完仍**无法解析任何 PDF**，且报错是 `NameError` 而非「缺依赖」 | 已确认 `mineru` 声明的 extras（`core`/`pipeline`/`vlm`/`all`）；**`01 §4` 与 `requirements.txt` 需写成 `mineru[core]==2.1.10`**。另注：文档 pin `2.1.10`，当前 PyPI 最新为 **4.0.7**，是否升版待定 |
| 42 | **Python 版本冲突以实证收口**：`install.sh §3` 实际选到 **Python 3.12.14**（候选顺序 `python3.12 → python3.11 → …`），且 FTS5 自检通过；`01 §3` 写 3.11、`02` 写 3.12 | 文档不一致会让人装错解释器 | 以 `install.sh` 的候选顺序为准（3.12），**`01 §3` 建议同步**；本仓库开发 venv 仍是 3.11（两者都被支持） |

| 43b | **生产 venv 是 Python 3.12，而 MinerU 我只在 3.11 下实测过**：已用 3.12 做依赖解析验证 —— `pip install --dry-run 'mineru[core]==2.1.10'` 在 3.12 下**可解析**（会装 torch 2.14 / torchvision / transformers / doclayout_yolo / ultralytics 等）。**但 3.12 下的真实解析尚未跑过**（要重下模型），而 `01 §3` 恰好注明「MinerU 与依赖对 3.11 兼容性最好」—— 真机安装后必须补一次 3.12 下的 PDF 解析实测 | 生产 venv 的解析能力未闭环 | 见 §六 第 3 步 |
| 43 | **MinerU 2.1.10 的产物里根本没有 `structure.json`**（`01` §1/§4、`docs/spec/20`、`06` §4.3 都按它写）：实测产物是 `<out>/<文件名>/auto/` 下的 `<stem>_content_list.json`（块列表）+ `_middle.json` + `_model.json` + `.md` + `images/`。另有两处保真度问题：**文档标题行会被整块丢弃**（测试 PDF 的「员工年假管理制度」在 `.md` 与块列表里都不见），**`第X条` 中文条文不被识别成 heading**（`text_level` 缺失） | 页码/图片/标题路径的取法全部按旧假设写会取不到；标题路径为空会影响 `06` §3 的分块 | 已按实测实现 `_parse_mineru`（页码取 `page_idx+1`、图片取 `img_path`、异常一律明确报错），并同步 `01 §4` 产物契约 + 关闭 `docs/spec/20` 的 T8；**`06` §4.3 的表格取法待一并按 `table_body` 调整** |
| 45 | **`$var` 紧跟非 ASCII 字符会让 macOS 自带 bash 3.2 解析错变量名**（我自己踩了三次：`$svc：` 被当成变量 `svc：`，报 `unbound variable`，行号还指向别处）。真机部署默认就是 bash 3.2，必须守住 | 脚本在真机静默失效/报怪错 | 已全部加花括号（`${svc}：`），并加**仓库级 lint 测试**扫描 `scripts/` 与 `deploy/scripts/` 下所有 shell 脚本；该测试当场又抓出 `dev_smoke.sh` 里两处既有隐患并修掉 |
| 44 | **`mineru` 装在 venv 里时，`shutil.which("mineru")` 在生产会找不到**：launchd 的 PATH 只有 `/usr/bin:/bin:/usr/sbin:/sbin`，而可执行文件在 `/opt/ekbs/venv/bin/`。把 MinerU 放独立 venv（推荐）后更明显 | 会误报「MinerU 未安装」，且只在 launchd 场景复现（手工 `source activate` 时正常） | 已把查找顺序改为**「与解释器同 venv」优先**，再回退 PATH；`EKBS_MINERU_BIN` 可显式覆盖。已加回归用例（模拟 launchd 窄 PATH） |


### 四之二、钉钉：`方案/04` 与官方文档不符之处（14 条，其中 5 条会导致功能不正确）

核实方法与逐字段依据见 [`docs/spec/50-dingtalk.md`](docs/spec/50-dingtalk.md)（1105 行；
`open.dingtalk.com/document/.../<slug>.md` 可取官方全文，SDK 契约由解包 `dingtalk-stream` wheel 源码核实）。
**以下 5 条建议优先修文档**（我未改 `方案/`）：

| # | 位置 | 问题 | 影响 |
|---|---|---|---|
| D1 | `04` §5.7 | 卡片流式用**增量**方式更新 markdown 变量（`append=True`）；官方明确要求 markdown 变量**必须 `isFull=true`**，官方示例全程 `append=False` | 内容错乱或直接报错 |
| D2 | `04` §5.7 | `put_card_data(..., updateCardDataByKey=True)` 位置错误 —— 该字段是 `cardUpdateOptions` 的子字段，SDK 把 kwargs 平铺到顶层 ⇒ 传了个无效字段，实际按「覆盖更新」执行 | **会清掉卡片已有内容** |
| D3 | `04` §5.3 / §7.3 | `StreamRunner` 在建立连接**之前**就 `set_connected(True)`，而 SDK 连接失败只 sleep 重试、永不抛异常 | `/readyz` 长期**谎报就绪** |
| D4 | `04` §5.3 / §7.3 | `StreamState.reconnect_count` 没有任何自增点 | `/readyz` 与 `/health/deps` 的该指标恒为 0（假指标） |
| D5 | `04` §10.1 | 把 `topapi/v2/user/getuserinfo` 标成「旧版兼容模式，默认关闭」——它其实是当前 H5 免登的**官方主链路** | 免登可能被误判为不可用 |

另有 9 条（路径/事件名/字段口径不实：`contact/v3/departments/lists` 未核实到、
事件名应为 `org_dept_create/modify/remove`、`isInAtList==false` 也能收群消息无依据、
`config.streamingMode` 非官方卡片协议字段等），以及官方与方案一致的 6 条，
详见 `docs/spec/50-dingtalk.md` 的比对章节。**这些都在等你决定是否改 `方案/04`。**

三份实现规格文件里还有更细的待确认清单：
[`docs/spec/10-retrieval-permission.md`](docs/spec/10-retrieval-permission.md)（K1–K17）、
[`docs/spec/20-ingest-chunking.md`](docs/spec/20-ingest-chunking.md)（T1–T20）、
[`docs/spec/30-api.md`](docs/spec/30-api.md)（C1–C18）、
[`docs/spec/40-cloud-models.md`](docs/spec/40-cloud-models.md)（云端契约与 8 条未核实项）。

---

## 五、需要你确认（按紧急度）

| # | 事项 | 为什么必须先定 |
|---|---|---|
| 0 | **嵌入文本是否包含小节标题路径**（本轮的补充决策，`06` §3 未规定） | 影响向量语义与 Recall@10；若定稿为「不含」，需回退 `chunker.retrieval_text` 并全量重嵌入 | 
| 1 | ~~embedding + rerank 模型选型~~ ✅ **已定**：`qwen3.7-text-embedding-flash@1024` + `qwen3.7-text-rerank`（LLM 维持 `qwen-max`），配置已切、分片已按新模型重建 | 仍需同步 `00` D2/D3 与 `01` §7 的文字（见 §四 第 34 条） |
| 2 | ~~云端 key~~ ✅ **已提供并跑通**（`DASHSCOPE_API_KEY` 走环境变量） | — |
| 3 | **合规书面确认**（`00` §7）：入库即全量语料出网 | 开 `corpus_egress_ack=true` 才允许真实语料入库；当前 false |
| 4 | **O6-1 / K1**：`dept_code` 与部门 ID 的映射存在哪 | 现在由配置承载（不新增 Schema），需要定稿 |
| 5 | **路径前缀**：`/api/v1` 还是 `/v1`（`01` §14 与 `04` §2.5 冲突） | 现在两个都挂；定稿后删一个 |
| 6 | **是否按 §四之二的 D1~D5 修订 `方案/04`** | 卡片流式写法、`/readyz` 谎报、免登主链路标注——不修会在接入钉钉时踩到 |
| 7 | ~~Python 版本~~ ✅ **已由实证收口**：`install.sh` 实际选 3.12.14（候选顺序优先 3.12），FTS5 自检通过；开发 venv 用 3.11（两者都被支持）。**`01` §3 的文字待同步** | — |
| 8 | O7：目标 Mac mini 磁盘 ≥256 GB | `00` §8 列为**唯一阻塞开工项** |

---

## 六、下一步（建议顺序）

1. **首批入库**：`python -m ekbs.tools.import --dir <目录> --kb-code <code> --level 2 --dept-ids <ids> [--shard-id kb_hr_L2]`（真实语料需先开合规闸，见 §五）。
2. ~~接真实云端~~ ✅ **已完成**：`config/application.cloud.yaml` 已切到 `dashscope`（`text-embedding-v4@1024`
   / `qwen3.7-text-rerank` / `qwen-max`，key 走 `DASHSCOPE_API_KEY` 环境变量），M2/M3 实测已跑，
   `min_score` 已按三带校准到 `0.60`（见 §三、§四 第 29~32 条）。
   **仍需你做**：从候选清单里定 embedding 与 rerank 模型（§五 第 1 项）→ 我写进配置并同步 `00` D2/D3、`01` §7。
   真实语料入库后，必须用同一套三带方法**重做阈值校准**（demo 语料只有 2 个文档，带宽偏乐观）。
3. ~~MinerU~~ ✅ **已完成**：装 `mineru[core]==2.1.10`（独立 venv）+ `MINERU_MODEL_SOURCE=modelscope`
   → 真实 PDF 实测 M1（15.6 s/页）→ 按实测产物实现 `ekbs/ingest/parser.py::_parse_mineru`（T8 关闭）。
   **仍需你/真机补**：在生产 venv 的 **Python 3.12** 下跑一次真实解析（我只在 3.11 下实测过，
   3.12 仅验证了依赖可解析），并把 `EKBS_MINERU_BIN` 写进部署配置。
4. **真实语料试点**：合规确认到位后开 `corpus_egress_ack`，首批 200 份入库（`01` §12）。
5. **本机 launchd 演练（已可跑，建议每次改部署件后都跑）**：`bash scripts/deploy_rehearsal.sh`
   —— 用真实 launchd 拉起四服务、探活、测自愈、再拆掉（当前 **PASS=38 / FAIL=0**，api 自愈 6.7s）。
   ⚠️ 该脚本需要**未被沙箱限制的 shell**：受限环境下 `launchctl bootstrap` 会以
   `Bootstrap failed: 5: Input/output error` 失败（本机实测；同一命令在放开沙箱后成功）。
6. **目标机部署**：`deploy/scripts/install.sh` → `preflight.sh` → `smoke.sh`（需 Mac mini 与 `/opt`、`/data` 写权限）。
7. **钉钉接入**：拿到企业应用凭据后实现 Stream 客户端与免登（`04` §3）。

---

## 七、开发机与生产机的差异（务必知道）

| 项 | 开发机（本仓库） | 生产（Mac mini / `01`） |
|---|---|---|
| 代码位置 | 本目录 | `/opt/ekbs/app`（release 符号链接） |
| 虚拟环境 | `.venv/` | `/opt/ekbs/venv` |
| 数据目录 | `var/{raw,parse,index,backup,tmp}` | `/data/ekbs/…` |
| 配置 | `config/application.dev.yaml` | `/opt/ekbs/etc/application.yaml` |
| 密钥 | 环境变量（`EKBS_INTERNAL_TOKEN=…`） | `~/.ekbs/secrets.env`（600，launchd 注入） |
| 进程 | 手动 `uvicorn` / `python -m ekbs.*` | 4 个 launchd 服务 |

`config/application.dev.yaml` 的**检索参数与生产逐字一致**，改的只有目录与 provider（mock ↔ dashscope），
因此「开发机上跑通的链路 = 生产链路」。
