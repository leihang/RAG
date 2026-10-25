# EKBS 应用仓库（开发机骨架）

> **当前状态：一期骨架可运行 + 云端代码路径已就绪**
> 入库 → 分片 → 混合检索 → alpha 融合 → 精排 → C5 闸门 → 生成 → 引用校验 整条链已跑通；
> 离线（mock 模型）与**云端代码路径**（respx 按真实契约模拟百炼三端点）都有端到端用例；
> `983` 个 pytest 用例全绿（981 passed / 2 skipped）。**云端三模型已选定并用真实 key 跑通全链路**（`qwen3.7-text-embedding-flash@1024` + `qwen3.7-text-rerank` + `qwen-max`），见 §三。
>
> 权威设计文档在 [`方案/`](方案/)（8 份）。本文件只讲**代码怎么跑、已做到哪、还差什么**；
> 与 `方案/` 冲突时以 `方案/` 为准，并请把本文件与代码一起改掉。

---

## 一、五分钟跑起来

```bash
cd /Users/lh/Documents/工作学习/AI项目/RAG
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt   # 首次

# 管理台前端（首次；改前端后重跑 build 即生效，服务不用重启）
cd ekbs-admin-web && npm install && npm run build && cd ..

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
.venv/bin/python -m pytest tests/ -q          # 983 collected（981 passed, 2 skipped）；不出网、不花钱
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
| `ekbs/retrieval/rrf.py` | RRF 融合（k=60）+ **α 加权融合**（`fusion: rrf\|alpha`，2026-10-02 实测改用 alpha/0.85） | `00` §2.1 |
| `ekbs/retrieval/numeric.py` | **取数模式**（课程 19 口径）：类型锁死 + 不许算/不许猜 + **抄写校验** | `docs/eval/`、README §四 第 77 条 |
| `ekbs/retrieval/pipeline.py` | 链路编排：召回 → RRF → rerank → C5 → 父块 → 引用校验 | `00` §2.1/§2.2 |
| `ekbs/cache/store.py` | L1 LRU + L2 `ekbs_cache`（替代 Redis） | `01` §5.5、`03` §4.4 |
| `ekbs/ingest/` | 解析（可插拔）/ 四层分块 / 入库编排 | `06` §3、`docs/spec/20` |
| `ekbs/ingest/parser.py` | **解析引擎单一来源** `SUFFIX_ENGINES`（63 种后缀）：MinerU（PDF/图片）、`textutil`（旧版 `.doc`）、`python-pptx`、`xlrd`、代码后缀→文本 | `06` §3.1 |
| `ekbs/ingest/code_chunker.py` | **代码感知分块**：Python AST 符号切分 / 51 种后缀启发式 / 行窗口兜底 | `docs/代码知识库-方案C.md` |
| `ekbs/ingest/retrievable.py` | **「谁进向量/进 FTS/可被检索」的唯一口径**（叶子块规则，6 个消费方共用） | `06` §4.11、`03` §5.4 |
| `ekbs/ingest/dir_import.py` | **服务器目录导入**：路径限定 raw 之内（防穿越/软链接逃逸）+ 扫描分类 + 登记 `pending`（解析交 worker） | `01` §12 |
| `ekbs/audit.py` | 问答审计三表（append-only） | `01` §5.4/§5.6 |
| `ekbs/api/` | FastAPI 应用、鉴权、健康端点、问答、图片回源、**管理台只读接口** | `04` §7.3、`05` §8.1/§8.2 |
| `ekbs/authz.py` | RBAC 判定（角色 → 权限点；``super_admin`` 通配） | `01` §5.3、`05` §3.4 |
| `ekbs/api/docs.py` | **文档写接口**：登记/编辑（含分片迁移）/发布/下线/删除/批量/上传（秒传）/索引重建 | `05` §8.2 |
| `ekbs/api/parse_tasks.py` | **解析任务中心**：任务列表（含成功率）、详情、结果预览、重跑、取消 | `05` §8.2 |
| `ekbs/api/imports.py` | **导入接口**：目录浏览 / 扫描预览 / 执行登记 / 队列反馈 / 元信息（两张白名单） | `05` §8.2 |
| `ekbs/api/admin_ui.py` | **常规管理台 `/admin`**（免构建单页）：概览/文档/**导入**/权限/分块/解析/调试/系统/审计 | `05` §8.1 |
| `ekbs-admin-web/` | **管理台前端工程**（方案 `05` §1.1 技术栈）：React 18 + TS5 + Vite5 + Ant Design Pro 5 + TanStack Query + Zustand + React Router 6 + ECharts；`vite build` → `dist/` | `05` §1.1/§6.1 |
| `ekbs/api/admin_web.py` | 托管 `dist/`：SPA 回落 + 哈希资源长缓存 + `index.html` 禁缓存 + 穿越防护（方案 §6.9 的 Caddy 过渡期实现） | `05` §6.9 |
| `ekbs/api/playground.py` | **检索调试台**：改写/逐环节召回/完整链路/权限模拟/路由解释 | `05` §8.2 |
| `ekbs/api/chunks.py` | **分块预览与编辑**：父子块树/详情/导出/编辑（换向量+重写 FTS）/拆分/合并/重嵌入 | `05` §8.2 |
| `ekbs/api/perm_admin.py` | **权限管理页**：角色 CRUD/分配、用户密级与状态、部门视图、ACL 增删、权限模拟、变更审计 | `05` §8.2 |
| `ekbs/rbac_catalog.py` | 权限点目录 + 角色授权矩阵（**单一来源**，seed 与迁移共用） | `05` §3.4/§8.2 |
| `ekbs/tools/migrate_permissions.py` | **权限数据迁移**：补点 / 合并历史命名 / 按矩阵重放授权 / 留痕 | — |
| `ekbs/main.py` | uvicorn 入口 `ekbs.main:app` | `01` §8.1 |
| `ekbs/worker.py` | `python -m ekbs.worker` 解析 worker | `00` D13 |
| `ekbs/scheduler.py` | `python -m ekbs.scheduler` APScheduler 单实例 | `00` D13、`04` §7.1.4 |
| `ekbs/stream.py` | `python -m ekbs.stream` 钉钉适配器（`/livez` `/readyz`） | `04` §7.3 |
| `ekbs/tools/` | `init_db` / `new_shard` / `rebuild_shard` / `sync_org` / `seed_demo` / **`backup`（含恢复演练）** / **`import`（批量入库）** / **`eval_retrieval`（金标 Hit@k/MRR）** / **`eval_switch_sweep`（检索开关 A/B + 负例分布 + `--calibrate` 阈值校准）** | `01` §5.7/§6.3/§12/§13/§16、`04` §4、`docs/eval/` |
| `ekbs/tools/gc.py` | **删除后的残留清理**：raw 孤儿文件 / parse 孤儿产物 / 孤儿向量盘点（默认只报告） | `00` §3.1 |
| `scripts/dev_services.sh` | 开发机四服务管理（与 launchd 同一套入口，无 root） | `01` §8 |
| `scripts/dev_smoke.sh` | **开发机冒烟**（与生产 `smoke.sh` 同口径）：Schema/分片/四服务/健康端点/两条红线 | `01` §14、`04` §7.3 |
| `scripts/acceptance.sh` | **上线接收检查**：把 DoD 逐项 + 迁移闸门 + 配置断言 + 契约测试 + 冒烟 + 备份演练汇总成**一次命令的结论**（READY / NOT-READY + 待外部条件清单） | `01` §0.3 |
| `docs/spec/` | 四份**实现规格**（从 `方案/` 提炼，含待确认清单） | — |

`sql/001_init.sql` 是 `deploy/sql/001_init.sql` 的逐字副本（部署时它落在 `$APP_DIR/sql/`）。

### 支持的文件类型（**两张清单，宽度不同**）

权威来源只有一处：`ekbs/ingest/parser.py::SUFFIX_ENGINES`（**63 种**）。
但收文件的两条路宽度不同，复核时最容易张冠李戴：

| 入口 | 生效清单 | 宽度 |
|---|---|---|
| **批量导入 CLI**（`ekbs.tools.import`） | `SUFFIX_ENGINES` 全量 | **63 种** |
| **上传接口 HTTP**（`/api/v1/docs/upload/init`） | 配置 `limits.file_ext_whitelist` ∩ `SUFFIX_ENGINES` | **11 种** |

| 引擎 | 数量 | 后缀 |
|---|---|---|
| `mineru` | 4 | `.pdf` `.png` `.jpg` `.jpeg` |
| `docx` / `doc` | 2 | `.docx`；`.doc`（macOS 自带 `textutil`，**无需 LibreOffice**） |
| `xlsx` / `xls` | 2 | `.xlsx`（openpyxl）；`.xls`（`xlrd`，只读旧版） |
| `pptx` | 1 | `.pptx`（`python-pptx`，逐页 Slide） |
| `text` | 54 | `.md` `.markdown` `.txt` + **51 种代码后缀**（`.py .ts .go .java .sql .sh .rs .cpp …`） |

* **上传接口可收的 11 种**：`pdf docx doc pptx xlsx xls txt md png jpg jpeg`
* ⚠️ **代码文件目前只能走 CLI 批量导入** —— 从上传接口传 `.py` 等会被 **422**（配置白名单不含代码后缀）。
  若要在管理台上传代码，改**配置**即可（取交集那步会自动放行），不必动解析器。
* ⚠️ **明确不支持**：`.csv` `.json` `.rst` `.ini` `.toml`（不在 63 种内，CLI 按"跳过"计数）；
  `.ppt` `.html`（配置里有、解析器没实现 → 上传时 **422 并说明原因**，不会静默收下变乱码）。
* 以上清单已用 `tests/test_ingest_extensions.py` 钉住（含各引擎数量、51 种代码后缀、上传口径宽度）。

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
| 入库编排：四层分块、分片归属、批量嵌入、FTS 写入、多部门 shard ACL 授权 | `seed_demo` 后等常驻 worker 轮询至 `published`（**勿**与常驻并存再跑 `worker --once`，会双开 MinerU 耗尽内存；抢不到 `data_dir/worker.lock` 会 exit(1)。强制联调：先 `bash scripts/dev_services.sh stop worker` 再 `--once`，跑完再启） |
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
| **图片不丢 + 可检索**（需求②）：文档内图片落盘到 `var/parse/<sha\|doc_id>/images/`（不再被 `tempfile` 回收）、生成 `figure` 块并**入向量 + 入 FTS**（检索文本 = 题注 + OCR）、`/assets/figure/<chunk_id>` 可回源且复检 ACL、无题注无 OCR 的图**跳过不落块**。实测：扫描合同 3 张图入库，OCR 文字可被关键词召回，`/assets/figure/50` 返回真实 JPEG | `tests/test_ingest_code_and_figures.py`（4 用例）+ curl 实测 |
| **旧版 Office 格式兼容**（需求①）：`.doc` 走 macOS 自带 `textutil` 转 docx（无需 LibreOffice）、`.pptx` 走 `python-pptx` 逐页、`.xls` 走 `xlrd`（`.xlsx` 仍走 openpyxl，数值 `1` 不显示成 `1.0`）。实测：`.doc` 出 341 字正确中文；38 页 `.pptx` 0.10s / 3151 字 / 峰值 302MB；`.xls` 出 markdown 表格 | 解析分支 + 上传接口实测（`.doc/.pptx/.xls/.pdf` → 200） |
| **代码知识库（方案 C）**（需求③）：代码走 `parse_file` → **符号级分块**（Python AST：`路径 > Class.method`；51 种后缀启发式；行窗口兜底）→ 与制度文档**同一套分片/FTS/权限/检索**。实测真实仓 `ekbs/ingest/` 6 文件：三方对账 138=138=138、`diff_ratio=0.0`；用只存在于代码里的符号名提问，代码块排 **rank 1/2** | `tests/test_code_chunker.py`（27 用例）+ `tests/test_ingest_code_and_figures.py` + 真实 CLI 导入，见 `docs/代码知识库-方案C.md` |
| **「可检索」口径单一权威**（跨层回归）：规则曾是散落 8 处的 `chunk_type='child'` —— 代码符号块与图片块因此**入不了向量，且重建分片会再丢一次**。现统一为「叶子块」判据（`ekbs/ingest/retrievable.py`），入库/重建/批量导入/换级重嵌/三方对账/发布闸门 6 处共用；两道 lint 用例防止它再散回去 | `tests/test_ingest_code_and_figures.py` 的 3 条不变式用例 |
| **扩展名白名单单一来源**：CLI 默认白名单与配置白名单曾是两张皮（`.pdf` 只在配置里 → `--ext .pdf` 才看得见 PDF）。现统一取自 `parser.SUFFIX_ENGINES`（63 种）；上传接口取「配置允许 ∩ 解析器已实现」，`.ppt/.html` 这类返回 **422 并说明原因**而非静默丢弃 | `tests/test_ingest_extensions.py` + 上传接口实测 |
| **管理台「导入」页**（`/admin` → 导入）：**服务器目录导入** —— 逐层浏览 `data_dir/raw`（**支持中文多级子目录**）→ 扫描预览（可导入 / 重复跳过 / 超限 / 闸门原因）→ 一键登记；解析交 worker，进度复用「解析任务」页。三个安全性质用用例钉住：**路径只能落在 raw 之内**（`..`、绝对路径、软链接逃逸三种都拒）、**闸门不能被界面绕过**（合规闸/L3 闸与 CLI 同一判据，且把原因显示出来而不是抛 500）、**只登记不解析**（避免网页请求挂几十分钟） | `tests/test_import_api.py`（20 用例）+ 真实 HTTP 全流程实测 |
| **管理台上传页签**：浏览器选文件 → 分片（5 MB）→ 秒传（浏览器算 SHA-256）→ 进度条；明确区分「目录导入 63 种」与「上传 11 种」，并把配置里允许但未实现的 `.ppt/.html` 提前标出来，不让用户挑完文件才吃 422 | `tests/test_admin_ui_page.py` + 上传接口实测 |
| **管理台页面自身的回归**：页面是内嵌在 Python 字符串里的单文件 JS，JS 一坏**接口全绿、页面全白**。用 `node --check` 真解析一遍（无 node 则跳过），并锁住「菜单 ↔ 视图一一对应」「每个菜单都声明权限点」 | `tests/test_admin_ui_page.py`（6 用例） |
| **权限管理页**：角色 CRUD（内置角色禁删）、角色分配（幂等）、改密级与改 ACL **走权限变更服务**（策略版本+缓存+审计一体）、权限模拟落安全事件、变更审计 | `tests/test_perm_admin_api.py`（10 用例）+ curl 实测闭环 |
| **四服务同时运行**实测：api/dingtalk/worker/scheduler 共享同一 SQLite，探针全绿，问答链路正常 | `bash scripts/dev_services.sh start all` + curl |
| **钉钉 OpenAPI 客户端**：鉴权头 `x-acs-dingtalk-access-token`、`msgParam` JSON 字符串与 20 人上限、卡片 `openSpaceId` 构造、流式全量帧与长度上限、`flowControlledStaffIdList` 检查 | `tests/test_dingtalk_client.py`（17 用例） |
| **Stream 消息处理器**：未知用户不调 API、重复 msgId 不重复回答、API 5xx 释放幂等行、内容拦截 ACK 带原因、卡片失败回落普通消息、20001 安全跳过、群聊走群接口 | `tests/test_dingtalk_handler.py`（15 用例） |
| **适配器不谎报就绪**（D3 修复）：只有真的收到回调才 `connected`；缺凭据时 `/readyz` 503 并列出缺哪几个 | `tests/test_deployment_contract.py` + 真实进程验证 |

### ◐ 部分实现 / 待条件

| 项 | 现状 | 缺什么 |
|---|---|---|
| 云端模型真实连通性 | ✅ **已用真实 key 跑通**（embed/rerank/LLM 全部可用、延迟与价格已核实，见 `docs/spec/40`、`60`） | 仅剩**真实语料**下的阈值重校准（demo 语料只有 2 文档，带宽偏乐观） |
| MinerU 解析 | ✅ **已实现并实测**：独立 venv（`var/mineru-venv`）+ 1.9 GB 模型（`var/mineru-models`，`_mineru_env()` 自动指向）；2026-09-29 实测 PDF 端到端通（1 页中文 PDF，30 s 含模型加载，正文抽取正确） | 生产机把模型放非 TCC 保护路径（如 `/data/ekbs/mineru-models`）并显式 `MODELSCOPE_CACHE`：开发机的 `~/Downloads` 受 macOS 隐私保护，launchd 进程读不到 |
| 图片 OCR | 默认开启（`EKBS_FIGURE_OCR=1`），让"只有图形、没有题注"的图也能被检索 | 真实语料下张数与最小体积阈值可能要调（`EKBS_FIGURE_OCR_MAX` / `_MIN_KB`） |
| 代码知识库 | 方案 C 已实现并验证（符号切分 + 同一套权限与检索），见 `docs/代码知识库-方案C.md` | **不支持**符号跳转与跨文件调用图；管理台分块编辑目前只支持 `child`，代码符号块需改源文件后重导 |
| 音频 / 视频 | **不支持**（不在扩展名白名单） | 需先转写。官方 ASR `qwen3-asr-flash` 约 **0.792 元/小时**；本机 `mlx_whisper` 已装但未下模型 |
| **删除回收站** | 当前是**硬删除**：分块 / FTS 行 / **FAISS 向量** / 文档行 + **磁盘原件与解析产物**一次清完（策略版本 +1；索引已换 `IndexIDMap2`，删得掉、不留孤儿）。删空后分片 `.faiss` 留一个 0 向量的空壳（它是注册在库里的基础设施对象，下次复用）。接口可带 `purge_assets=false` 只清库与向量 | 方案 P5 要求「软删除 + 回收站 30 天」；需加 `deleted_at` + 列表过滤 + 恢复接口 + 30 天清理作业 |
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
| 46 | **「可检索」的口径被写死成 `chunk_type='child'` 散落在 8 个文件里**。本轮加代码分块与图片块时暴露：代码的符号块是 `parent`（顶层 `class`/`def` 没有子块）、图片块是 `figure`，**两者都不在 `'child'` 里** ⇒ 入库时进不了向量与 FTS（代码库"建了但搜不到"）；更隐蔽的是 `rebuild_shard` / 权限换级重嵌 / 三方对账 / 发布闸门也各写了一遍，所以**重建分片一次还会把已入的向量再丢一次**，而库里看不出异常（对账数自己也按同一错误口径算，diff_ratio 反而=0） | 代码与图片静默不可检索；重建即丢数据；对账指标自证清白 | 统一为「**叶子块才是检索单元**」并收敛到 `ekbs/ingest/retrievable.py` 一处，6 个消费方全部改引用；加两道 lint 用例（不允许再出现 `chunk_type='child'` 过滤、6 个模块必须引用同一口径）。教训：**跨层的不变量不能靠"每处都记得写对"** |
| 47 | **CLI 默认扩展名白名单与配置白名单是两张皮**：配置里 `limits.file_ext_whitelist` 含 `.pdf`，而 `import.py` 的 `DEFAULT_EXTENSIONS` 不含 —— 于是 `发现文件 0`，看起来像"目录里没文件"。上传接口则只按配置判白名单，`.ppt`/`.html` 这类**配置允许但解析器没实现**的格式会被收下来再静默失败 | PDF 默认导不进去；用户以为目录错了 | 白名单单一来源化（取 `parser.SUFFIX_ENGINES`，63 种）；上传接口改为「配置允许 ∩ 解析器已实现」，未实现的返回 **422 并说明原因**；加 `tests/test_ingest_extensions.py` 锁住 |
| 66 | **`00` §11 的 M12 / `06` §4.11.2 的 O6-7（MinerU 对纯图片输入的支持程度）——已实测关闭，答案是「不支持」**：装好模型后拿真实图片跑 `_ocr_figure`，MinerU 对独立 PNG/JPG 走的是版面分析流水线，它把整张图判定成一个 figure 区域，产出的 markdown 就是**这张图自己的引用**（`img-02561a4c.png 530KB 32.1s → "![](images/2b6202af….jpg)"`）——每张图 ~33 秒、换回一个链接、一个字都没有。按方案写好的兜底：**回落 PaddleOCR PP-OCRv4**（`06` §4.11.2 第 3 步） | 文档内嵌图片的「图内 OCR 文字进索引」目前拿不到；若沿用 MinerU 每张图要空跑 33 秒 | 已把 `_figure_ocr_budget()` 默认改为 **0**（并让它按「尝试次数」扣预算、剥掉回吐的图片链接、产出为空时不再逐张重试）；接入 PaddleOCR 后重新打开。图片本身照旧落盘 + 建 figure 块（题注路径可用） |
| 67 | **规则型忠实度校验在表格类语料上会误杀好答案**：比对口径是「claim 整串是否为原文子串」，而模型会对表格原始值做单位换算与格式化 —— 实测 `2026年7月能耗分析.xlsx`：答案写「474.004万元」，单元格里是 `474.004`；答案写「7.69%」，单元格里是 `0.0769`。25 条 claim 里 9 条被判「编造」，于是**整答拒答 + 引用清空**（用户看到的是「答不出来」，而检索侧完全正常：dense 30 → rrf 34 → rerank 15 → **C5 kept=15**） | 表格/汇总类问题几乎全部答不出来 —— 对一套有 164 张表的语料，等于核心场景不可用 | ① `claim_supported` 增加「**够特异的数字主体**」等价匹配（带小数点 / ≥3 位 / 千分位，并支持「百分比 ↔ 小数」换算）；1~2 位整数**刻意不参与**（如「28万元」的 28 在长文里几乎必中，会把校验放空）。② 改**分级**：`missing_citation` 与「unsupported 占比 ≥60% 且 claim ≥3」仍**硬拒**，其余**降级放行**（答案与引用都保留 + `degraded=unsupported_claim:N` + 记 `faithfulness_degraded` 审计 + 调试台黄色提示「部分数字未能逐字核对」）。同一条问题实测：修复前 `refused=true / 引用 0`，修复后 `refused=false / 引用 4`。**这是口径变更**：方案侧若要求「凡有存疑数字必拒」，请回退本条 |
| 68 | **表格块借 `figure_id` 存了 `table_id`**（`chunker.py` 里写的是 `figure_id=table.table_id`，而那是「文档内第几张表」的计数器 1、2、3…）。实测 164 个表格块**全中** | 引用表格块时前端看到 `figure_id` 有值就去渲染图片 → `GET /assets/figure/{chunk_id}` 取不到（表格本来就没图）→ 界面显示「图不可用」；同时污染「带图引用」这类统计 | 表格块不再写 `figure_id`（表格标识在正文 `表名: …` 里已足够；真要单独追踪应另开 `table_id` 字段，而不是借用 `figure_id`）；另用一条 SQL 把线上 164 行的 `figure_id` 清空（**不动向量，无需重嵌**）。已加断言：`tests/test_office_tables.py` |
| 69 | **`/health/deps` 把「本地计数器」与「网络探针」一起缓存 60 秒**。本地计数（分片数/向量数/磁盘/指纹）读库即得、免费，没有理由与会出网的探针共用 TTL | 排查时被它误导：实测软删一份文档后立刻查询，它仍返回删除**前**的向量数（真实 3、显示 4），据此差点判定「软删漏摘向量」（后经文件/登记表/metric 快照三方对账才排除） | 改成：**网络探针**（embed/llm/rerank/dingtalk）照旧缓存 60s；**本地计数每次现算**。验证：重建分片后立刻查到 `vectors=0` |
| 70 | **「服务器目录导入」在界面上不可达**：该功能只存在于**旧的免构建单页**（`ekbs/api/admin_ui.py`，页签里有「服务器目录导入」），而 `/admin` 已被 SPA 顶掉（`ekbs/api/app.py` 里 `install_admin_web` 先注册 `/admin` 与 `/admin/{spa_path:path}`，旧单页让位）。实测 `/admin` 返回的是 SPA（含 `<div id="root">`、0 处「服务器目录导入」）。SPA 的上传页只用 `/import/dirs` 填「保存到文件夹」下拉，**没有** scan/run 的界面 —— 于是 §六 第 4 条那句「打开 /admin → 左栏「导入」」是失效指引 | 有功能、没入口：目录导入只能直接调 API 或 CLI（本轮重灌 16 份语料就是这么做的） | 在 SPA 新增「知识资产 → 目录导入」页（`ekbs-admin-web/src/pages/import/ImportDirs.tsx`）：逐层浏览 raw 目录 → 扫描预览（counts/blockers/warn）→ 勾选导入或整目录导入，复用 `/import/scan` 与 `/import/run`；预览截断（>200 条）时禁用「按勾选导入」以防静默只导前 200 份。另把 `/import/meta` 的 `knowledge_bases` 补上 `default_level`（原先缺该字段，界面只能去调需 `kb:knowledge_base:read` 的 `/kb/knowledge-bases`，只有 `doc:document:create` 的角色拿不到 → 密级悄悄回落 L1） |
| 71 | **本仓库的 `tsc --noEmit` 是空转**：根 `tsconfig.json` 是 solution-style（`files: []` + `references`），它一个文件都不检查 —— 拿一个**故意写错类型**的文件实测：`tsc --noEmit` 退出码 0，`tsc -b --noEmit` 才报 TS2322 | 前端"类型检查通过"可能是假绿：改坏了类型也照样 exit 0 | 真实检查用 **`tsc -p tsconfig.app.json --noEmit`**（不写 tsbuildinfo）或 `tsc -b --noEmit`（会写脏**被跟踪**的 `tsconfig.app.tsbuildinfo`）。建议把 `tsconfig.app.tsbuildinfo` 从版本控制里摘掉（它每次构建都会变）|
| 72 | **拒答阈值 `rerank.min_score` 在真实语料上偏低**：原值 0.60 是拿 **2 个 demo 文档**校准的。用 16 份真实语料（28 正例 + 8 负例）重做后：正例 top 分 `min 0.583 / P10 0.774 / 中位 0.980`，负例 `min 0.273 / 中位 0.326 / **max 0.608**` —— **两带重叠**（负例最高 > 正例最低），单一阈值不存在干净分界。逐阈值实测：0.60 → 误拒 1 / **漏放 1**；0.62–0.65 → 误拒 1 / **漏放 0**；0.70–0.75 → 误拒 2 / 漏放 0 | 0.60 那条漏放的是「消防演练多长时间组织一次」（0.608）：语料里根本没有，却会拿无关内容交给 LLM 去圆 | 取 **0.65**（`config/application.cloud.yaml` 与 `deploy/config/application.yaml` 同步，注释里保留全部实测数字）；线上实测那条负例已变成 `below_threshold` 拒答，而正例照旧命中。复现：`python -m ekbs.tools.eval_switch_sweep`。**2026-10-02 换 `fusion: alpha` 之后又校了一次**（分数分布会随融合变）：正例 min 0.570 / P10 0.696、负例中位 0.357 / max 0.608，最优区间 **0.62–0.67**（均为误拒 1 / 漏放 0）→ **0.65 维持不变** |
| 73 | **四条检索增强首次被 A/B 量化，结论是「一条有用、三条量不出」**：新增金标题集（28 正例 + 8 负例，[docs/eval/](docs/eval)）与对比工具 `ekbs/tools/eval_switch_sweep`（复用 `eval_retrieval` 的打分口径，baseline 逐位一致：Hit@5 96.4% / MRR 0.896）。逐个关开关：**关图片多模态旁路 → MRR 0.896→0.878（旁路净收益 +0.018）**；只用 BM25（α=0）→ Hit@5 −3.6%、MRR −0.027（向量侧是主力）；`multi_query=1` / 关父块扩展 / 向量 top_k 30→60 → **无可测差异**；只用向量与基线完全相同（⇒ 稀疏侧含 Doc2Query 问句行的增量**在这套题上测不出**） | ① 之前"加了四条增强"没有任何证据，现在至少能说清哪条是净收益、哪条是未知；② 顺带暴露**仪表饱和**：这批题的问法与原文高度重合（正例 top 分中位 0.980），只测得出退化、测不出小幅提升 —— 想量 MultiQuery/Doc2Query 得另做**同义改写**题集；③ 顺带纠正了我自己的一次误判：原以为「3.RAG多模态数据处理」那条 `below_threshold` 是「标题没进嵌入」的召回缺口，**实测是精排打分偏低**（该块稠密命中、RRF 第 3、精排第 2 = 0.570，整批候选压在 0.55~0.58 被阈值切掉），而 `chunker.retrieval_text` **一直是** `heading + 正文` —— 详见第 74 条 | 工具与题集已入库（`docs/eval/golden_corpus16.json` + `_negative.json`）；`eval_switch_sweep` 的负例指标**必须用 `top_score`**（拒答时 `chunks` 被清空，用 `chunks[0].rerank_score` 会只统计到没被拒的那几条 —— 我第一版就是这么错的） |
| 74 | **仪表饱和 + 我的一次误判（两件事一并纠正）**。① 误判：看到「RAG多模态数据处理」那条 `below_threshold`，我判成「小节标题没进嵌入」的召回缺口，差点去改 `retrieval_text` 并全量重嵌 16 份（收益为零）。**实测证伪**：该块稠密召回**命中**、RRF 排名 **第 3**、精排 **第 2（0.570）**，是整批候选分数压在 0.55~0.58 被阈值切掉；而 `chunker.retrieval_text` 一直是 `heading + 正文`（其 docstring 里就写着「只嵌入正文时查『住宿标准』会落空，实测踩到过」）。**教训：改口径前先读那个口径的唯一实现**。② 仪表饱和：原题集用「原文片段」当期望、问法又与原文高度重合（正例 top 分中位 **0.980**），只能测退化、测不出小幅提升 | ① 差的诊断会让下一步把力气花在没收益的地方（全量重嵌不是免费的：16 份语料要重跑嵌入）；② 饱和的仪表对"某条增强有没有用"给不出答案，会让人误以为"加了没用" | ① 新增**同义改写题集** `docs/eval/golden_corpus16_paraphrase.json`（17 题，问法贴近真人、不与原文共享关键词；期望片段**逐条先用 SQL 验证存在于语料**才入集）：Hit@5 **82.4%** / MRR **0.657**（原集 96.4% / 0.896）—— 有分辨力了；② 新集立刻暴露一个真实模式：**文档召回到了、但承载答案的那个块没进 top-5**（3 条未命中全是这种形态），这指向 chunk 粒度 / `rerank_keep` / 父块扩展，而不是召回 |
| 75 | **`fusion: rrf` 是负优化：把很弱的稀疏侧与向量侧同等加权**。用两套题量（原题集已饱和，另加 17 题同义改写集）：`rrf` 同义改写集 **82.4% / MRR 0.657**；`alpha 0.85` **88.2% / 0.664**；`alpha 1.0`（纯向量）**94.1% / 0.683**；纯 BM25（`alpha 0`）**47.1% / 0.402**。原题集上四者全部持平（96.4% / 0.896）—— 也就是说**只有口语化问法能暴露这个差距**，而口语化正是钉钉里真人提问的常态 | 融合是每条查询的必经环节，权重配错等于把好候选挤下去：同义改写集上 RRF 比"偏向向量"低 6~12 个百分点 | 改 `fusion: alpha` + `hybrid_alpha: 0.85`（cloud 与 deploy 模板同步，注释里留下整套对比表）。取 0.85 而非 1.0：两者只差 1 道题（17 题里 5.9%），在噪声内，保留 15% 词法权重作为「精确编号/金额」类查询的保险。线上已验证参数生效（`fusion = alpha, hybrid_alpha = 0.85`）|
| 76 | **图片多模态旁路的云端配额是瓶颈**：压测（连续提问）下 `tongyi-embedding-vision-plus` **几乎每次调用都被 429 限流**（sweep 日志：858 次调用 vs 882 次 429），客户端靠退避重试才走完，一轮 A/B 从 7 分钟被拖到 50 分钟 | ① 每条查询都要多打一个"低配额、被限流即重试"的云端调用；而这条旁路实测只值 **+0.008~0.018 MRR**（关掉它：同义改写集 0.657→0.649、原题集 0.896→0.878）；② 排查时极易误判为"程序卡死"（工具输出还是块缓冲，看起来像没反应）| **已修（2026-10-02）**：多模态**查询向量**补上了缓存（原先文本查询向量有 `query_embedding_ttl` 缓存、这条却每次直连云端 —— 同一问题每问一次就多打一次），实测同一问题问两次（绕开答案缓存）云端调用 **2 次 → 1 次**。键用 `CacheStore.mm_embed_key`，**与文本嵌入分开命名空间**（维度不同：1152 vs 1024，混用会静默搜不准）。⚠️ 这从头到尾是**重复付费**问题，**不是「要不要图片旁路」** —— 图片向量化 + 参与检索是需求且已满足：`figures.faiss` 201 向量 / 1152 维，与库里 201 个图片块一一对应（孤儿 0），作为第三路参与融合，关掉它 MRR 下降（0.896→0.878、0.657→0.649）。排查经验：`--calibrate` 与 sweep 建议用 `python -u` 跑，并看 `var/log/eval_switch_sweep.json.log` 里的 `multimodal-embedding` 计数判断是否在重试 |
| 77 | **数字准确性：把课程 19 的"取数"口径落成一条可选路径**（`mode=numeric`）。课程的方法是"**不解析数字，只在约束下抄数字**"（类型锁死 `float｜int｜"N/A"` + 不许算/不许猜），而我们原来的业务问答是"自由文本 + 事后拿规则对账" —— 后者对换算/四舍五入/派生/张冠李戴四类判不了（实测 `474.004万元` vs 单元格 `474.004` 那一类就是靠打补丁修的）。新增 `ekbs/retrieval/numeric.py`：独立提示词（不许计算、指标不等价就 N/A、四种格式陷阱、正例+N/A 负例）+ **抄写校验**（模型给的数必须逐字出现在它引用的原文片段里，量级等价可用）—— 这一层比课程更强（课程靠类型锁死，我们额外证明"这个数是从这段原文抄的"）| 单指标取数（"电梯维保一年多少钱"）走这条能同时拿到**值 + 单位 + 页码 + 原文片段**，且编数字/算出来的数一律 N/A 拒答；业务问答不受任何影响 | 三处**刻意隔离**以避免"有效但矛盾"：① 全局 `SYSTEM_PROMPT` 一个字不动（"宁可 N/A 也不解释"塞进去会污染业务问答），改用 `NUMERIC_SYSTEM_PROMPT`；② 答案缓存键加 `mode=`（否则两种形态的答案会互相命中）；③ 取数模式不跑自由文本的 `assess_faithfulness`（那是事后对账，对结构化值不适用），改由抄写校验承担，并在 `faithfulness.reason` 里显式写 `numeric_copy_verified`，免得调试台按另一套面板显示成"未通过"。**已补**：服务端 `response_format={"type":"json_object"}`（provider 透传，**只在取数模式带**，既有请求体逐字不变）+ **一次格式修复重试**（只在"输出不是合法 JSON"时触发；模型按规则返回 N/A 不重试，免得白烧调用）。**尚未做**：`json_repair` 式畸形修复库、强制 CoT≥5 步、**指标等价性的二次复核**（见第 79 条）。测试：`tests/test_numeric_mode.py`（30 用例）|
| 78 | **图索引缺"跨进程外部变更"保护 → 静默累积孤儿向量**。事故链（2026-10-02 实测）：`ekbs-api` 软删 16 份文档时摘掉图向量并把 `figures.faiss` 原子替换成空文件（90 B），而 `ekbs-worker` 仍持有**启动时那份内存索引**；worker 随后重新入库时在陈旧副本上追加新向量 → 存盘后**新旧两代并存**：`figures.faiss` 里有 **402** 个向量，而库里只有 **201** 个图片块（201 个孤儿，且搜索时它们会占掉 Top-k 名额）。文本分片**早就有**这套 `(mtime_ns, size)` 指纹重载（`faiss_store.ShardIndexManager.get` 的 `shard_reloaded_externally`），图索引当时没有 | 孤儿向量会被召回（chunk_id 在库里已不存在），挤占图片旁路的候选名额；`/health/deps` 与指标读到虚高的向量数；跨进程行为不一致（文本正确、图片错）| `FigureIndex` 补上同一套指纹机制（`_reload_if_changed()`，`upsert`/`remove`/`search`/`vector_count` 四个入口都过），并记下自己写完后的指纹以免白重载。已按"索引里有、库里没有"精确清掉那 201 个孤儿（现 201 = 201，孤儿 0）。单测：`tests/test_eval_tools.py::test_figure_index_reloads_external_changes` |
| 79 | **取数模式的能力边界：抄写校验保证"抄得对"，不保证"抄得准"**。实测（同一个问题连问两次）：「商铺住宅和公司承担的电量合计是多少度」第一次返回 **26087023**、第二次返回 **25178448** —— 两者**都真实存在于语料**（分别是 `2026年` 与 `2024年` 的 `总用电/度` 行），因此**都通过了抄写校验**。根因：问题里没指定年份，模型选了不同年份的行；而抄写校验只能证明"这个数是从这段原文逐字抄的"，**证明不了"抄的是问题问的那一行"** | 取数结果在口径歧义时不稳定（同一问题不同答案，且都"有出处"）；这正是课程**防线 3「严格指标匹配五条」**要解决的问题 —— 属语义判断，规则校验兜不住 | 现状缓解：① 取数结果同时返回 `indicator` 与 `quote`，调试台的取数卡片把**依据片段整段显示**，用户能一眼看出抄的是 2024 还是 2026；② 提示词已写入"指标范围/口径不完全一致 → N/A"（但只是提示词约束）。**待办**：把口径限定词做成结构化字段（年份/表名/行名）并显式进答案，或问题缺口径时按 N/A / 反问（课程更保守的口径）。这是"数字准确"这条路上**唯一还没被结构化的环节** |
| 80 | **表格的「合并标题行」被当成了表头 → 列名退化成 `列2…列N`**。实测：`2026年7月能耗分析.xlsx`「用电分析」表前两行都是合并单元格（第 1 行表名、第 2 行写单位 `电量分析(KW/h）`），真正的内容行在第 3 行；而 `table_serialize.serialize_information_blocks` 取**第一个非空行**当表头 → 全库 **97 个表格块**中招（重灾 doc 44 一个文件 80 个）。判据：`content like '%列2 %'` | 表格的**语义就是列名**：列名叫 `列2` 时，按列名检索召不回、取数也定位不到列（口径核实依赖「标签=值」结构）—— **内容在、语义没了**。质量报告当时完全没报这个（它只看表格是否非空） | 修：开头「足够宽却只有 ≤1 格有字」的行判为**标题行**，剥掉后取下一行当表头；标题文字**折叠进「表名」而不是丢掉**（第 2 行常写单位口径，丢了数值无法解读）。单测见 `tests/test_table_serialize.py`；已随本轮重灌生效 |
| 81 | **章节分隔页的标题不进索引**（入库「丢结构」）。实测 doc 41（`开班典礼.pdf`，课程课件导出的 55 页）：质量报告报 7 个**零产出页** `[3,4,5,7,11,14,26]`；回源直读 PDF 发现这些页**并非空白** —— 每页都有标题（`一、课程安排`、`模型训练与微调`、`模型部署及高并发`…）加平台水印 `知乎知学堂`×6。库里核对：标题在索引里出现 **0 次**，而水印**正确地被丢弃**（含水印的块 0 个） | 课件/制度文件的**大纲**（章节标题）不可检索 —— 用户按章节名问问题时这一层线索完全不存在；而水印被丢掉是对的，说明「页装饰过滤」已在工作，问题只出在「标题行没有正文就不产生块」 | **诊断链**值得复用：汇总指标（零产出页）→ 回源直读原件 → 库里逐词核对（标题/水印分别查）。**待办**：标题行即使没有正文也应产生标题块（或至少进 `heading_path` 供导航），否则任何「只有标题的页」都会静默消失 |
| 82 | **质量指标要区分「正文短」与「没产出」**。本轮改了三处判读口径：① 只有图/表的页**不再算空页**（正文短≠没内容：PPT 插图页、扫描图片页常只有 `[图片 xxx]` 17 字 < 20 阈值）；② 新增**零产出页**（一个块都没有）+ 以 `ekbs_document.page_count` 为分母的丢页率 —— 只看「块覆盖到的页码」会漏掉尾部的空白/未解析页（实测 doc 41 源 55 页 vs 块覆盖 48 页）；③ **幻灯片类（`.ppt/.pptx`）不做空页率准出**：拿「页面正文长度」这条为页面型文档设计的准出线去卡幻灯片是**指标误用**（doc 33 的章节过渡页反复假警报），记 N/A 并在报告里写明原因 | 修之前：16 篇语料报 2 篇 FAIL，实际**两篇都不是清洗缺陷**（一篇 PPT 过渡页、一篇课件章节页）—— 指标噪声会让人**忽略真问题**（狼来了） | 实测：doc 41 的零产出 7/55=12.7% 被单独列出，人工对源核对后确认是标题页（见发现 81）。`assess_parse_quality` 新增 `visual_pages`/`source_page_count`/`lost_pages`/`lost_page_rate`/`slide_based`，报告工具与 `assess_batch` 已串通。单测见 `tests/test_ingest_quality.py` |
| 83 | **「清洗」此前没有独立实现** —— 入库的文本就是解析器吐出的原始 Markdown。`pipeline.ingest_markdown` 的 docstring 写的是「清洗与解析由调用方完成」，方案 `06` §1.7 的「清洗 10 步」只零散落实了几条（表格序列化在 `table_serialize`、页装饰过滤在解析器里各做各的）。后果实测：MinerU 数学模式把 `100%` 输出成 `$1 0 0 \%$`，**原样入库就再也搜不到**（用户搜「覆盖率100%」，库里是 `1 0 0 \%`） | 关键词那一路直接召不回；且这类「存进去了但搜不到」的问题**质量报告看不出来**（内容非空、乱码率为 0） | 新增显式一步 `ekbs/ingest/clean.py`，插在 `checksum`（仍基于解析原始产物，去重语义不变）**之后**、分块**之前**；第一条规则 = 公式/单位残留规整（`$1 0 0 \%$`→`100%`、`$100 \text{‰}$`→`100‰`、`$4 2 . 6 \%$`→`42.6%`），**真公式一律不动**（含 `\frac`/`^`/字母 → 原样保留）。xlsx/docx 的原生表格单元格走同一函数。单测见 `tests/test_clean.py` |
| 84 | **本地 token 计数的词表选错同源 → 偏差最高 47%，而且不是常数**。规格 `02` TC-INDEX-004 要求"取 200 段真实文本，本地计数与云端 `usage.total_tokens` 偏差 ≤5%"，**这是所有尺寸判定的前置条件**。实测（200 段真实文本，云端真值 25,883 token = 0.964 token/字符）：`tiktoken cl100k_base` **16,731（−35.4%）**、jieba 词数 **7,671（−70.4%）**、Qwen 官方词表 **23,010（−11.1%）**。分组看更糟：同一份本地计数在**中文散文上 +9%、在数字/表格行上 −42%**（Qwen 词表则相反：−30% / −10%）→ **没有任何单一本地词表能在所有文本类型上 ≤5%** | `token_count` 决定分块尺寸（`PARENT 1024~1536`、`CHILD 256~384`、`TABLE_MAX 2048`）、质量报告的 p50/p90、尺寸合规率、成本估算 —— 口径偏 35% 意味着**这些数字全都不是它声称的含义**；更麻烦的是偏差随文本类型在 −47%~+47% 之间摆动，**乘一个系数修不了**。根因：给 Qwen 系嵌入模型配了 OpenAI 的 `cl100k_base` 词表（同源选错） | ① `count_tokens` 改为**Qwen 词表优先**（`ekbs/tools/fetch_tokenizer` 部署时取一次到 `var/models/`，纯本地零成本），取不到**回退 tiktoken 并告警**（分块链路不被数据文件卡住）；② `/health/deps.token_counter` 显式暴露当前口径（避免"悄悄降级"）；③ 新增 `ekbs/tools/token_calibration`（按 TC-INDEX-004 判定，`--fail-over` 可做门禁，**按中文/数字/混合分组报告**）；④ 测试夹具改为**按 token 造正文**（原来按字符造，换词表就假失败 —— 实测 1600 字符在 Qwen 口径下只剩 960 token < 下限）。**遗留**：要真正 ≤5% 还需"每篇文档用云端真值校准"（云端只给整批合计、不给逐条），待办 |
| 85 | **规格文档里的嵌入模型名已漂移**：`docs/spec/10`、`20`、`40` 写的是 `dashscope/text-embedding-v4@v4#1024`，而**实际配置**（`config/application.cloud.yaml`）是 `qwen3.7-text-embedding-flash`。这直接决定两件事：① 该用哪个词表做本地计数（发现 84）；② `TC-INDEX-004` 的校准对象是谁 | 文档与配置不一致时，"按文档校准"会校准到**错误的模型**上；换模型后本地计数口径与尺寸阈值都应重做，而文档会让人以为还是 v4 | 校准/口径一律以**实际配置的模型**为准，锚点用系统里已有的 `ekbs_shard.embedding_fingerprint`（换模型它必然变）。建议把"校准结果 + 指纹"绑定存储，并在规格里把模型名改为指向配置项而非硬编码型号 |
| 48 | **MinerU 抽出的图片被 `tempfile.TemporaryDirectory` 回收**：解析产物交给 `assets_dir` 之前，临时目录已随 `with` 退出被删，库里 `image_path` 指向不存在的文件 —— 「图片不丢」在"存了但打不开"这一层就已失效 | 图全丢，引用回源 404 | 图片改为 `_persist_figure()` 复制到**持久目录** `var/parse/<sha\|doc_id>/images/`（按内容 sha 命名，天然去重）；`image_path` 只存**相对 `data_dir`** 的路径，换机/换目录不失效 |
| 49 | **`ekbs_chunk_fts.chunk_id` 是 FTS5 的 `UNINDEXED` 列且以字符串存入**（`FtsIndex.upsert_chunks` 里 `str(chunk_id)`）。手工清理时用 `DELETE ... WHERE chunk_id IN (50)` 用**整数**比较，**一行都删不掉**（FTS5 列无 affinity，不会把字面量转成文本），静默留下 130 条悬挂行；而 `JOIN ekbs_chunk ON c.chunk_id = f.chunk_id` 却能匹配（列对列比较时会应用 INTEGER affinity），所以"查得到、删不掉"，更迷惑 | 手工运维只删了主表，FTS 里留下悬挂行，对账看不出来 | 已记入 `docs/测试方案-按工作流.md` §1.5 的提醒：**动 FTS 一律用 `FtsIndex.delete_chunk_ids()` / `upsert_chunks()`**，不要裸 SQL |
| 50 | **worker 不把文件 checksum 传给 pipeline，导致「秒传/去重」的键在解析完那一刻就变了**：`upload_complete` / 目录导入写入的是**文件内容 SHA-256**，而 `pipeline.ingest_markdown` 里有一句 `checksum = checksum or sha256(markdown)`，worker 没传就回写**markdown 内容哈希**覆盖掉它。后果：同一份文件再次上传查不到重复（去重失效、重复入库、重复向量），而且从库里看不出异常 | 秒传/去重静默失效；同一份文件在库里出现多份 | worker 改为把 `ekbs_document.checksum` 原样传下去（与 CLI 一致），并在用例里断言「解析完成后 checksum 仍等于文件内容 SHA-256」 |
| 51 | **代码块的 `heading_path` 里进了机器绝对路径**：`code_chunker` 的文档明确要求传仓库相对路径，但 worker 与 CLI 传的是 `doc["source_path"]`（磁盘绝对路径），于是变成 `/Users/<用户名>/…/var/raw/代码样例/order_service.py > OrderService`。三个后果：同一份代码在不同机器/不同 `data_dir` 下**检索语义不同**（绝对路径进了嵌入文本）；用户名与项目目录名混进向量与 FTS 成噪声；**服务器目录结构随引用卡片泄漏给最终用户** | 换机/换目录后检索结果漂移；引用里暴露服务器路径 | 新增 `code_chunker.display_source_path()`：raw 下 → `raw/<相对路径>`，data_dir 其它位置 → 相对 data_dir，本来就是相对路径 → 原样，**数据目录之外 → 只留文件名**。已加两条回归用例（含从绝对路径入库的端到端）。**这是实测管理台导入时才暴露的**：单元测试里我用的是相对路径，看不见 |
| 52 | **管理台侧栏菜单点了不换内容 —— 用户看到的症状是「选导入文件没有入口」**：侧栏是原生 `<a href="#/view">`，但页面**从来没有注册 `hashchange` 监听**，点击处理里也只改了高亮就 `return`；于是改 hash 没人渲染，主区域一直停在旧页面。性质恶劣在于：**后端接口全绿、`node --check` 也过**，只有真点一下才知道坏了 | 整个管理台的导航都是失效的（不只是导入页） | 改为原生 hash 路由（`hashchange` → `syncFromHash()` → `render()`），并新增 `tests/test_admin_ui_nav.py`：用 Node + 最小 DOM 桩**把页面真跑起来并模拟点击**，断言内容确实换了（已验证：去掉监听后该用例立刻变红）。另把「导入」做成侧栏常驻按钮、导入页默认落在上传页签、加拖拽区与已选文件回显，不再让「选文件」藏在第二个页签里 |
| 53 | **上传 `.xlsx` 被判「不支持的类型」**：三层原因叠在一起 —— ① 前端**又硬编码了一份** 11 种白名单（刚在 CLI/配置之间修掉「两张皮」，前端重犯）；② 扩展名取法太脆：`PurePath(name).suffix` 遇到 `报表.xlsx `（尾部空格，Windows/邮件下载常见）得 `'xlsx '`、遇到 `报表．xlsx`（**全角**句点，中文输入法）与 `报表.xlsx.` 得 `''`，三种都会命中「不支持」分支；③ 报错只说「不支持的类型」却不显示**检出的是什么**，用户无从判断 | 一个明明支持的格式传不上去，而接口单独测是 200 —— 只在真点界面时才暴露 | 后端新增 `parser.normalize_suffix()`（去空格 / 全角句点 / 尾部点 / 大写归一），上传与目录导入**共用同一份**；前端新增 `utils/fileExt.ts`（与后端同口径），且准入清单改为**从 `/import/meta` 取**、拿不到时不拦（权威在后端，原则 P3）；报错改为 `不支持的类型「.{检出的后缀}」；支持 .pdf / …`。两侧各加用例，含「归一化不能把 `.exe` 也放过」的反向断言 |
| 54 | **两个上传问题同源：拖入文件夹时浏览器给的是「没有扩展名的文件」**。① `方案 §4.4` 要求的「选择文件夹」我**根本没做**（只做了选择文件）；② 拖入文件夹时浏览器交的是一个**目录 entry**，早先直接拿 `dataTransfer.files` 当文件列表，得到的是一条「名字是文件夹名、无后缀」的记录 → 被类型校验判成「不支持的类型（无后缀）」。所以用户看到的是「拖文件夹说格式不支持」，指向了完全错误的方向 | 文件夹根本传不了 | 新增 `utils/fileDrop.ts`（`webkitGetAsEntry` 递归展开）+「选择文件夹」按钮（`webkitdirectory` 原生目录选择器）+ 保留相对路径以区分不同目录下的同名文件 + 单次 300 个文件上限并如实告知截断 |
| 55 | **递归展开目录的 `for(;;)` 可能永不退出**：`readEntries` 必须循环读到返回空数组（浏览器每次最多给 100 条，不循环会静默少传），但若实现异常或软链接成环让它**一直返回非空**，同层循环就退不出去 —— 目录深度护栏只管纵向，管不住横向爆炸。**这条是新写的测试把它挂死才发现的**（vitest 卡了三分钟被我杀掉） | 页面卡死且没有任何报错 | 加**全局遍历预算**（`MAX_ENTRIES=5000` / `MAX_READ_CALLS=500` / `MAX_FOLDER_FILES=300`），并加一条专门断言「readEntries 永不返回空时必须终止」的用例 |
| 56 | **「上传 xlsx 提示：提交内容不符合要求」——真实原因被我的错误处理吃掉了**。前端 `errorTextOf(code, status)` 排在 `detailToText(body.detail)` **前面**，于是任何 422 都被状态码兜底文案（`422 → 提交内容不符合要求`）盖住，后端那句「上传初始化时需给出 kb_id 或 kb_code」根本传不到界面上，用户无从下手。而真实原因确实是它：上传页当时只有一个**空的**「知识库 code」文本框，而后端在 **complete** 阶段要求 `kb_id` 或 `kb_code` 至少给一个 | 报错指向错误方向，同一个 422 有多种成因却长得一模一样 | ① 错误处理改成**后端 `detail` 优先**，状态码兜底文案垫底；② 上传页的知识库改成**从 `/import/meta` 取的下拉选择**并默认第一个，提交前本地先拦一道并给出明确提示 |
| 57 | **上传文件夹时「文件夹被分离了」**：后端用 `Path(filename).stem` 当标题、落盘只取 basename，相对路径整段丢掉 —— 传进去的是 `制度/2026/预算表.xlsx`，库里只剩「预算表」 | 目录结构完全丢失，用户以为功能坏了 | `UploadInit` 增加可选 `title`；前端上传时带上**相对路径**与其去扩展名形式；后端新增 `safe_relative_name()`（挡掉绝对路径/`..`/隐藏段）后按相对路径落盘并建父目录。实测：标题 `制度/2026/预算表2026`、落盘 `raw/<sha>/制度/2026/…`。**注意方案 §1.5 把「目录树管理」列为首期不做（扁平目录+标签）** —— 现在保留的是路径信息，不是可授权的目录对象 |
| 58 | **「FAISS 不支持热更新」这个前提只对了一半**：新增早就热（`os.replace` 原子替换 + 按 `(mtime_ns,size)` 指纹跨进程重载），删除在**逻辑上**也热（检索回表联查 `ekbs_chunk`，孤儿召不回），真正做不到的只有**单条物理删除**。而物理删除的缺失会连带出一串「删完还要重建」的收尾动作 | 删文档后分片里堆孤儿向量，占内存又占盘；重建分片时 `faiss_row` 全体行号重排，是「重建完所有行号错位」的隐患 | 索引换成 **`IndexIDMap2`**（标签 = `chunk_id`）：`remove_ids` 真删、`upsert` 原地替换、重建不再重排行号。实测删文档后分片登记向量 3 → **0** |
| 59 | **`add_with_ids` 对重复 id 是累积而不是替换**（实测：同一 id 加两次 `ntotal` 1→2，且搜索会把同一个 chunk 返回两次）。而「编辑子块 / 换级重嵌」正是**同一个 `chunk_id` 重新嵌入** —— 如果只做 `add_with_ids`，索引会越长越大且召回重复 | 编辑一次就多一条重复向量，越用越慢越脏 | 新增 `ShardIndex.upsert()`（先 `remove_ids` 再 `add_with_ids`），编辑/换级/重嵌全部改走 upsert；并加用例把这个「必须 upsert」的事实钉住 |
| 60 | **权限目录缺 22 个首期权限点，却没有任何用例守着**：方案 `03` §3.4 首期启用 **57** 个，实现只有 41 个；另有 `qa:chat` 用了**封闭模块前缀集合之外**的 `qa` 前缀。缺权限点的表现是「某个按钮消失 / 某个接口 403」，不测永远发现不了 | 首期功能看似缺失、且新增权限点无从对齐 | 按方案 §3.2 的矩阵**程序化生成**补齐（避免手抄）：编码阶段用脚本解析矩阵，生成权限点与角色授权；`qa:chat` 登记进 `LEGACY_PERMISSION_MAP` 重命名为 `audit:qa:chat`（存量库升级时自动搬，不会新旧并存）；新增 `tests/test_rbac_spec_conformance.py` 6 条用例，其中一条**逐点比对方案矩阵**，并把「已知且有意保留的缺口」写成带原因的清单 |
| 61 | **`/docs/documents` 没有行级权限过滤**：它只要 `doc:document:read` 就返回**全库**文档的标题/密级/状态，没有任何 `clearance`/`visible_dept_ids` 条件（实测确认）。也就是说「列表不泄漏」完全依赖「只有受信管理员角色持有这个点」这个隐含假设 | 一旦按方案 §3.2 把 `doc:document:read` 授予 `auditor`/`end_user`（矩阵标着 ✔），他们就能枚举全库文档元数据 | 本轮**不授予**这两类角色的读权限点，并在 conformance 用例里把这条写成带原因的「已知缺口」；要放开必须先给列表类接口补数据层过滤 |
| 62 | **改了 `faiss_store.py` 却只重启了 api、忘了 worker** → 用户上传直接失败。旧 worker 进程（改代码前启动）对着已被新代码换成 `IndexIDMap2` 的分片文件调 `index.add(向量)`（不带 id），FAISS 抛 `IndexIDMapTemplate<Index>::add`。**这是我自己造成的**：上一轮刚从同一个坑里学到「部分进程跑旧代码最难查」，这轮又踩了 | 用户看到「上传成功」但文档 `failed`（异步失败，界面不显示原因） | 重启 worker 并重新上传验证通过；**教训固化**：改 `ekbs/index/` `ekbs/ingest/` `ekbs/retrieval/` 下任何文件后，api/worker/scheduler **三个都要重启**（它们各自 import 一份） |
| 63 | **知识库只能靠种子造、界面里只有一个 `demo`**：`/kb/knowledge-bases` 一族接口（`方案 05` §8.1）没实现，只有 CLI 的 `ensure_kb` 能建库 —— 于是管理台里永远只有「演示知识库」可选，用起来就像个演示程序 | 用户直接质疑「这还算演示程序吗」 | 新增 `ekbs/api/kb.py`（列/建/改/删 + 文档数），界面支持「新建知识库」并按**名称**显示；种子库改名 `演示知识库` → `企业知识库`、code `demo` → `main`；删库有护栏：**库里有文档时 409 拒绝**，避免误删整库语料 |
| 64 | **上传落盘目录是 `raw/<sha256前16位>/…`** —— 文件管理器里就是一堆 `95fbf1ef16573acf` 这样的壳子，人认不出是什么；用户反馈「上传后会自动产生一个数字字母编号的新文件夹壳子」 | 落盘结果不可读，运维时无法判断哪个目录是谁的 | 改成 `raw/<知识库 code>/<上传时的相对路径>`（如 `raw/main/制度/年假办法.md`）；同名但内容不同时加 `-<sha8>` 后缀而不覆盖原件；去重本来就在库里按 checksum 做（秒传），不依赖目录名 |
| 65 | **云端配置的 `database.path` 与 `data_dir` 分居两棵树**：文件头写着「与 dev 的唯一区别是模型 provider」，但 `data_dir: var` 而 `database.path: var-cloud/index/ekbs.db`、`log_dir: var-cloud/log` —— DB 在 `var-cloud/`、raw/parse/分片按 `data_dir` 落在 `var/` | 环境边界说不清：清一次 `var-cloud` 就把「云端环境」弄坏了（表现为 `Schema 自检未通过：数据库文件不存在`，而报错完全看不出是配置分叉导致的） | 把 cloud 配置的 `log_dir` / `database.path` 对齐到 `var/`，与它自己声明的「唯一区别是 provider」一致 |
| 86 | **BM25 的词项被通用 jieba 切碎 → 词面精确匹配失效，而「碎片 OR」查询会把库里根本没有的编号也搜出来**。实测（`ekbs/index/fts.py`）：`XJWY-GC-01-01`、`XJ-01-11-1F02`、`XIWY-QR-GC-002` 都被切成 **7 个碎片**（`['xjwy','-','gc','-','01','-','01']`），`值班记录`/`交接班记录`/`维保费`/`能耗分析` 各 2 片。后果：查询 `XJ-01-11-1F02`（**库中出现 0 次**）竟然「命中 1 块」—— 那是碎片 `01` 的假命中；`值班记录` 命中 4 块，其中 2 块与该词无关。 | 编号/表名/术语的精确匹配基本失效，假命中还会污染 RRF 融合与精排候选（把它们喂给重排模型等于浪费额度） | ① 新增候选挖掘 `ekbs/tools/build_userdict`（A 编号 / B 书名号 / C 括号内 / D n-gram+邻接熵，且**当前被 jieba 切开**）；naive 版前 32 条约 60% 是**跨词边界碎片**（`的电`/`设备的`/`了电`）→ 加「虚词首尾 + 邻接熵 ≥1.5 + 中文占比 + 非数字开头」过滤后清单才可用（**必须人工评审，不能自动上线**）；② 评审后落 `var/dict/userdict.txt`（23 词，含丢弃理由）；③ `fts.tokenize()` 惰性 `jieba.load_userdict`，`/health/deps.fts_userdict` 暴露「路径+词条数」（缺失为 `none`，**不许悄悄降级**）；④ 新增 `ekbs/tools/rebuild_fts`：`content_tok` 是**预分词存库**的 ⇒ 只改词典不重建 = 写入侧旧口径、查询侧新口径（同号两边切法不同，匹配不上）；重建复用生产写入器、doc2query 行由本地启发式原样复原、**不动向量**；⑤ 实测：切碎率 **7/7 → 0/7**，`值班记录` 命中 4→2（真命中块 4854/4856 全保留）、`XJ-01-11-1F02` 1→0（消掉假命中）、非术语查询（15/5/3 块）不变；⑥ ⚠️ 重建后**必须重启服务**（jieba 用户词典在进程内**无法卸载**，否则查询侧仍是旧口径） |


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
| 87 | **换嵌入模型/编码变体时，「修复手段」与「启动自检」互相死锁**：`Runtime.create` 无条件 `shards.load_active(strict=True)`，于是分片指纹一旦与当前模型不符（换模型、换维度、开 `text_type` 不对称编码都会），**服务起不来（正确），但 `rebuild_shard` 这个唯一的修复工具也起不来（错误）** —— 实测 `python -m ekbs.tools.rebuild_shard --shard-id kb_all_L1 --dry-run` 直接抛 `EmbeddingFingerprintMismatch: kb_all_L1 嵌入指纹与当前模型不一致：登记 c0b1f49e… ≠ 当前 d2583d29…`，连 dry-run 都跑不了。 | 迁移路径被自己堵死：要修就得先起服务、要起服务就得先修（生产上表现为「换个模型整站起不来」） | `Runtime.create(..., strict_shards=False)`（**只给迁移工具**，服务进程永远严格），`rebuild_shard` 用它；用例 `test_migration_tool_can_start_despite_fingerprint_mismatch` 钉住「严格抛异常 / 非严格能起」两端。顺带确认守卫有效：切换后 `/readyz` 的 `embed_fingerprint_match=false`、服务拒绝启动 —— 这是**对的**，绝不静默混用新旧向量 |
| 88 | **两个「静默出错」的小陷阱（都在这一轮踩到）**。① `DashScopeEmbeddingProvider.embed()` 收到**裸字符串**时按「一串文本」逐项迭代 → 把问题拆成**单个字**各嵌一次（又费钱又离谱）：实测查询侧误写 `embed(question)` 后下游余弦计算直接类型错。② 新工具的 `--json` 输出被 structlog **默认打到 stdout** 污染（正式配置是打到 stderr）→ 管道里 `json.loads` 报 `Extra data: line 1 column 5`，根因是工具没调 `configure_logging`。 | 都会让自动化/评测拿到**错数据却不报错**（①花冤枉钱，②解析失败或读到脏数据） | ① `embed()` 遇到 `str` 直接 `TypeError` 并提示改用 `embed_one()/embed_query()`（用例钉住）；② 工具统一 `configure_logging(settings, "tools")`，并在 docstring 写明 `--json` 的 stdout 只放 JSON |
| 89 | **上下文没有预算 ⇒ 10 轮记忆 + 5 个表格块最坏能塞到 5 万 token，直接撞模型窗口**。推算：资料 5 块 × 2048（`TABLE_MAX_TOKENS`）= 10,240；记忆 10 轮 ×（答 2048 + 问 2000）= 40,480；合计 ≈ 5 万，而 qwen-max 官方**最大输入 30,720**。改动前我在 `ekbs/retrieval/`、`ekbs/api/qa.py` 里 **grep `truncate`/`max_context` 一处都没有** —— 即最坏路径**没有任何兜底**，只会撞云端参数错。库里 26 条真实问答实测 `prompt_tokens` 平均 3,103 / 最大 10,185（日常安全，但最坏无保护） | 超窗时请求直接失败（用户看到「服务不可用」），或平台侧静默截断（资料被切掉 → 答案缺依据）；多轮场景放大 | ① 新增 `models.llm.max_input_tokens: 20000`（本地 Qwen 计数 × 最坏偏差比 **1.19** ≈ 23.8k ⇒ 留 ~23% 余量）；② `Pipeline._fit_prompt()` 按**牺牲顺序**裁剪：**最旧轮次 → 排名最低的块 → 截断最后一块**（永远保留最近 1 轮 + 至少 1 块，截断处显式标注「内容超上下文预算，已截断」）；③ `generate` 与取数（numeric）两条路径都接，流式走同一个 `answer()`；④ 裁剪痕迹进 `AnswerResult.dropped_chunks / dropped_history / context_truncated` 并打 `prompt_budget_trimmed` 警告；⑤ 顺带把记忆策略调成 `context_ttl_seconds: 7200 → 604800`（**7 天**）、`context_max_turns: 6 → 10`；⑥ 7 个用例钉住（配置策略 / TTL 一周 / 10 轮 / 先丢历史 / 丢块+截断 / 宽松不裁剪 / 真跑一次看痕迹） |
| 90 | **改写要分两类、"变体"要分两侧；稠密侧原来只有单查询**。① 现状核实（代码）：`rewrite.rewritten`（上下文消解后的自包含查询）**稠密与稀疏都用**，但 MultiQuery 变体**只喂了稀疏**（`variants` 只传 `_sparse_recall_scored`），稠密永远只有 1 路。② 两侧需要的变体**不是同一种东西**：BM25 要词面变体（`_keyword_focus`／去语气词），稠密要**语义变体** —— 把关键词碎片喂嵌入会把语义拉偏（此前实测过）。③ 顺带发现一个真 bug：`expand_variants` 内部又按**进程级配置**截断一次，于是检索调试台的 `RetrievalOverrides` **被静默吞掉**（台面调 3、实际出 1） | 稠密召回无法从"换个说法"中受益；调试台调参不生效 → 排查时表现为"改了没用"，极易误判为"这个开关没用" | ① `RewriteResult` 扩展：`intent`（factual/numeric/figure/procedure，**保守**启发式）+ `sparse_variants` + `dense_variants`，旧字段 `variants` 保留并与之同步；② 新增 `QueryRewriter.expand_dense_variants()`（只做自然语言改写：主查询/原始措辞/去口语外壳/补问句）；③ 新增 `retrieval.dense_multi_query`（1=关，默认 1）：每个语义变体各嵌一次、各召回一路，**再用 RRF 融合——不平均向量**（变体越多平均向量越糊）；④ 显式 `limit` 优先于进程配置（修掉 override 被吞）；⑤ 缓存版本加入 `dense_multi_query`（漏了会让旧答案继续被服务、A/B 被污染）；⑥ 16 条用例钉住；⑦ A/B 工具 `var/reports/dense_multi_query_ab.py`（**完整链路**：嵌入→召回→融合→精排）：当前 10 题题集**饱和**（两种设置都 Hit@1 100% / MRR 1.0）⇒ **区分不出**，成本实测**每问多 8~11 token**（查询嵌入，命中 24h 缓存则不花钱）；本机开（cloud=3）、生产模板关（deploy=1），等 16 篇语料回来再判 |
| 91 | **`.html` 没有解析分支 + MinerU 偶发失败被错误信息掩盖（我为此误判两次）**。① `.html` 在白名单里却没有解析器 → 落到 `text` 引擎，**整行标签原样入库**（实测 `content` 就是 `<html><body><h1>交接班制度</h1>…`）。② PDF 解析失败时报的是 `MinerU 未产出可识别的结果（既无 content_list.json 也无 .md）`，真实原因（`PermissionError: … '/Users/…/.cache/modelscope'`、模型缺失、内存不足）**全被吞掉**；我据此先后误判为「mineru 没装」「模型不在」，实际 **MinerU 在 `var/mineru-venv`（2.0 G）、模型在 `var/mineru-models/models/PDF-Extract-Kit-1.0`（1.9 G，9-29 就在）**，代码本来就会自动发现并指向工作区缓存（`_mineru_env()`） | ①html 标签污染检索词项、白占 token；②PDF 的偶发失败（同批 16 份里出现过 2 次，重跑即好）**一次抖动就判整篇入库失败**，且报错信息把人引向错误方向（本次浪费了两次排查） | ① 新增 `_parse_html`（标准库 `HTMLParser`，零新依赖）：保留标题层级/列表/表格，`<head>` 整段丢弃，实体解码并把 NBSP 归一化成普通空格，表格进独立通道；`.html/.htm` 一起注册（能力清单 63→65、上传口径 11→12，四处钉死的用例同步）；② MinerU 加**重试**（`EKBS_MINERU_ATTEMPTS`，默认 2）并新增 `_subprocess_tail()`：**每次失败都把子进程输出尾部带进异常**，消息里还附「已尝试 N 次 / 模型缓存路径」；③ 修回被我误删的「只有 markdown 时回落」守卫；④ 新增 7 条用例（html 结构与表格、NBSP、`<head>` 丢弃、重试一次成功、失败信息含真实原因、退出码失败保留 stderr）；⑤ 清理我多复制的一层模型目录（`var/mineru-models/modelscope`），`var/mineru-models/models/` 为唯一权威位置 |
| 92 | **本地计数口径改用官方 SDK 自带词表，并把「计数口径」纳入校准绑定**。起因：被问"为什么不用 token-meter" —— 查证后它不是计数口径库（PyPI `smalldreamcollective-tokenmeter` 是**花销记账/预算**库：核心零依赖，"准确计数"要装 `[openai]`=**tiktoken**，支持模型只有 Anthropic/OpenAI，**要求 Python ≥3.12** 而本机是 3.11；用它等于退回实测偏差 −35% 的同源错配）。**顺带发现**：官方 `dashscope` SDK **随包自带** `resources/qwen.tiktoken`（2.5 MB，离线、零下载、与 SDK 同版本），`get_tokenizer()` 即可用 | ① 我们的计数口径本身没问题，但依赖一个部署物（`var/models/qwen-tokenizer.json`，11 MB）+ 一个依赖（`tokenizers`）+ 一次下载步骤，属于不必要的活动部件；② 更严重的是**校准只绑了嵌入指纹、没绑计数口径** —— 换词表（SDK/HF/tiktoken 回退）会改变 `local_tokens`，而旧系数会被**静默沿用**（不同口径实测差 1.69 倍），校准名不副实 | ① `count_tokens` 首选改为官方 SDK 自带词表（`EKBS_TOKENIZER_PATH` 仍是最高优先级；SDK 缺失时退 HF json → tiktoken 告警 → CJK 启发式），口径标识形如 `qwen:sdk:<path>`/`qwen:file:<path>`/`qwen:env:<path>`；② 实测两词表**计数逐条相同**（语料/英文/代码/表格/编号/emoji 共 7 组样本，462=462）而 `cl100k` 高 69% → 换口径无精度风险；③ `token_calib` 的 `record/summarize/load/calibrated_tokens` 全部增加 `counter`，**无口径字段的旧记录/旧系数在要求口径时一律失效**（与 `summarize` 同规则，宁严勿混），三处调用点（入库记录 / `/health/deps` / 校准工具）同步；④ 工具在"记录被排除"时打印可执行提示（"入库 ≥3 篇后重跑本工具即可重建系数"）；⑤ `requirements.txt` 与 `ekbs/tools/fetch_tokenizer` 标注为**可选路径**（想固定到 HF 词表时才用）；⑥ 新增 5 条用例（官方词表为默认 / env 仍最高优先 / 两词表一致 / 口径不符使校准失效 / 旧记录兼容语义）。**影响**：`/health/deps.token_calibration` 现为 `None` —— 这是**正确失效**（4 条旧记录既非当前嵌入变体、也无口径字段），入库 ≥3 篇后重跑工具即可恢复 |
| 93 | **待办：制度与数字的评估集还没建**（2026-10-22）。代码侧已收紧三处：条款不再按长度并进同一块；回答里的「第X条/章/节」必须出现在所引用块的标题路径里，对不上就拒答；取数禁止把「万/千/亿」换算成基本单位，展示原文那一串。库里当时已发布只有 3 份（115 新世纪表处理时间节点、117 2026年8月工作汇报、118 危险作业禁止行为），而且仍是旧切分；`docs/eval/golden_corpus16*.json` 对不上这批文件 | 没有对得上的题集，就不能给条款引用和数字原样卡门禁。拿旧的 16 份金标跑出来的 Hit@5 不能代表现在的库 | **待办，先不跑、不卡 0.92**。① 这 3 份按新规则重新解析后再抽题；② 三类各约 20 题：能答的、跨相邻条款的、原文里没有答案的；③ 检索仍用「标题 + 原文片段」看前 5 条，及格线维持现有的 0.8；④ 「条号对得上标题路径」和「数字与原文逐字一致」只记通过/不通过，不用模型打分 |

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
| 0 | ~~嵌入文本是否包含小节标题路径~~ ✅ **已定：包含**，并已写进本仓库实现规格 [`docs/spec/20`](docs/spec/20-ingest-chunking.md) 阶段 6（`chunker.retrieval_text()` = `heading + 正文`，含理由与影响面）。**`方案/06` §3.2 已于 2026-10-02 补上**（v3.0.1，含理由/实现约束/影响面）—— 该文档在**仓库外** `~/Documents/工作学习/AI项目/文档及说明/RAG维护文档/方案/`（`.gitignore` 把 `方案/` 排除，它是独立交付物，所以那次改动不进本仓库历史；改动前已备份到 `/tmp/06-数据治理与高级分片.orig.md`） | — |
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
   **入库方式（本轮起可以不用命令行）**：打开 `http://127.0.0.1:8000/admin` → 左栏「知识资产」→
   「目录导入」逐层选到目录 → 扫描预览 → 勾选导入（见 §四 第 70 条：旧单页的「导入」页签已被 SPA 顶掉，入口现在是这个）。文件放 `data_dir/raw`（开发机是 `var/raw`，
   生产是 `/data/ekbs/raw`），**支持中文多级子目录**；也可以走「上传文件」页签传少量文件。
   两条路的差别与边界见 `docs/测试方案-按工作流.md` §1.1。
5. **本机 launchd 演练（已可跑，建议每次改部署件后都跑）**：`bash scripts/deploy_rehearsal.sh`
   —— 用真实 launchd 拉起四服务、探活、测自愈、再拆掉（当前 **PASS=38 / FAIL=0**，api 自愈 6.7s）。
   ⚠️ 该脚本需要**未被沙箱限制的 shell**：受限环境下 `launchctl bootstrap` 会以
   `Bootstrap failed: 5: Input/output error` 失败（本机实测；同一命令在放开沙箱后成功）。
6. **目标机部署**：`deploy/scripts/install.sh` → `preflight.sh` → `smoke.sh`（需 Mac mini 与 `/opt`、`/data` 写权限）。
7. **钉钉接入**：拿到企业应用凭据后实现 Stream 客户端与免登（`04` §3）。
8. ~~需求①②③（本轮）~~ ✅ **已完成**：① 旧版 Office `.doc`（macOS `textutil`）/`.pptx`（`python-pptx`）/`.xls`（`xlrd`）兼容；
   ② 图片**不丢 + 可检索**（落盘持久 + `figure` 块入向量与 FTS + 可回源；无题注无 OCR 不落块）；
   ③ 代码知识库走**方案 C**（符号级分块 + 复用同一套权限与检索，见 `docs/代码知识库-方案C.md`）。
   顺带修掉两处跨层不一致：**扩展名白名单两张皮**、**「可检索」口径散落 8 处**（后者会让重建分片悄悄丢代码/图片向量）。
   **仍需你定**：`代码文件/` 目录（142MB `.pptx`、`.doc`、317 页中芯国际研报 PDF）是否纳入代码知识库；
   合规闸仍是 `corpus_egress_ack=false`，真实语料仍不能入库。
9. **私有远端 + 托管 CI（待办，2026-10-24 记录）**：仓库当前没有 `git remote` —— 代码与全部提交历史
   只有本机一份，这是比"没有 CI"更大的单点风险。建议顺序：
   ① 先加一个**私有远端**（GitHub private / GitLab / 自建 bare repo / NAS），哪怕只为备份；
   ② 之后再加一个 CI job 跑 `bash scripts/ci.sh`（与本地 CI、`acceptance.sh` 第 4 步同口径；
      ubuntu runner 即可，`deploy/` 下的 macOS 专属脚本不进 CI）。
   当前本机门禁已生效：pre-commit（commit=ruff+卫生，pre-push=全量 pytest）、`scripts/ci.sh`、
   `scripts/acceptance.sh`（第 4 步即本地 CI 门禁）。

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

---

## 八、回答后置核验 `answer_verify`（默认关闭）

背景：公开基准负例口径下，仅靠 `rerank.min_score=0.65` 拒答只有 **7.6%**；离线实验里
「claim 分解 + 逐条找原文」可把负例拒答提到 ~35–50%，代价是可答误拒 2–3%。该门禁把核验
做成**可配置、默认关闭**的生成后置开关。

### 行为

- 只在 `rerank` 分数落在 `[min_score, max_score]` 的**边界题**上调用（默认 0.65–0.85）：
  低分已由阈值拒答，高分直接放行，避免每题都多打一次判官；
- 判官用**显式模型名** `deepseek-flash` + `reasoning_effort: high`（公开基准选型，见
  `docs/公开基准评测报告-2026-10-25.md` §6.5）；不用 `deepseek-chat` 这类会随版本漂移的旧别名；
- 判官判定 `supported=false` → 返回 `refused=true`、`refusal_reason=answer_verify_unsupported`、
  清空引用（与现有拒答语义一致）；
- 判官不可用（缺 key / 超时 / 解析失败）按 `fail_open: true` **放行**，只记审计
  `answer_verify_refused`，门禁本身不制造不可用；`fail_open: false` 才按不可用拒答。

### 配置（三个配置文件已同步，默认 `enabled: false`）

```yaml
answer_verify:
  enabled: false            # 默认关闭；灰度时改 true
  provider: deepseek
  base_url: https://api.deepseek.com/v1
  model: deepseek-flash     # DeepSeek-V4.1-Flash
  key_env: DEEPSEEK_API_KEY # 密钥只从环境变量读
  reasoning_effort: high    # none=关思考，low/high/max
  max_tokens: 4000
  timeout_seconds: 180
  max_retries: 3
  min_score: 0.65
  max_score: 0.85
  max_contexts: 5
  max_context_chars: 900
  fail_open: true
```

### 启用步骤

1. 生产 `~/.ekbs/secrets.env` 里加 `DEEPSEEK_API_KEY=...`（`deploy/config/secrets.env.example`
   已有该键名）；launchd 重启服务；
2. 对应环境 `application.yaml` 里把 `answer_verify.enabled` 改为 `true`；
3. 先灰度观察：`refused_rate`、`answer_verify_refused` 审计事件、`answer_verify_failed` 日志；
4. 回滚：把 `enabled` 改回 `false` 即可（无需重启数据库/索引）。

### 测试

`tests/test_answer_verify.py` 覆盖：输出解析容错、429 重试、默认关闭、边界分过滤、
判官判定不支持时拒答、`fail_open` 开/关。全量 `pytest tests/ -q` 通过（990 用例）。

---

## 九、评测与灰度工具索引

| 内容 | 位置 |
|---|---|
| 公开基准评测报告（RGB / DuRetrieval / RAGAS / CRUD-RAG / 门禁回归） | `docs/公开基准评测报告-2026-10-25.md` |
| 评测脚本用法与复现命令 | `eval/README.md` |
| 指标核对（只读汇总各结果 JSON） | `eval/metrics_audit.py` |
| CRUD 全量收尾（补失败 → 基线 → 门禁 → 两次判分） | `eval/crud_full_run.sh` |
| 生产灰度步骤 | `deploy/RUNBOOK-回答核验灰度.md` |
| 生产灰度巡检（只读） | `deploy/scripts/answer_verify_gray_check.sh` |

> `answer_verify` 默认关闭；开启/回滚见 runbook，模型固定为 `deepseek-flash` +
> `reasoning_effort: high`（选型依据见评测报告 §6.5）。
