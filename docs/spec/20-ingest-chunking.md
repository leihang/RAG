# 20 · EKBS 入库与分块实现规格（清洗 / 分块 / 分片 / 索引 / FTS5）
> 范围：从 `方案/` 提炼**入库链路**上可直接照着编码的参数、算法与落库规则。
> 文档优先级：以 `00-架构定稿与决策台账.md` 为准（来源：00 §9.2）；Schema 以 `01` §5 为唯一来源（来源：01 §5）。
> 本文只提炼，不新增口径；文档互相矛盾或不足以直接编码之处逐条列入 §9，**不在本文拍板**。

## 1. 入库流水线
### 1.1 步骤顺序（唯一顺序，不可调换）
```
受理 → 解析(MinerU) → 清洗 10 步 → 表格序列化 → 四层分块 → 云端嵌入 → 写索引(FAISS+SQLite+FTS5) → 质量门禁 → 发布
```
（来源：01 §12）

| # | 步骤 | 输入 | 输出 / 落库 | 失败处理 |
|---|---|---|---|---|
| 1 | 受理 | 钉钉文件消息 / 批量导入目录 | `/data/ekbs/raw/<sha前2位>/<sha>/原文件` + `ekbs_document(status='pending')` | **拒绝时不落盘**：扩展名 ∩ MIME 双重白名单（来源：04 §5.9、06 §1.7）；`checksum` 命中已有非 `offline` 记录则复用 `doc_id`、不新建不重嵌（来源：04 §5.9）；上传者 `clearance < kb.default_level` 拒绝（来源：04 §5.9）；`source_path` 必须在 `/data/ekbs/raw/` 内（来源：06 §1.4）；必填字段缺失拒绝并指出**具体字段名**（来源：02 TC-GOV-007） |
| 2 | 解析 | `raw` 原文件 | `parse/<sha>/` 下 Markdown + `structure.json` + images（来源：01 §1） | `status='failed'` + `ekbs_sync_log` 失败记录 + 人工复核入口（来源：02 TC-GOV-012）；指数退避重试 ≤3 次，成功后回 `published`（来源：02 TC-PARSE-020）；`E_DOC_ENCRYPTED`/`E_DOC_CORRUPT` 不重试（来源：02 TC-PARSE-016/017） |
| 3 | 清洗 10 步 | 解析产物全文 + 结构 | 规范化文本 + 图表占位 + PII 标记（**不改原文**） | 单篇失败 `failed`；空页率/乱码率超线整篇退回人工复核；单表失败只跳过该表并记录（来源：06 §1.7） |
| 4 | 表格序列化 | `structure.json` 的表格对象 + 前后相邻结构位置 | 每个表格的 `information_block` 换行拼接文本 | 单表失败跳过并记录（来源：06 §1.7）；不用页码取上下文（来源：06 §4.3） |
| 5 | 四层分块 | 清洗后文本 + 表格/图占位 | `ekbs_chunk` 行（`parent`/`child`/`table`/`figure`） | 不产生空块、不产生只有标点的块（来源：02 TC-INDEX-029）；两次入库 `seq` 与顺序必须完全一致（来源：02 TC-INDEX-033） |
| 6 | 云端嵌入 | **标题路径 + 正文**（批量）—— 唯一口径 `chunker.retrieval_text()` | 向量 + `ekbs_chunk.embedding_fingerprint` | 入库期失败 → 排队重试 + 退避，**不影响在线查询**（来源：00 §2.3）；维度 1024、L2 归一化、无 NaN（来源：06 §1.2 阶段 4）；`batch_size=20`、`max_retries=3`（来源：01 §7） |
| 7 | 写索引 | 向量 + chunk 行 | FAISS 分片文件 + `ekbs_chunk.faiss_row` + `ekbs_chunk_fts` 行 + `ekbs_shard.vector_count` | `ekbs_chunk` 与 `ekbs_chunk_fts` **必须同一事务**（来源：02 TC-INDEX-015）；SQLite 每 200 行提交一次（来源：02 TC-INDEX-027）；FAISS 先写 `.new` 再原子 rename（来源：06 §2.4） |
| 8 | 门禁 + 发布 | 全部分片与元数据 | `status='published'` | `ntotal == vector_count`（来源：02 TC-INDEX-013）；三方一致性（来源：03 §5.4）；PII 命中阻断 `published`（来源：02 TC-GOV-028）；解析成功率 ≥98%、空页率 ≤1%、乱码率 ≤0.5%、表格还原率 ≥95%、页码连续性 100%（来源：01 §12、06 §1.7） |

**阶段 6 的口径（2026-10-02 定稿：包含小节标题路径）**

嵌入文本 = ``chunker.retrieval_text(heading_path, content)`` = ``f"{heading}\n{body}"``，
即**小节标题路径 + 正文**，两段用换行连接；``ekbs_chunk.content`` 本身不变（正文照旧）。
入库（`ekbs/ingest/pipeline.py`）、分片迁移（`ekbs/permission/changes.py`）、
索引重建（`ekbs/tools/rebuild_shard.py`）三处都调用同一个函数 —— **口径必须唯一**，
任何一处不同都会让同一分片里混进两套向量语义。

为什么必须带上标题：制度/合同类文件的关键限定词常常只在小节标题里（如「第二条 **住宿标准**」，
正文写的是「一线城市每晚不超过 600 元…」）。只嵌入正文时，查询「住宿标准」与这段正文
**没有任何共享词**，稠密召回直接落空（历史实测踩到）。FTS 侧另有 `heading_path_tok` 列
（见 §FTS 一节），两路一致。

影响面：**改这个口径必须全量重嵌**（只重建分片、不重新解析）。此前 `06` §3 未规定该口径，
本行即为本仓库的定稿；方案侧文档需同步一句同样的话。

### 1.2 `ekbs_document.status` 状态机
取值（Schema 唯一来源）：`pending | parsing | indexing | published | failed | offline`（来源：01 §5.2）

```
pending ──受理完成──> parsing ──解析+清洗通过──> indexing ──写入+门禁通过──> published
   │                     │                          │
   │                     └──解析失败──────────────> failed
   └──元数据/合规闸拒绝──> failed（不产生任何 chunk 与分片）
published ──新版本入库──> offline（旧行，数据保留）
published ──expire_date 到期（每日巡检）──> offline（数据保留，可追溯）
failed ──人工复核/重试成功──> published
```
硬规则：只允许上述跃迁、不存在跳级、每步更新 `updated_at`（来源：02 TC-GOV-011）；仅 `published` 可被检索，`pending`/`parsing`/`offline` 一律不可召回（来源：02 TC-INDEX-019）；`offline` **不删除数据**（来源：06 §1.5）。`deleted`/`superseded` 的冲突见 T1。

## 2. 清洗 10 步（解析后、分块前；**全部确定性规则，不调用 LLM**）
| # | 步骤 | 做什么 |
|---|---|---|
| 1 | 页眉页脚剔除 | 删除在所有页重复出现的首尾行；页码信息保留到 `page_from/page_to` |
| 2 | 水印与印章 | 不进正文文本；仅记录所在页与位置，供回源展示 |
| 3 | 断行合并 | 行尾非句末标点且下一行不以序号/缩进开头 → 合并为一段 |
| 4 | 表格结构还原与跨页合并 | 由 `structure.json` 合并续表与主表，补出空表头语义（如「合计」） |
| 5 | 条款编号规范化 | 「第 三 条」→「第三条」；全角数字转半角；`3.2`/`3、2`/`3．2` 统一为 `3.2` |
| 6 | 单位与币种归一 | 元/万元/亿元统一标注；百分比统一 `xx%`；金额千分位统一 |
| 7 | 空白与乱码清洗 | 连续空行压为一段；删除 `U+FFFD` 与孤立控制字符并计数 |
| 8 | 目录/索引页剔除 | 排除在分块之外，原文保留供回源 |
| 9 | 图表占位标注 | 原位置插入占位标记（含图号、图注、所在页），并落 `figure_id`、`image_path` |
| 10 | PII 打标 | 手机号/身份证/银行卡号**打标不删除**（标记位置与类型），供准入判定与输出侧脱敏 |

（来源：06 §1.7）准出门槛（写入 `status` 前必须全过）：解析成功率 ≥98%；空页率 ≤1%；乱码率（`U+FFFD` 占比）≤0.5%；表格还原率 ≥95%；页码连续性 100%（缺失区间写 `WARN`，不阻断）；第 5/6 步规则命中率记录并观察（来源：06 §1.7）。规则集必须版本化：写入 `application.yaml` 的 `parse.clean_rules_version`，与 `INDEX-REG v1` 一起回归（来源：06 §1.7）。不在此处做语义改写、同义替换、摘要（来源：06 §1.7）。

## 3. 分块策略
### 3.1 四层结构与尺寸基线
| 层 | `chunk_type` | 是否入索引 | token | 备注 |
|---|---|---|---|---|
| L0 文档 | — | 否 | — | 只有元数据，是权限/版本/生效期的载体 |
| L1 章节 | `parent` | 是（父块） | 1024 ~ 1536 | 父块必须覆盖其全部子块 |
| L2 条款块 | `child` | 是（主力检索单元） | 256 ~ 384（±5 token 容差） | 相邻子块重叠 **12% ~ 15%**（样本偏差 ≤1%） |
| L3 结构化块 | `table` / `figure` | 是 | 表格**不设子块上限**，整块 | 不参与文本层切分 |

（来源：06 §3.2、02 TC-INDEX-001/002/003）token 用本地 `tiktoken` 近似计数，但必须先按 TC-INDEX-004 校准：与云端 `text-embedding-v4` 的 `usage.total_tokens` 偏差 ≤5%，否则先调计数口径（来源：02 TC-INDEX-004、06 §3.2）。子块上限取 384（而非 512）的附带理由：天然落在 rerank `max_length=512` 以内（来源：06 §3.6 #12）。

### 3.2 切分算法（对每个 L1 章节；前一步能定边界就不进下一步）
```python
def chunk_section(section, md_structure):
    # 规则1 结构优先：按 MinerU 的标题层级，遇一级/二级标题即开新 L1 章节。
    #        不以 '# ' 或「一、」「1.」等字符串前缀猜（PDF 抽文没有这些标记会整体退化）。
    blocks   = section.blocks                              # 清洗第 9 步已把表格/图片替换为占位标记
    tables, figures, texts = split_placeholders(blocks)    # 规则5：表格/图片独立成 L3 块

    units = find_clause_units(texts) or []                 # 规则2 条款优先：第X条/第X章/X.X → 每条款一子块
    if not units:
        units = aggregate_by_sentence(texts, target=CHILD_TARGET)   # 规则3 句子聚合：。！？；聚合到 target
    # 规则4 长度兜底：仍超上限按固定长度切，向前回退到最近句末符（回退窗口 100 字符）
    children = [len_fallback(u, hard_max=384, backoff_chars=100) for u in units]
    children = apply_overlap(children, ratio=0.135)        # 12%~15% 区间内；实现方式见 T7
    parent   = pack_parent(children, target=(1024, 1536))  # 父块含 children 全文
    return parent, children, tables, figures
```
（来源：06 §3.3；回退窗口 100 字符见 06 §3.3-4）

### 3.3 父子关系落库（`ekbs_chunk`）
| 字段 | 写入规则 |
|---|---|
| `parent_id` | `child`/`table`/`figure` 行指向**同文档** L1 `parent` 行的 `chunk_id`；引用完整性 100%，无孤儿（来源：02 TC-INDEX-010、06 §3.2） |
| `seq` | 文档内自增，按切分确定性顺序；同一文档两次入库必须完全一致（来源：02 TC-INDEX-033） |
| `heading_path` | 每个 `child` 必须注入，格式 `办法名>章>条`；缺标题时以文件名作根，不得为空或 `None`（来源：06 §3.3、02 TC-INDEX-009） |
| `page_from` / `page_to` | 该块覆盖的原文页码区间；页码来自 MinerU 结构化输出，**禁止 `text.find(chunk[:100])` 反查**（来源：06 §3.6 #9）；表格块取表格页码区间（来源：06 §4.4） |
| `shard_id` | 同文档全部 chunk 同值，规则见 §4 |
| `token_count` | 本地计数（经 §3.1 校准） |
| `chunk_type` | `parent | child | table | figure | summary`（Schema 枚举，来源：01 §5.2） |

### 3.4 表格块（`chunk_type='table'`）
- **整表一个 chunk，不切分**；`content` = 各 `information_block` 用换行拼接（来源：06 §4.4）。
- 序列化输出契约：`subject_core_entity`（核心实体，通常来自行表头）、`information_block`（自解释整段，须含全部相关表头、单位及其说明、表名/脚注/币种/金额呈现方式）、`subject_core_entities_list`（含空表头的语义补全，如「合计」）、`relevant_headers_list`（来源：06 §4.2）。
- `heading_path` = 所在章节路径 + 表名；`figure_id` = `表 <n>`（来源：06 §4.4、06 §4.11.1）。
- 序列化结果 >2048 token 时按行分组输出多个块（`subject_core_entities_list` 本就是多实体设计）（来源：06 §4.4）。
- 序列化用与答案生成同一云端 LLM 网关；**必须补 2~3 个真实制度表 few-shot 示例**并纳入 Prompt 模板管理（来源：06 §4.2、06 §4.10）。
- 表格若是扫描图，`image_path` 存其裁剪图，否则为 NULL（来源：06 §4.11.1）。
- 数值口径：百分比归一、元↔万元↔亿元换算、括号表负数、跨币种返 `N/A`、**不允许推算**（来源：06 §4.5、06 §4.6）。

### 3.5 图片块（`chunk_type='figure'`）
| 来源 | `content`（进向量 + FTS5） | `image_path` | `figure_id` |
|---|---|---|---|
| 文档内含图片 | 图注 + 图内 OCR 文字 + `heading_path` | `parse/<sha>/images/<name>` | 从图注抽「图 1」/「Figure 1」，抽不到用 `fig-<seq>` |
| 独立图片文件 | 同一条消息的文本描述（若有）+ OCR 文字 | 原图存 `raw`，解析产物存 `parse` | 文件名去扩展名 |

（来源：06 §4.11.1）独立图片入库硬规则（来源：06 §4.11.2）：

```
1. 扩展名白名单 ∩ MIME 白名单双重校验（含 png/jpg/jpeg、image/png、image/jpeg）
2. 描述来源优先级（取到即止）：① 同消息 richText 文本 ② 文件名去扩展名 ③ 仅 OCR
3. 解析：MinerU 对纯图片的支持待 POC（00 §11 M12）；不支持则回落 PaddleOCR PP-OCRv4
4. 判定（硬）：可检索文本 = len(描述) + len(OCR 文本)
     ≥ 30 字 → 正常入库 status='published'
     <  30 字 → status='failed'，error='E_IMAGE_NO_TEXT'；不产生 figure 块、不进向量库、不进 FTS5
5. 原图保留在 raw/<sha>/；解析产物副本落 parse/<sha>/images/
```
图片**不以像素进向量库**（来源：06 §3.5、06 §4.10）；`image_path` 只存相对 `data_dir` 的**相对路径**，不存绝对路径（来源：01 §5.2）；回源端点 `GET /v1/assets/figure/{chunk_id}` 必须按 `shard_id` 做 ACL 复检、越权返 404、断言最终路径落在 `/data/ekbs/parse/` 内，且禁止 Caddy 静态托管 `parse` 目录（来源：06 §4.11.3、00 §2.2）。

## 4. 分片归属规则
### 4.1 判定算法（必须确定性）
```python
def resolve_shard(doc, cfg) -> str | Reject:
    level = int(doc.level)                        # 1..4
    vd    = json.loads(doc.visible_dept_ids)      # 空数组 = 不限部门（即 L1 公共）
    if level == 4 and not cfg.permission.l4_enabled:
        return Reject("L4_DISABLED")              # 不产生任何 chunk 与分片
    if not vd:
        if level != 1:
            return Reject("PUBLIC_SHARD_MUST_BE_L1")   # 空数组 + level>1 属配置错误
        return "kb_all_L1"
    dept_code = dept_code_of(vd[0])               # 归属主部门 = 第一个；其余部门靠 ACL
    return f"kb_{dept_code}_L{level}"
```
（来源：06 §2.2）

- 命名正则：`^kb_[a-z0-9_]{2,16}_L[1-4]$`；`dept_code` 为小写英文或拼音缩写，**不使用中文**；公共分片 `dept_code='all'`（来源：00 §3.1、02 TC-GOV-010）。首期 L4 不产生分片（来源：06 §1.1、00 D11）。
- **多部门文档向量只存一份**，落第一个可见部门的分片；其余可见部门通过 `ekbs_acl_entry`（`object_type='shard'`、`subject_type='dept'`、`effect='allow'`）授予（来源：06 §2.2、02 TC-GOV-006）。`deny` 优先于 `allow`，与 `03` §4.2 判定顺序一致（来源：06 §2.5）；ACL 变更必须在同一事务 `bump_policy_version()`（来源：06 §2.6、03 §4.2）。
- 分片按**一级或二级部门**划分，三级部门的可见性差异用 ACL 表达（来源：06 §2.4）；`dept_code ↔ ekbs_dept.dept_id` 映射暂定放配置文件，见 O6-1（来源：06 §1.4、06 §7.3）。

### 4.2 `ekbs_shard` 与 `ekbs_chunk.shard_id` 的写入
```python
async def ensure_shard(shard_id, dept_code, level):      # lazy：不预建笛卡尔积
    if SELECT 1 FROM ekbs_shard WHERE shard_id = ? is None:
        new_shard(shard_id, dim=1024)                    # = python -m ekbs.tools.new_shard --shard-id .. --dim 1024
        INSERT ekbs_shard(shard_id, dept_code, level, faiss_path, dim=1024,
                          embedding_fingerprint=<当前指纹>, vector_count=0,
                          index_type='flat', status='active')
    UPDATE ekbs_chunk SET shard_id = :shard_id WHERE doc_id = :doc_id
```
（来源：06 §2.4、01 §6.3、01 §12）

- `ekbs_shard` 字段：`dept_code`（`all` 或部门码，与 `shard_id` 中段一致）、`level`（1~4）、`faiss_path`、`dim`（1024）、`embedding_fingerprint`（该分片全部向量的模型指纹）、`vector_count`、`index_type`（`flat`/`ivf`）、`status`（`active`/`building`/`retired`）（来源：01 §5.2）。
- 创建时机：首次有文档落入该 `(dept, level)`（来源：06 §2.4）；分片数 >100 写 `ekbs_security_event` 并告警（来源：06 §2.4、02 TC-SHARD-011）；`vector_count=0` 且连续 30 天无新增 → `retired`，退役需 `kb_admin` 确认（来源：06 §2.4）；`retired` 不参与路由（来源：02 TC-SHARD-013）。

### 4.3 密级 / 部门变更 = 迁移分片（不可只改字段）
```
密级 L2→L3（同一事务，用同一个 policy_version 提交）：
  ① UPDATE ekbs_document.level
  ② 该文档全部 chunk 迁到 kb_<dept>_L3：旧分片删除对应向量；新分片追加向量并重排 faiss_row
  ③ bump_policy_version()
  ④ ekbs_perm_change_log 落记录：operator_id / object_type='document' / change_type='level'
     / field / old_value / new_value / policy_version_before|after / reason
```
（来源：06 §1.5、06 §5.2 硬规则、03 §4.2、02 TC-GOV-014/015/018）

只改 `ekbs_document.level` 而不迁移向量 = 真实越权（L3 内容继续躺在 L2 分片被召回）（来源：06 §1.5）。`visible_dept_ids` 变更：更新字段，若归属分片随之变化则重建分片；权限指纹变化使缓存自然隔离（来源：06 §1.5）。部门撤销：从 `visible_dept_ids` 移除，分片归属不变但路由不再可见，≤5 分钟生效（来源：06 §1.5、02 TC-GOV-017）。

## 5. FAISS 索引的建立与维护
### 5.1 索引类型、度量与调用
| 项 | 取值 | 依据 |
|---|---|---|
| `index_type` | **`flat`（`IndexFlatIP`）**；扩容时才评估 `ivf` | 10 万 chunk ≈ 409.6 MB（`N×1024×4B`）；精确检索、无需训练、无参数可调、`add`/`search` 行为确定（来源：06 §2.3） |
| 度量 | **内积**；写入前与查询前都执行 `faiss.normalize_L2`，归一化后内积即余弦 | 来源：00 §2.1、06 §3.6 #1 |
| 维度 | 1024；`ekbs_shard.dim` 与 `models.embed.dim` 必须一致 | 来源：00 §2.1、01 §7 |
| 调用方式 | `idx.search(query_vec, min(30, idx.ntotal))` 必须包在 `asyncio.to_thread`（同步 CPU 密集 C 调用，`--workers 1` 下会阻塞整个事件循环） | 来源：06 §2.3、03 §5.2 |
| 逐分片检索 | 只遍历可见分片；某分片未加载 → 跳过并告警，不报错 | 来源：03 §5.2 |

换 `ivf` 的触发条件（任一命中即重新评估）：总 chunk >50 万（`nlist=√N`，`nprobe` 从 8 起调）；分片检索 P95 >300ms（实测）；常驻内存 >6.8 GB；分片数 >100（来源：06 §2.3）。

### 5.2 `faiss_row` 与新增文档的追加
```python
async def append_to_shard(shard_id, chunk_ids, vectors):     # vectors 已 normalize_L2
    index = shard_cache[shard_id]
    start = index.ntotal
    index.add(vectors)                                       # 行号 = 追加顺序，从 start 起连续
    for offset, cid in enumerate(chunk_ids):
        UPDATE ekbs_chunk SET faiss_row = :start + :offset WHERE chunk_id = :cid
    write_index_atomic(index, f"{shard_id}.faiss")           # 先写 <shard>.faiss.new，再原子 rename
    UPDATE ekbs_shard SET vector_count = :index.ntotal, updated_at = datetime('now')
```

- `faiss_row` 就是该 chunk 向量在分片中的**行号**（追加顺序连续分配；不用列表下标、不用自增主键）（来源：06 §3.6 #13、01 §5.2）。
- 可反查：用 `faiss_row` 取回向量与该 chunk 重新 embed 的结果余弦 ≥0.999，且无越界（来源：02 TC-INDEX-014）。
- 不变式：`index.ntotal == ekbs_shard.vector_count == COUNT(ekbs_chunk WHERE shard_id=?)`，差异必须为 0（来源：02 TC-INDEX-013、03 §5.4）；`faiss_row_invalid` → 从 SQLite 重建该分片（来源：03 §5.4）。
- 分片只从 `ekbs_shard.faiss_path` 白名单路径加载，**不用 pickle 承载索引元数据**（来源：06 §3.6 #10）。

### 5.3 重建（C4「旋转而非重建」）与换模型迁移
```
1. 读该分片下全部 ekbs_chunk.content（C4 保证原文永远在 SQLite）
2. 重新分块（分块策略变了）或重新嵌入（模型变了）——**不重新解析原始文档**
3. 写 <shard>.faiss.new（不触碰正在服务的文件）
4. os.replace(<shard>.faiss.new, <shard>.faiss)      # rename 只换目录项，旧 inode 仍被进程引用
5. POST http://127.0.0.1:8000/internal/shard/reload  # 单分片重载 ≤2s
6. UPDATE ekbs_shard.vector_count / embedding_fingerprint / faiss_path
```
（来源：06 §2.4）零停机原理：重载完成前检索一直用旧内存副本（不必双写、不必别名切换）；重载那一刻若新索引未就绪，该分片**降级为不参与本次检索并告警**（宁可少召回，不可出错）（来源：06 §2.4）。中断恢复：未完成的 `.new` 被识别并清理，正式分片文件不被破坏（来源：02 TC-INDEX-024）。

**换嵌入模型（全有或全无）**（来源：06 §5.5）：① 改 `application.yaml` 的 `models.embed.provider/model/dim`；② 计算新指纹 `sha256("<provider>/<model>@<version>#<dim>")`（模型标识见 00 §3.1）；③ 校验 `dim` 必须与既有 `ekbs_shard.dim` 一致，不一致 → 必须重建**全部**分片，不能只重建一部分；④ 逐分片执行上面 1~6 步；⑤ 任一分片仍是旧指纹 → 该分片对本次查询不可用并告警，**禁止混用不同模型的向量**；⑥ 重跑 `02` §4.4 的 RETR-REG 回归集，确认 Recall 未下降。

### 5.4 启动自检（必须实现，对应 C3）
```
加载每个分片前校验：
  1. shard.dim                    == 配置的嵌入维度（1024）
  2. shard.embedding_fingerprint  == 当前云端嵌入模型的指纹
  3. 任一不一致 → 拒绝加载该分片并告警；全部分片不一致 → 拒绝启动
运行期：FAISS 索引未加载 → 拒绝服务并告警（不降级、不假装检索）
```
（来源：01 §6.2、03 §5.4、00 §2.3）

## 6. FTS5 写入与一致性维护
- 虚表结构（唯一来源）：`ekbs_chunk_fts(content_tok, heading_path_tok, chunk_id UNINDEXED, shard_id UNINDEXED, tokenize='unicode61')`（来源：01 §5.2）。
- 写入方式：**应用层用 jieba 预分词**后写 `content_tok`（正文）与 `heading_path_tok`（标题路径），两列都进 BM25；查询同样预分词后走 `MATCH`（来源：01 §5.2、06 §3.3、02 附录 A）。

```python
def tokenize_for_fts(text: str) -> str:      # 写入路径与重建路径**必须调用同一函数**
    return " ".join(jieba.cut(normalize(text)))     # 词典/停用词/数字与单位口径见 T19

with sqlite_tx():                            # 与主表同事务：要么都成功，要么都失败，无补偿
    for i, c in enumerate(chunks):           # 每 200 行 commit 一次
        chunk_id = INSERT INTO ekbs_chunk(...) RETURNING chunk_id
        INSERT INTO ekbs_chunk_fts(content_tok, heading_path_tok, chunk_id, shard_id)
        VALUES (tokenize_for_fts(c.content),
                tokenize_for_fts(c.heading_path), chunk_id, c.shard_id)
```
（来源：02 TC-INDEX-015/016/027、03 §5.4）

- 预分词一致性：写入路径与重建路径的分词结果必须 100% 一致，否则检索结果会随重建而变（来源：02 TC-INDEX-016）。
- FTS5 侧权限与时效条件必须在 SQL `WHERE` 中**下推**（`c.shard_id IN (...)` + `d.level <= :clearance` + `d.status='published'` + 生效期 + 部门交集），不得取回后再过滤（来源：03 §5.3）。
- 一致性维护：`fts_missing` → 用同一分词函数补写该 chunk；`count_mismatch` → 触发分片重建（来源：03 §5.4）。每日对账三方计数：SQLite / FAISS `ntotal` / FTS5 行数（来源：03 §5.4）。
- doc2query 附属行（可选，采纳但只写 FTS5）：`content_tok = jieba(问题 + 原文)`，`chunk_id` 指向原 chunk、`shard_id` 相同，**不写 FAISS**；一个 chunk 会有多行 FTS5，查询后必须 `GROUP BY chunk_id` 取 `MIN(rank)`，否则 RRF 会把它当多个候选虚假抬高得分；问题文本的承载位置未定（O6-6）（来源：06 §5.4）。

## 7. 合规闸与 C5 闸对入库路径的硬约束
| 闸 | 配置 | 拦截点 | 错误行为 |
|---|---|---|---|
| 语料出网闸 | `permission.corpus_egress_ack=false` | 入库接口 + `deploy/scripts/preflight.sh`，**首次调用云端嵌入之前** | 拒绝真实语料入库任务，不产生任何向量；POC 仅可用合成/公开样本；置 `true` 才放行（来源：00 §7-1/§7-4） |
| L3 入库闸 | `permission.l3_ingest_enabled=false` | 受理阶段的密级校验（`level==3`） | 拒绝真实 L3 入库；**未实现 C5 前该值必须为 false**；C5 不提供关闭开关（来源：01 §7、03 §5.2.1） |
| L4 闸 | `permission.l4_enabled=false` | §4.1 判定算法 | 整篇拒绝：`ekbs_chunk` 无新增、`ekbs_shard` 无 `_L4` 分片、`ekbs_document` 无 `published` 行（来源：06 §1.1、02 TC-GOV-004） |
| PII / 越级闸 | — | 发布门禁 | PII 未脱敏命中 → 告警并**阻断 `published`**（来源：02 TC-GOV-028） |
| 上传者密级闸 | — | 受理阶段 | 上传者 `clearance < 目标库 default_level` → 拒绝并入审计，且 `/data/ekbs/raw` 下不产生新文件（来源：02 TC-GOV-005、04 §5.9） |

C5（进 LLM 前文档 ACL 闸门）本身落在**检索路径**（rerank 之后、父块扩展之后、Prompt 组装之前，对每个候选 chunk 执行 `can_read_document`，失败丢弃并写 `ekbs_security_event`），不在入库路径；但它是 L3 入库闸的前置条件（未落地 C5 → `l3_ingest_enabled` 必须为 false）（来源：00 C5、03 §5.2.1）。

## 8. MinerU 的调用与产物
- 调用方式：当前文档只给了 **CLI**（无 SDK 用法）：
  ```bash
  export MINERU_MODEL_SOURCE=huggingface          # 首次自动下载模型约 2–3 GB
  mineru -p /data/ekbs/raw/示例制度.pdf -o /data/ekbs/tmp/out
  ```
  后端可选 `-b pipeline` / `-b vlm`（CPU 过慢或表格还原不达标时评估）（来源：01 §4）。
- Worker 参数：`parse.engine=mineru`、`max_file_mb=500`、`timeout_s=1800`、`worker_concurrency=1`（16 GB 机器解析**串行**，避免内存峰值叠加）（来源：01 §7、00 D13）。
- 兜底链：MinerU 失败 → PaddleOCR PP-OCRv4；Office 文件走 python-docx / openpyxl / python-pptx（来源：02 §4.2、02 TC-PARSE-008）。
- 产物目录：`/data/ekbs/parse/<sha>/`，含 Markdown + `structure.json` + images；图片相对路径形如 `parse/<sha>/images/fig1.png`（来源：01 §1、01 §5.2）。

> **[实测修订 2026-09-27]** 本节多处提到的 `structure.json` 在 MinerU 2.1.10 里**不存在**。
> 实测产物为 `_content_list.json`（块列表）+ `_middle.json` + `_model.json` + `.md` + `images/`，
> 目录层级为 `<out>/<文件名>/auto/`。**下面所有以 `structure.json` 为依据的取数，改按
> `_content_list.json` 实现**；`heading_path` 字段 MinerU **不提供**（`text_level` 也常常缺失），
> 标题路径需由分块器自行推断或由入库元数据补齐（`06` §3.6 #9 同此结论）。
- 页码：来自 MinerU 的结构化输出，**禁止字符串反查**（来源：06 §3.6 #9、00 §2.2）。
- 图片：`figures[].caption` 为图注、`vlm_desc` 为图内文字（图注捕获率 ≥90%、图内 OCR CER ≤8%）；每个图形成 1 个 `figure` 块，`figure_id` 非空率 100%，`image_path` 指向的文件必须真实存在，图片抽取率 ≥95%（来源：02 TC-PARSE-010、02 TC-PARSE-024）。
- 表格：`structure.json` 提供表格 HTML、标题、脚注与前后相邻段落的**结构位置**（用结构位置而非页码合并跨页续表）（来源：06 §4.3）。
- `structure.json` 契约：必填字段 `doc_id/pages/blocks/heading_path/type`，须 100% 通过 JSON Schema 校验（来源：02 TC-PARSE-029）。

## 9. 实现待确认清单（以下均未拍板，逐条列出）
| # | 待确认项 | 冲突 / 缺口出处 |
|---|---|---|
| T1 | `status` 取值冲突：Schema 只允许 6 值，测试用例又出现 `deleted`、`superseded` | 01 §5.2 / 02 TC-PARSE-023、TC-PARSE-028 / TC-INDEX-030 |
| T2 | 合规闸配置键名不一致：`permission.corpus_egress_ack` vs `compliance.corpus_egress_ack` | 00 §7-4、01 §7 vs 01 §0.3 |
| T3 | 分片归属：算法派生（`visible_dept_ids[0]`）vs 批量导入 `--shard-id` 显式指定，谁优先、是否强制一致校验 | 06 §2.2 / 01 §12 |
| T4 | `visible_dept_ids` 存 `dept_id` 还是 `dept_code`、int 还是 str：示例是字符串数组，SQL 按 INTEGER 比较 | 06 §1.4 / 03 §5.3 / 01 §12 |
| T5 | `heading_path` 格式：`办法名>章>条` vs `一级>二级>三级`，层级数与分隔符不统一 | 06 §3.3 / 02 TC-INDEX-008 |
| T6 | 父/子块的 target token 默认值未给（只给区间），§3.3「聚合到 target 长度」无数值 | 06 §3.2 / 06 §3.3-3 |
| T7 | 重叠 12%~15% 的实现方式（复制尾部 token 还是句子重叠）、是否所有 `child` 都重叠未定义；与 06 §3.1「滑动窗口不用于全库」有措辞张力 | 06 §3.2、06 §3.1 / 02 TC-INDEX-003 |
| T8 | ~~`structure.json` 无字段级 Schema，也未规定它与 MinerU 原生产物的映射；页码与图片路径的取法无法直接编码~~ **2026-09-27 已实测关闭**：该版本**根本没有 `structure.json`**，实际产物是 `<stem>_content_list.json`（块列表，字段 `type`/`text`/`page_idx`(0 基)/`text_level`/`img_path`/`table_body`/`table_caption`/`img_caption`）、`<stem>_middle.json`、`<stem>_model.json` 与 `<stem>.md`；页码取 `page_idx+1`、图片取 `img_path`。实现见 `ekbs/ingest/parser.py::_parse_mineru`，离线契约用例见 `tests/test_parser_mineru.py`。**遗留**：文档标题行可能被整体丢弃、`第X条` 不被识别为 heading（`text_level` 缺失）→ 标题路径可能为空 | 01 §4 / 02 TC-PARSE-029 / 06 §3.6 #9 |
| T9 | MinerU 由 worker 以 CLI 子进程还是 Python SDK 调用未定；`-b` 默认后端与 Office 文件统一入口未定 | 01 §4 / 01 §12 |
| T10 | 空分片的 `ekbs_shard.embedding_fingerprint`（NOT NULL）在建分片时写什么值 | 01 §5.2 / 01 §6.3 |
| T11 | 独立图片：多图在同一条消息时，richText 文本如何分配到具体某张图 | 06 §4.11.2 |
| T12 | 图片块是否进 FAISS：§4.11.1 要求 `content` 进向量，§4.10 又写「图片的向量检索不做」；措辞易被实现成「图片文本块禁止入向量库」 | 06 §4.11.1 vs 06 §4.10、06 §3.5 |
| T13 | 合规闸与元数据拒绝的错误码未定义；`ekbs_document` 也没有承载 `error='E_IMAGE_NO_TEXT'` 的列 | 02 TC-PARSE-026 / 01 §5.2 |
| T14 | 清洗规则版本 `parse.clean_rules_version` 不在 `01` §7 的配置清单里 | 06 §1.7 / 01 §7 |
| T15 | 表格序列化走云端 LLM，是否同受 `corpus_egress_ack` 约束；序列化失败时 `status` 取何值未定义 | 06 §4.10 / 06 §1.7 |
| T16 | 重建期间分片是否置 `status='building'` 不参与检索（03 的 count_mismatch 处置）与 06「重建期间保持可检索」冲突 | 03 §5.4 / 06 §2.4 |
| T17 | `chunk_type='summary'` 在 DDL 中允许，但全目录未定义其产生规则与是否入索引 | 01 §5.2 / 06 §3.2 |
| T18 | `doc_version` 的递增比较规则（`v2.1` vs `v1.9`）与唯一性约束未定义 | 02 TC-GOV-021 / 01 §5.2 |
| T19 | jieba 分词口径未定：自定义词典内容、停用词策略、分隔与归一（仅测试用例点名，无实现规则） | 02 TC-INDEX-035~050 / 06 §5.4 |
| T20 | 分片数上限告警的落库字段、`building` 期间的路由行为与 `building`→`active` 的触发者未定义 | 06 §2.4 / 03 §5.4 / 02 TC-SHARD-011 |
