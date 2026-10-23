# AGENTS.md — EKBS 企业知识中枢

给 AI 编码助手（DSH / Cursor / Claude Code）的项目说明书。
**改代码前先读这份；改完后确认没破坏第 6 节的"硬约束"。**

---

## 1. 这是什么

部门级 RAG 知识库。Python 3.11 + FastAPI + SQLite + 无 Docker，用 launchd 托管。

```
接收文档 → 解析(MinerU/Office/文本) → 分块 → 向量化(DashScope) → 入库
                                                                  ↓
                          用户提问 → 权限过滤 → 检索(混合+alpha 融合) → rerank → LLM 生成
```

四个服务：`api`（FastAPI）、`worker`（解析队列）、`scheduler`（定时）、`dingtalk`（钉钉流）。

---

## 2. 目录地图

| 路径 | 内容 | 改动风险 |
|---|---|---|
| `ekbs/` | 主包，30+ 模块 | 高 |
| `ekbs/api/` | FastAPI 路由 | 中 |
| `ekbs/ingest/` | 文档解析入库流水线 | 高 |
| `ekbs/retrieval/` | 检索编排/融合（rerank 实现在 `ekbs/provider/`） | 高 |
| `ekbs/permission/` | 部门级权限过滤 | **极高**（红线） |
| `tests/` | 87 个 .py（86 个测试模块 + `conftest.py`），983 用例 | 只增不减 |
| `config/` | `application.dev.yaml`（mock）/ `application.cloud.yaml`（真实） | 高 |
| `deploy/` | launchd / 安装 / 回滚 / 双机切换 | **极高**（生产） |
| `docs/spec/` | 规格文档，编号 10-60 | 只读参考 |
| `scripts/` | 开发/部署脚本 | 中 |

**文档编号对照**：`10`=检索权限 · `20`=入库分块 · `30`=API · `40`=云端模型 · `50`=钉钉 · `60`=模型定价。

---

## 3. 常用命令

```bash
# 测试（不出网、不花钱，必须全绿再提交）
.venv/bin/python -m pytest tests/ -q

# 单个测试文件
.venv/bin/python -m pytest tests/test_retrieval.py -q

# 静态检查（ruff；配置在 pyproject.toml，先只开 E4/E7/E9/F）
.venv/bin/ruff check .

# 起四服务（开发机）
export EKBS_CONFIG=config/application.dev.yaml
bash scripts/dev_services.sh start all
bash scripts/dev_services.sh status
bash scripts/dev_services.sh logs api

# 只起 API（调试用）
.venv/bin/uvicorn ekbs.main:app --host 127.0.0.1 --port 8000 --workers 1

# 冒烟
bash scripts/dev_smoke.sh

# 管理台前端（改前端后必须重跑 build，服务不用重启）
cd ekbs-admin-web && npm install && npm run build && cd ..

# 上线接收检查（一次命令出 READY / NOT-READY 结论）
bash scripts/acceptance.sh
```

---

## 4. 编码约定

### 必须遵守

1. **依赖版本声明分三处、改动需同步** —— 运行时依赖以 `requirements.txt` 为准，测试依赖在
   `requirements-dev.txt`，生产完整清单是 `deploy/requirements.txt`（三者内容已有漂移）；
   `pyproject.toml` 的 `dependencies` 故意留空，避免重复声明两处漂移。

2. **配置从 `EKBS_CONFIG` 指向的 YAML 读** —— 代码不再硬编码阈值/模型名（pydantic 默认值仅兜底；
   `EKBS_*__*` 环境变量可覆盖）。`deploy/config/application.yaml` 是生产模板，其非环境键
   （模型/检索/解析/权限）应与 `config/application.cloud.yaml` 保持一致；**当前两文件已漂移**
   （如 `dense_multi_query` cloud=3/deploy=1、`figure_caption` true/false，cloud 独有
   `models.vision`/`multimodal_embed`/`query_rewrite`/`doc2query` 等），收敛前别声称"一致"。

3. **密钥从环境变量读，绝不写死** —— 生产在 `~/.ekbs/secrets.env`（权限必须 600），
   开发机已由 `dev_services.sh` 自动 source。缺 key 时报明确错误，不要静默降级。

4. **错误要带上下文** —— 抛异常时带上 `doc_id` / `shard_id` / 模型名 / HTTP 状态码，
   不要只抛 `Exception("failed")`。

5. **日志用结构化字段** —— 项目用 structlog 风格，如 `log.warning("parse_failed", doc_id=..., error=...)`，
   不要 f-string 拼字符串。

### 代码风格

- 类型注解：公开函数必须标注参数和返回值。
- 异步：`ekbs/api/` 与 `ekbs/ingest/pipeline.py` 是 async；同步的解析/分块（`parser.py` 等）
  由 `ekbs/worker.py` 用 `asyncio.to_thread` 调用，云端 embed/rerank 在 provider 层下沉线程。
- 注释：解释**为什么**，不解释**是什么**（现有代码就是这个风格，见 `pyproject.toml` 里的注释）。

---

## 5. 改动前必读的知识点

### 5.1 检索阈值是校准过的，别乱动

生产配置（cloud/deploy）的 `rerank.min_score = 0.65` 是用 16 份真实语料（28 正例 + 8 负例）实测校准的；
**dev 的 mock 配置是 0.30**（mock 精排量纲，代码默认值也是 0.30）。
重新校准方法：`python -m ekbs.tools.eval_switch_sweep`。
**改融合方式（如 `fusion: alpha`）后分数分布会变，必须重新校准**（README 发现 72 有完整数据）。

### 5.2 分词口径绑定模型

token 计数默认用 `dashscope` SDK 自带的 `qwen.tiktoken`（离线零下载；`EKBS_TOKENIZER_PATH`
可覆盖，取不到时依次退 HF 词表 → tiktoken 并告警）。
**换嵌入模型 → 分词口径和尺寸阈值都要重做**（README 发现 84/85）。
锚点：`ekbs_shard.embedding_fingerprint`，换模型它必然变。

### 5.3 两个配置的 data_dir 必须完全相同

这里指 `config/application.dev.yaml` 与 `config/application.cloud.yaml`：`data_dir`、`log_dir`、
`database.path` 必须同根。早先 cloud 配置把 `database.path` 指到 `var-cloud/index/`，导致 DB 与
raw/parse/分片分居两棵树，清一次 `var-cloud` 就起不来。**已对齐，别再改回分叉。**

### 5.4 MinerU 内存峰值

MinerU 进程内存峰值很高（`正式方案-v4/00-架构定稿与决策台账-v4.0.md` §5.2 容量预算 ~4.0 GB；
24 GB 机器上双 worker 双峰叠加实测耗尽内存）。
`parse.worker_concurrency = 1` 是硬约束（"同一数据库只有一个 worker 进程"）。**不要为了并发调大。**

---

## 6. 硬约束（改这些算破坏红线）

| # | 约束 | 为什么 |
|---|---|---|
| 1 | **无凭据不得谎报就绪**：问答接口无凭证必须 401；钉钉适配器 `/readyz` 缺凭据/未连接必须 503；API `/readyz` 公开（无凭据 200）、`/health/deps` 本机免凭据、远程须身份 | 无凭证红线：冒烟第 5 组（无凭证问答被拒）+ `test_adapter_app_reports_not_ready_honestly` 等用例 |
| 2 | **越权数据绝不出现在检索结果** | 权限红线，`tests/` 有专门用例 |
| 3 | **`parse.worker_concurrency` 保持 1** | MinerU 内存峰值叠加会打爆机器 |
| 4 | **`deploy/` 下的 launchd plist 改动需真人复核** | 影响生产自愈 |
| 5 | **测试套件必须全绿**（当前 983 用例） | 提交前跑 `pytest tests/ -q` |
| 6 | **不修改 `docs/spec/` 里的编号文档** | 它们是 `方案/` 的派生规格，不是需求源；有冲突以 `方案/`（现仓库为 `正式方案-v4/`）为准，改动先跟人确认 |

---

## 7. 已知陷阱

- **必须先 source secrets**：`dev_services.sh` 已自动处理；手工起服务时要 `set -a; source ~/.ekbs/secrets.env; set +a`。
  否则"生产能起、dev 起不来"。
- **管理台改前端要重跑 build**：`npm run build`，服务不用重启。
- **`.venv` 是 Python 3.11，不是 3.12**：`token-meter` 之类要求 3.12+ 的库用不了（README 发现 92）。
- **`pyproject.toml` 打包注释**：`packages.find` 的注释解释了为什么不能只写 `packages = ["ekbs"]`
  （非 editable 安装/打包会漏 10 个子包）。
- **`ekbs-admin-web/node_modules` 很大**：git 已忽略，不要 `git add`。

---

## 8. 给 AI 的工作方式

### 改代码前

1. 读相关模块 + 对应 `docs/spec/` 编号文档。
2. 在 `tests/` 找现有用例，理解契约。
3. 确认改动是否触碰第 6 节硬约束。

### 改代码时

- **小步**：一次一个函数/一个接口/一个 bug。
- **先写测试**：新增行为 → 先在 `tests/` 加用例，再改实现。
- **保持现有风格**：注释写"为什么"，错误带上下文，日志结构化。

### 改代码后

```bash
.venv/bin/python -m pytest tests/ -q      # 必须全绿
.venv/bin/ruff check .                    # 静态检查（E4/E7/E9/F）
git diff --stat                           # 检查改动范围
```

- 如果改了 `config/`，同步检查 `deploy/config/application.yaml`。
- 如果改了检索/权限，跑 `bash scripts/dev_smoke.sh --with-qa --redline`（两条红线：无凭证问答 401、
  越权检索不泄漏；不带 `--redline` 时只跑前者）。
- commit message 用 `feat(scope): 描述` / `fix(scope): 描述`，正文写清"起因/改动/影响"。

### 不要做

- ❌ 不要一次改 5 个文件说"重构完成"
- ❌ 不要跳过测试直接提交
- ❌ 不要在 `ekbs/permission/` 里"优化"逻辑而不加测试
- ❌ 不要把密钥、token、内网地址写进代码或文档
- ❌ 不要改 `docs/spec/` 的编号文档（派生规格，需求源是 `方案/`）

---

## 9. 文档地图

| 想了解 | 看 |
|---|---|
| 项目全貌 / 已实现与未实现 | `README.md`（110KB，含 94 条编号"发现"：1–93 + 43b；另 §四之二 5 条钉钉发现） |
| 检索与权限设计 | `docs/spec/10-retrieval-permission.md` |
| 入库与分块 | `docs/spec/20-ingest-chunking.md` |
| API 契约 | `docs/spec/30-api.md` |
| 云端模型配置 | `docs/spec/40-cloud-models.md` |
| 钉钉接入 | `docs/spec/50-dingtalk.md` |
| 模型定价 | `docs/spec/60-model-pricing.md` |
| 怎么起服务（真实云端） | `docs/正式路径-怎么起.md` |
| 部署 / 双机切换 | `deploy/README.md`、`deploy/RUNBOOK-双机切换.md` |
| 测试怎么组织 | `docs/测试方案-按工作流.md` |

---

*最后更新：2026-10-24 · 由 DSH 根据项目实际状态生成，同日复核并据实修正*
