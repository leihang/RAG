# 代码知识库（方案 C：复用主链路 + 代码感知分块）

> 需求原话：「如果代码文件夹的相关文件不被解析，如 ragflow 方案的项目团队的代码知识库是如何管理的？」
> 结论：**把代码当作一类普通文档导入**，复用解析 → 分块 → 索引 → 权限 → 检索**全链路**，
> 只在「分块」这一步换成代码感知切分。不引入独立的代码索引、调用图数据库或 LSP 服务。

---

## 1. 为什么选 C（而不是独立的代码知识库）

| 方案 | 做法 | 代价 | 结论 |
|---|---|---|---|
| A. 不解析 | 代码文件夹整体排除 | 团队最该被问到的资产缺席 | ✗ |
| B. 独立代码索引 | 另起一套 embedding + 图数据库 + 代码搜索服务 | 第二套权限、第二套检索口径、第二套运维；**C1/C5 红线要在两处各实现一遍** | ✗ |
| **C. 复用主链路**（本方案） | 代码走现有 `parse_file` → `chunk_code` → 同一批分片/FTS | 要写一个代码感知分块器（已完成） | ✓ |

选 C 的三个理由：

1. **权限红线只有一处**：代码文件同样落 `kb_*_L*` 分片，同样受 C1（无权分片不参与计算）
   与 C5（文档 ACL 闸门）约束。独立索引意味着这些红线要在新系统里再实现一遍，
   而「漏实现一次」就等于泄密。
2. **代码与制度能一起被问到**：「这个接口的鉴权规则，制度文档怎么规定的？」
   这类问题需要同时召回两类语料，只有同一套 RRF 融合才做得到。
3. **运维成本为零**：不多一个进程、不多一个数据库、不多一份备份。

C 的已知代价（明确接受）：**不提供「跳转到定义」与跨文件调用图**。
本系统的定位是「问答」，不是 IDE；需要符号跳转时用 IDE 本身。

---

## 2. 怎么导入

```bash
# 单仓库（默认白名单已含 51 种代码后缀，不用写 --ext）
.venv/bin/python -m ekbs.tools.import \
  --dir /path/to/repo --kb-code code --level 2 --dept-ids 102 \
  --parallel 2 --json

# 先看会导哪些文件（不写库）
.venv/bin/python -m ekbs.tools.import --dir /path/to/repo --kb-code code --level 2 --dry-run
```

要点：

* **`source_path` 必须是仓库相对路径**（如 `ekbs/ingest/pipeline.py`）。它会被原样写进
  每个块的 `heading_path`；传绝对路径会让同一份代码在不同机器上产生不同的检索语义。
* 代码没有页码，`page_from/page_to` 一律为 `NULL`（**不伪造**）。
* 去重是**全局按内容 checksum**：同一个文件已在任何 kb/密级下导入过，就不会再导第二次。
  需要多部门可见请用 `--dept-ids a,b`，而不是重复导入。
* 二进制内容不在白名单内（`.pyc`、`.so`、`.jar` 等），不会被当成文本读进来。

---

## 3. 切分规则（`ekbs/ingest/code_chunker.py`）

**核心口径：一个符号 = 一个检索单元。** 定长切分会把函数从中间劈开，检索效果很差；
符号级切分让召回结果直接是「哪个类、哪个方法」。

| 语言 | 切分方式 | `heading_path` |
|---|---|---|
| **Python** | `ast` 真解析 | `路径 > Class.method`；顶层 `def` 为 `路径 > func`；模块级零散语句聚成 `路径 > <module>` |
| **其它 50 种后缀** | 启发式（缩进为 0 且行首形如 `function`/`class`/`func`/`fn`/`CREATE`/`public` …） | 认出的符号名进路径 |
| **认不出边界** | 带重叠的定长行窗口（**全文不丢**，允许重叠） | 只保证含 `路径` |

超长符号（默认 `max_tokens=384`，入库时取自 `CHILD_MAX_TOKENS`）：

```
ekbs/ingest/pipeline.py > IngestPipeline            ← 父容器（3188 tok）：不进向量，仅供上下文扩展
ekbs/ingest/pipeline.py > IngestPipeline#1 … #10    ← 子块（约 330 tok）：进向量 + 进 FTS
```

短符号（如 `IngestResult.as_dict`）**自身就是检索单元**，没有子块。

> ⚠️ 这里与 Markdown 的形态不同，别按 Markdown 的直觉去查库：
> Markdown 的检索单元是 `child`、父块是容器；**代码的检索单元可能是 `parent`**。
> 统一判据（叶子块）实现在 `ekbs/ingest/retrievable.py`，
> 别在别处再写一遍 `chunk_type='child'`。

护栏：除「被切分的符号父容器」（必须留全文供扩展）外，任何块都 ≤ `max_tokens × 1.5`；
`content` 一律非空白且 `token_count > 0`。27 条用例见 `tests/test_code_chunker.py`。

---

## 4. 实测（真实仓源码，非构造样本）

导入本仓 `ekbs/ingest/` 目录（6 个 `.py`）：

| 文件 | 分块数 | 入向量 |
|---|---|---|
| `__init__.py` | 1 | 1 |
| `chunker.py` | 21 | 17 |
| `code_chunker.py` | 30 | 27 |
| `parser.py` | 31 | 27 |
| `pipeline.py` | 32 | 30 |
| `retrievable.py` | 5 | 4 |

三方对账（可检索叶子块 / FTS 行数 / 分片登记向量）**138 = 138 = 138**，
`job_reconcile` 报 `diff_ratio=0.0`。

检索实测（离线 mock provider，避免出网）：

```
QUERY='OrderService submit 提交订单'
   [parent] demo/service.py > OrderService          ← 排名 1
   [parent] demo/service.py > OrderService.submit   ← 排名 2
   [parent] 员工手册（总则） > 第一章 行为规范        ← 制度文档（同一套融合里一起召回）
```

用只存在于代码里的符号名提问，召回结果排在最前 —— 说明代码库不是「建了但搜不到」。

---

## 5. 与主链路的一致性要求（改代码前必读）

「谁进向量 / 谁进 FTS / 谁能被检索」这条规则**只有一处定义**：
`ekbs/ingest/retrievable.py` 的 `RETRIEVABLE_PREDICATE` / `retrievable_chunk_ids()`。

必须引用它的 6 个模块：入库编排、重建分片、批量导入统计、权限换级重嵌、三方对账、发布闸门。
原因：这条规则曾分散写成 `chunk_type = 'child'`，结果**重建分片一次就会悄悄丢掉代码与图片的向量**
（代码符号块是 `parent`，被过滤掉了），而库里看不出异常。

两道防复发用例（`tests/test_ingest_code_and_figures.py`）：

* `test_no_module_redefines_what_is_retrievable` —— 除白名单外，任何模块出现
  `chunk_type='child'` 作为过滤条件即失败（白名单条目必须写明「为什么它不是重复定义」）；
* `test_every_retrieval_consumer_uses_the_single_source` —— 上述 6 个模块必须引用同一口径。

---

## 6. 已知限制（诚实清单）

| 限制 | 说明 | 影响 |
|---|---|---|
| 无符号跳转 / 调用图 | 方案 C 的定位是问答，不是 IDE | 需要跳转定义时用 IDE |
| **管理台分块编辑只支持 `child`** | `ekbs/api/chunks.py` 的拆分/合并/编辑限定 `child` | 代码符号块（短符号是 `parent`）暂不支持在线编辑；需改动请改源文件后重导 |
| 启发式语言可能认错边界 | 非 Python 走缩进+关键字启发式 | 只影响块边界，不影响「全文不丢」；Python 走 AST 无此问题 |
| 单文件超长类会切成很多子块 | 如 3188 tok 的类 → 1 容器 + 10 子块 | 检索更精确，但同文件的块数会明显多于普通文档 |
| 代码不参与 `page_from/page_to` | 代码无页码 | 引用卡片不显示页码，属正常 |
| 视频/音频仍不支持 | 与代码库无关，见 README 的诚实清单 | — |
