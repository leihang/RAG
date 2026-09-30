# 10 · 检索与权限实现规格（EKBS）

> 用途：把 `00`/`03`/`06` 的权威设计提炼成**可直接照着编码**的契约。不复述背景与论证；冲突以 `00` 为基线。
> 来源标注格式：（来源：03 §4.3）。矛盾与缺口集中列在 §11，实施前不得自行拍板。

## 1. 贯穿对象与字段

| 对象 | 关键字段 | 来源 |
|---|---|---|
| `PermContext` | `user_id:int`、`dt_userid:str`、`clearance:int(1..4)`、`dept_chain:Tuple[str,...]`、`visible_kb_ids:FrozenSet[int]`、`visible_shard_ids:FrozenSet[str]`、`role_codes`、`perm_codes`、`policy_version:int`、`fingerprint:str`、`now_ts:int`；不可变（frozen+slots） | 03 §4.2 |
| `ekbs_shard` | `shard_id`(PK)、`dept_code`、`level`、`faiss_path`、`dim`、`embedding_fingerprint`、`vector_count`、`index_type='flat'`、`status∈{active,building,retired}` | 01 §5.2 |
| `ekbs_chunk` | `chunk_id`、`doc_id`、`parent_id`、`chunk_type∈{parent,child,table,figure,summary}`、`page_from/page_to`、`figure_id`、`image_path`、`shard_id`、`embedding_fingerprint`、`faiss_row` | 01 §5.2 |
| 分片命名 | `kb_<dept_code>_L<level>`；`dept_code=[a-z0-9_]{2,16}` 小写、不用中文；公共分片 `dept_code='all'`；文件 `index/kb_<dept_code>_L<level>.faiss` | 00 §3.1、06 §2.2 |
| 模型标识 | `<provider>/<model>@<version>#<dim>`，如 `dashscope/text-embedding-v4@v4#1024`；`embedding_fingerprint = sha256(模型标识)` | 00 §3.1 |

## 2. 分片划分（写入规则，必须确定性）

```
shard_of(doc):                                  # 来源：06 §2.2
    if doc.level == 4 and not cfg.permission.l4_enabled:
        reject("E_L4_DISABLED")                 # 不产生 chunk、不产生分片
    if not doc.visible_dept_ids:                # 空数组
        shard_id = f"kb_all_L{doc.level}"       # 仅 level==1 合法，否则视为配置错误
    else:
        dept_code = dept_code_of(doc.visible_dept_ids[0])   # 归属主部门 = 第一个
        shard_id  = f"kb_{dept_code}_L{doc.level}"
```

- 向量只存一份：多部门可见文档落在**第一个**可见部门的分片，其余部门靠 `ekbs_acl_entry(object_type='shard', subject_type='dept', effect='allow')` 授权，ACL 变更只需 `bump_policy_version`，不重建分片。（来源：06 §2.2）
- 分片按一级/二级部门划分，三级部门差异用 ACL 表达；分片 `status='building'` 与 `'retired'` 不参与路由；分片数 >100 写 `ekbs_security_event` 告警。（来源：06 §2.4、06 §6.4.4 TC-SHARD-013）

## 3. `dept_chain` 的含义与来源

- **含义**：用户所属部门 ∪ 其**所有祖先**部门的集合。方向不可反：文档授权给部门 D，则 D 及其所有祖先的成员可见；绝不按子部门向下展开。（来源：03 §2.5）
- **来源链**：`ekbs_dept`（`dept_id` 自增 PK、`dingtalk_dept_id`、`parent_dept_id`、`dept_path`、`depth`）→ `ekbs_dept_closure(dept_id, ancestor_id, depth)` 存「部门→祖先闭包（含自身 depth=0）」→ `ekbs_user_dept(user_id, dept_id)` 为用户-部门多对多 → 聚合结果物化到 `ekbs_user.dept_chain`。（来源：01 §5.1、03 §2.5）
- **刷新逻辑**（由 `ekbs-scheduler` 同步任务执行，检索期不递归查询）：
  ① `DELETE FROM ekbs_dept_closure` 后用 `WITH RECURSIVE up AS (...)` 重建闭包；
  ② `UPDATE ekbs_user SET dept_chain = json_group_array(ancestor_id)`，取 `ekbs_user_dept ⨝ ekbs_dept_closure` 的 `DISTINCT ancestor_id`，无部门时写 `'[]'`，仅 `status='active'` 用户。（来源：03 §2.5）
- **值域**：`ekbs_user.dept_chain` 按 Schema 注释是 **dingtalk_dept_id 的 JSON 数组（TEXT）**；闭包表本身是 **INTEGER 自增 `dept_id`**；分片侧是 `dept_code`（`hr`/`finance`）。三者互转的映射位置未定 → §11 K1。（来源：01 §5.1、06 §2.2/§2.5）
- 组织变更后：更新 `ekbs_user_dept` → 刷新 `dept_chain` → 删该用户 L1/L2 缓存 →（全局或用户级版本号自增，见 §5）目标时效 ≤5 分钟。（来源：03 §6.4）

## 4. `visible_shard_ids` 权威算法（分片路由唯一入口）

**唯一入口**：`ekbs/service/shard_router.py::visible_shards(...)`。此后任何检索路径都**只读取** `PermContext.visible_shard_ids`，不得自行重算或裁剪。（来源：03 §4.1、06 §2.5）

**输入**：`user_id`、`clearance`、`dept_chain`、当前时间、以及 `ekbs_acl_entry` 中 `object_type='shard'` 的条目（`role`/`group` 主体先展开为 `user_id` 集合）。`policy_version` **不参与**路由，只参与指纹。（来源：03 §4.1、06 §2.5）

```python
def visible_shard_ids(user_id, clearance, dept_chain, now) -> list[str]:
    # ① 候选：仅 active 分片（building/retired 不出现）          来源：06 §2.5 / TC-SHARD-013
    rows = SELECT shard_id, dept_code, level FROM ekbs_shard WHERE status = 'active'
    # ② 密级（纵向）：level <= clearance                        来源：06 §2.5
    cands = [r for r in rows if r.level <= clearance]
    # ③ 部门（横向）：dept_code == 'all' 或 dept_code 命中用户部门链
    visible = {r.shard_id for r in cands if r.dept_code == 'all' or r.dept_code in dept_chain}
    # ④ ACL 叠加：object_type='shard' AND permission='read'
    #    AND (expire_at IS NULL OR expire_at > now)
    #    AND (subject_type='user' AND subject_id=:user_id)
    #     OR (subject_type='dept' AND subject_id IN :dept_chain)
    #     OR 展开后的 role/group 主体                             来源：06 §2.5、03 §6.3
    allow, deny = load_shard_acl(user_id, dept_chain, now)
    result = (visible | allow) - deny          # deny 优先，最后才减      来源：03 §1.2 P1、03 §6.3
    if not result:
        log.warning("user %s has no visible shard", user_id)   # 非错误，但必须留痕
    return sorted(result)                      # 去重 + 升序，顺序稳定是契约
```

- **去重/排序**：用 `set` 去重后 `sorted()` 升序；同一用户连续 10 次调用必须「集合相等且有序」，顺序影响缓存键可复现性。（来源：06 §6.4.4 TC-SHARD-001）
- **不变量（硬）**：指纹第二项即本函数输出，因此「`perm_fingerprint` 相同 ⇒ `visible_shard_ids` 相同」是恒等式；`ekbs_acl_entry` 变更必须与 `bump_policy_version()` **同事务**，否则出现「ACL 变了、指纹没变、旧缓存复用」的窗口。（来源：06 §2.6、00 §3.4）
- **性能约束**：单次路由 ≤5ms（纯 SQLite）；结果缓存 `ekbs:perm:user:<user_id>`，TTL 300s；权限解析 P95 ≤20ms、缓存命中率 ≥95%；`uvicorn --workers 1` 是前提（进程内 L1 靠同进程失效）。（来源：00 §3.4、06 §6.5、03 §8.5、01 §7）
- **空集**：返回明确用户提示并写 WARN，不得 500、不得返回空答案冒充正常。（来源：06 §6.4.4 TC-SHARD-009）
- 检索遍历分片时必须按 `sorted(visible_shard_ids)` 顺序，保证结果可复现。（来源：03 §5.2.1、TC-SHARD-001）

## 5. `perm_fingerprint`

**逐字照抄 `00` §3.4（唯一权威定义；`03` §4.3 与 `04` §4.6 是同一实现，必须只有一个代码实现，建议 `ekbs/service/perm_cache.py`，其余位置 import）：**

```python
def perm_fingerprint(user_id: int, visible_shard_ids: list[str], clearance: int,
                     dept_chain: list[str], policy_version: int) -> str:
    raw = "|".join([
        str(user_id),
        ",".join(sorted(visible_shard_ids)),
        str(clearance),   # 整数密级序号 1..4 序列化为 "1".."4"，禁止传 "L2" 之类字符串
        ",".join(sorted(dept_chain)),
        str(policy_version),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

- 五项的顺序、分隔符（`|` 连接五项、项内 `,`）、是否排序（第 2/4 项排序）**都是契约**；类型也是契约：`clearance` 为整数 1..4，进哈希前 `str()` 得 `"1".."4"`。（来源：00 §3.4）
- **不得包含** `now_ts`（否则缓存永不命中，时效由独立的 effective 过滤承担）。（来源：03 §4.3）
- **不得包含** 用户级 `perm_version`（会破坏跨用户缓存隔离假设）；第五项必须是全局 `policy_version`。（来源：04 §4.6）

**`policy_version` 自增（= 全量缓存失效）的条件**，权威存储在单行表 `ekbs_policy_version`：

| 变更 | 动作 | 来源 |
|---|---|---|
| ACL 新增/删除（含分片级 allow/deny） | `bump_policy_version()`，**与写 ACL 同事务** | 04 §4.6、06 §2.6 |
| 用户密级调整 | 同上 | 03 §6.4、04 §4.6 |
| 用户离职 / 调岗 | 更新数据 + 删该用户 L1/L2 缓存 + 版本自增（全局还是仅 `perm_version` 未定 → §11 K2） | 03 §6.4、04 §4.6 |
| 文档密级调整 / 文档可见部门调整 | 更新 SQLite + **迁移该文档全部 chunk 到目标分片**（重算 `shard_id`、`faiss_row`、重写 FTS5 行）+ 自增 + 写 `ekbs_perm_change_log` | 03 §6.4、03 §6.4 `PermissionChangeService` |
| 角色权限点集合变更 | 清空 L1/L2 权限缓存或版本自增 | 03 §6.4 |
| 知识库可见性变更 | 重建该 KB 的分片归属 + 自增（时效 ≤30 分钟） | 03 §6.4 |
| 业务强制失效 | 删 `ekbs:perm:user:<id>` + 失效相关 `ekbs:perm:fp:*`（倒排集合 `ekbs:perm:idx:*`） | 03 §4.5、04 §4.6 |

- 自增一律走 `PermissionChangeService`（数据更新 + 分片迁移 + 缓存失效 + 审计四件事），禁止绕过。（来源：03 §6.4）
- 只读镜像 `ekbs:perm:policy_version` TTL 60s，权威永远是 SQLite。（来源：04 §4.6）

## 6. ACL 判定语义

- `effect ∈ {allow, deny}`，**deny 优先于 allow**：任一命中主体的 deny 生效即拒绝，不受 allow 影响。（来源：03 §1.2 P1、03 §6.3）
- 读判定取 `permission ∈ {read, manage}`（`write`/`audit` 不授予读）。（来源：03 §6.3、01 §5.3）
- `subject_type ∈ {user, dept, role, group}`；`dept` 主体经部门闭包展开**含子部门**；`role` 展开为用户集合，`group` 为 `ekbs_group_member`。（来源：03 §2.3）
- `object_type` 各取值的含义与作用点：

| `object_type` | 含义 | 生效点 |
|---|---|---|
| `kb` | 知识库可见性 | 收敛 `visible_kb_ids`，进而影响 C5 第 5 步 |
| `folder` | 目录级授权，**逐层累积、不覆盖** | 文档有效 ACL 的组成；任一层 deny 即拒绝 |
| `document` | 文档级跨部门授权 / 片内 deny | **仅在 C5 生效**，不改变分片集合 |
| `shard` | 整片并入/移出可见集合 | **仅在分片路由生效**（§4 第 ④ 步） |

（来源：01 §5.3、03 §2.2、03 §6.3、06 §2.2）
- 分块不可单独授权；chunk 的密级/可见部门/生效期在写入时从文档**复制冗余**，并据此决定归属分片。（来源：03 §2.2）
- **deny 为什么必须靠 C5 兜底**：分片粒度只能表达「(部门 × 密级)」，无法表达同部门同密级下的单文档 deny；「无权分片不参与计算」≠「片内没有 deny 条目」。因此 C5 是红线而非优化项；未落地 C5 前不得将真实 L3 语料入库（`permission.l3_ingest_enabled=false`）。（来源：03 §5.2、00 C5、01 §7）
- 无权一律返回 **404（不是 403）**，避免存在性泄露；同时返回固定耗时的空响应。（来源：03 §4.5、03 §6.2）

## 7. C5 闸门 `can_read_document`（精确判定顺序）

```python
async def can_read_document(perm, doc) -> bool:          # 来源：03 §6.3
    # 1) 状态与时效
    if doc.status != "published": return False
    if doc.effective_date and doc.effective_date > today(): return False
    if doc.expire_date    and doc.expire_date    <= today(): return False
    # 2) 密级（纵向）
    if doc.level > perm.clearance: return False
    # 3) 知识库可见性（与 00 §3.2「L1 全员可见」冲突 → §11 K3）
    if doc.kb_id not in perm.visible_kb_ids: return False
    # 4) ACL：先 deny 再 allow（deny 优先）
    entries  = await acl_cache.get_entries("document", doc.doc_id)
    subjects = expand_subjects(perm)   # {(user,uid)} ∪ {(dept,d) for d in dept_chain} ∪ role/group 展开
    for e in entries:
        if (e.subject_type, str(e.subject_id)) in subjects and e.effect == "deny" \
           and e.permission in ("read", "manage"):
            return False                                   # 4.1 deny
    if any((e.subject_type, str(e.subject_id)) in subjects and e.effect == "allow"
           and e.permission in ("read", "manage") for e in entries):
        return True                                        # 4.2 allow
    # 5) L4 白名单
    if doc.level == 4: return False                        # 无显式 allow → 拒绝
    # 6) 部门 / 公开
    if doc.level == 1: return True
    doc_depts = set(doc.visible_dept_ids or [])
    if not doc_depts: return True                          # 未限定部门 = 内部公开
    return bool(doc_depts & set(perm.dept_chain))
```

- **在链路上的位置**：rerank 之后、组装 Prompt 之前；对每个候选 chunk 的父文档执行。`00` 要求**父块扩展后的父块仍须过 C5**，且闸门不可关闭。（来源：00 C5、00 §2.1、03 §5.2.1）
- **失败行为**：丢弃该 chunk，写 `ekbs_security_event`（`reason='c5_denied'`，含 user_id/doc_id/chunk_id）；被丢弃内容**不得**进入 LLM 上下文、答案缓存键对应的值、日志正文；全部被丢弃 → 拒答 `RefuseAnswer("无可见依据")`。（来源：03 §5.2.1、00 §2.3）
- **禁止**用「引用后剔除」冒充闸门；测试锚点 TC-PERM-033（片内 deny）、TC-PERM-035（C5 未过不得进缓存）。（来源：03 §5.2.1、03 §8.3）

## 8. 检索链路逐环参数与顺序

顺序：查询改写（多轮启用）→ 查询嵌入 → 逐分片 FAISS → FTS5/BM25 → RRF → rerank → **C5** → 父块扩展 → 生成 → 引用后置校验。（来源：00 §2.1、03 §5.2.1）

| 环节 | 精确参数与行为 | 来源 |
|---|---|---|
| 查询嵌入 | `dashscope/text-embedding-v4@v4#1024`，dim=1024；先查 `ekbs:cache:embed:<sha256(query)>`（TTL 86400，全局共享） | 00 §3.1、03 §4.4 |
| FAISS 召回 | `IndexFlatIP`，写入前 `faiss.normalize_L2`；**每分片** `idx.search(qvec, min(30, idx.ntotal))`；`search` 是同步 C 调用，必须包在 `asyncio.to_thread` 中；分片缺失见 §11 K17 | 06 §2.3、03 §5.2.1 |
| FTS5 召回 | jieba **预分词**写入 `content_tok`/`heading_path_tok`，`tokenize='unicode61'`；`bm25(ekbs_chunk_fts)` **越小越相关**；`top_k=30`；权限条件必须 SQL 下推（见下） | 01 §5.2、03 §5.3 |
| RRF 融合 | `rrf(c) = Σ_{l∈{dense,sparse}} 1/(k + rank_l(c))`，**k=60**；只用名次，不做分数校准、不归一化 BM25；两路各自 topK=30 | 05 §1647、00 §2.1、06 §3.6 |
| rerank | `gte-rerank-v2`；输入候选 15、输出 `keep=5`、`min_score=0.30`；返回条数不足即视为失败并走降级，**禁止补 0 分、禁止硬编码假分数** | 00 §2.1、01 §7、06 §3.6 |
| 拒答 | rerank 最高分 < 阈值 或 上下文无有效内容 → 拒答；`ekbs_qa_message.refused=1` 是一等公民 | 00 §2.1、06 §4.6 |
| 父块扩展 | 命中子块 → 取 `parent_id` 指向的 L1 父块全文（1024~1536 token）；`rerank keep=5` ⇒ 最多回填 5 个父块；扩展后的父块仍须过 C5 | 06 §3.2、06 §3.4、00 §2.1 |
| 引用后置校验 | 见下方三条 | 00 §2.2、03 §5.5、06 §4.7/§4.11.3 |

FTS5 下推 SQL 的权限条件（不可取回后再过滤，且 FAISS/FTS5 是两条独立路径、权限语义必须一致）：

```sql
WHERE f.content_tok MATCH :fts_query
  AND c.shard_id IN (SELECT value FROM json_each(:shard_ids))       -- ① 分片（核心）
  AND d.level <= :clearance                                        -- ② 密级
  AND d.status = 'published'                                       -- ③ 状态与时效
  AND (d.effective_date IS NULL OR d.effective_date <= date('now'))
  AND (d.expire_date    IS NULL OR d.expire_date    >  date('now'))
  AND (json_array_length(d.visible_dept_ids) = 0                   -- ④ 部门：空数组=内部公开
       OR EXISTS (SELECT 1 FROM json_each(d.visible_dept_ids) v
                  WHERE v.value IN (SELECT value FROM json_each(:dept_chain))))
GROUP BY c.chunk_id                                                 -- ⑤ 同 chunk 多行（原文+doc2query 问题）
ORDER BY score LIMIT :top_k;                                        --    取 MIN(bm25(...)) 去重，否则 RRF 虚假抬高得分
```

- 第 ⑤ 步是硬要求：doc2query 使同一 `chunk_id` 有多行 FTS5，不去重会让 RRF 把一行当多个候选。（来源：06 §5.4）
- ④ 中 `v.value` 的**类型/值域**取决于 §11 K1 的结论（原文档写 `CAST(v.value AS INTEGER)`）。（来源：03 §5.3）
- 索引要求：为 `ekbs_chunk(shard_id)`、`ekbs_document(status, level)` 建索引；`visible_dept_ids` 数组内元素不建索引（数据量小）。（来源：03 §5.3）
- FAISS 路径与 FTS5 路径的权限条件必须语义等价，每日对账（SQLite↔FAISS↔FTS5）差异率 <0.01%。
  （来源：03 §5.3、03 §5.4）

**引用后置校验（生成之后，三条都必须做）**：
1. 答案中每个引用编号必须在本次请求的 `citation_map` 中存在，否则为**幻觉引用** → 删除该引用标记 + 写审计 `hallucinated_citation`。（来源：00 §2.2、03 §5.5）
2. **ACL 二次确认**该 chunk 对当前用户仍可见（防长对话期间权限变更）→ 不可见则删除标记 + 写审计 `citation_acl_revoked`。（来源：00 §2.2、03 §5.5）
3. **页码必须落在 `page_from ~ page_to` 区间内**，结果写入 `ekbs_qa_citation.page_check`（`pass`/`out_of_range`）与 `acl_result`；通过率目标 100%。（来源：00 §2.2、06 §4.7、01 §5.4）
4. `chunk_type ∈ {figure, table}` 的引用额外携带 `figure_id` 与 `image_path`；图片经 `GET /v1/assets/figure/{chunk_id}` 获取，服务端按 `ekbs_chunk.shard_id` 复检可见性（不在当前 `visible_shard_ids` → **404**），并断言 `image_path` 拼出的绝对路径落在 `/data/ekbs/parse/` 之内；**禁止 Caddy 静态托管 `/data/ekbs/parse`**；访问写 `ekbs_doc_access_log(action='view', channel='citation')`。（来源：00 §2.2、06 §4.11.3）
5. 每轮都重新加载 `PermContext`、重算指纹，**不跨轮复用检索结果与上下文**（防多轮残留）。（来源：03 §6.1 V5）

## 9. 缓存键、TTL 与 L1/L2

- **结构**：L1 = 进程内 LRU（`l1_size=2000`）；L2 = SQLite 表 `ekbs_cache(cache_key PK, value TEXT, expires_at INTEGER)`；过期判断用 `expires_at < now`，清理任务定期 `DELETE FROM ekbs_cache WHERE expires_at < :now`。（来源：01 §7、01 §5.5、03 §4.4）

| 用途 | Key | TTL | 含指纹 | 来源 |
|---|---|---|---|---|
| 用户权限上下文 | `ekbs:perm:user:<user_id>` | 300s | —（本身是权限源） | 03 §4.4 |
| 权限指纹上下文 | `ekbs:perm:fp:<fingerprint>` | 300s | ✔（即指纹本身） | 03 §4.4、04 §4.6 |
| 检索结果 | `ekbs:cache:retrieve:<fingerprint>:<qhash>` | 1800s | **✔ 必须** | 03 §4.4、01 §7 |
| 答案 | `ekbs:cache:answer:<fingerprint>:<qhash>` | 3600s | **✔ 必须** | 03 §4.4、01 §7 |
| 查询嵌入 | `ekbs:cache:embed:<text_sha256>` | 86400s | ✘（向量与权限无关，**可全局共享**） | 03 §4.4、01 §7 |
| 文档元数据 | `ekbs:meta:doc:<doc_id>` | 600s | ✘（仅元数据，不含正文） | 03 §4.4 |
| 策略版本镜像 | `ekbs:perm:policy_version` | 60s | —（权威在 SQLite） | 04 §4.6 |

- 缓存写入必须使用**当前请求**的 `fingerprint`；**禁止**异步批量预热跨用户缓存；命中后按 1% 采样做 ACL 抽检；缓存击穿用单飞（singleflight）+ TTL 随机抖动 ±10%。（来源：03 §4.5、03 §6.1 V4）
- **CI 强制**：静态扫描禁止 `ekbs:cache:answer:` / `ekbs:cache:retrieve:` 前缀的 f-string 缺少 `fingerprint` 变量。（来源：03 §4.4）

## 10. 降级矩阵的代码行为（对应 `00` §2.3 逐行）

| 故障 | 代码中的具体行为 | 来源 |
|---|---|---|
| 云端 LLM 不可用 | 连续超时/非 2xx → 网关切 `fallback`（`deepseek/deepseek-chat`）；用户无感、响应稍慢 | 00 §2.3、01 §7 |
| 云端 rerank 不可用 | 捕获失败/超时 → **跳过精排**，直接取 RRF Top-5 作为候选（仍须过 C5）；`ekbs_metric` 记数并告警；相关性下降约 8–15% | 00 §2.3、06 §3.6 |
| **云端嵌入不可用（查询期）** | 先查 `ekbs:cache:embed:<sha256(query)>`：命中 → 继续；未命中 → **明确拒答并提示用户**（`refused=1`）。**禁止**降级为「只走 FTS5 假装语义检索」——不同模型/无向量都不构成等价召回 | 00 §2.3、00 §2.3 注 |
| 云端嵌入不可用（入库期） | 任务排队重试 + 退避（`timeout_s=30`、`max_retries=3`），不影响在线查询 | 00 §2.3、01 §7 |
| FAISS 索引未加载 | 启动自检或运行期异常 → **拒绝服务并告警**；分片级缺失的处置与 `03` §5.2.1 冲突 → §11 K17 | 00 §2.3、03 §5.2.1 |
| 嵌入指纹不一致 | 启动自检：分片 `dim != 1024` 或 `embedding_fingerprint != 当前模型指纹` → 拒绝加载该分片并告警，全部不符 → **拒绝启动** | 03 §5.4 |
| 本地磁盘水位 > 85% | 停止入库任务，仅提供查询 | 00 §2.3 |
| 索引未就绪（`status != published`） | 该文档不参与召回（FAISS 与 FTS5 两侧同时过滤） | 00 §2.3、03 §5.3 |
| 分片重建/重载窗口 | 该分片本轮不参与检索并告警（宁可少召回，不可出错）；重建用 `.faiss.new` + 原子 rename + `POST /internal/shard/reload` | 06 §2.4 |
| 无可见分片 | 明确提示用户 + WARN 日志，不得 500、不得返回空答案 | 06 §6.4.4 TC-SHARD-009 |

## 11. 实现待确认清单（文档矛盾或不足以直接编码，不自行拍板）

| # | 问题 | 冲突/缺口出处 |
|---|---|---|
| K1 | `dept_chain` 与分片的**值域不一致**：`ekbs_user.dept_chain` 是 dingtalk_dept_id（TEXT）数组，闭包表是 INTEGER `dept_id`，分片用 `dept_code`（`hr`）；`03` §2.5 的刷新 SQL 把 INTEGER `ancestor_id` 写进 `dept_chain`，`03` §5.3 又 `CAST(v.value AS INTEGER)`，而 `06` §2.5 直接 `dept_code in dept_chain`。**dept_code ↔ dept_id 映射放哪未定** | 01 §5.1 vs 03 §2.5/§5.3 vs 06 §2.5；06 §7.3 O6-1 |
| K2 | 用户离职/调岗时是 `policy_version++`（全局失效）还是只 `perm_version++` + 删该用户缓存？两处结论相反 | 03 §6.4 vs 04 §4.6 |
| K3 | C5 第 3 步「`doc.kb_id ∉ visible_kb_ids` → 拒绝」与「L1 全员可见」（00 §3.2）及 FTS5 SQL 无 kb 条件相冲突：无该 KB 授权的用户是否可见其 L1 文档？ | 03 §6.3 vs 00 §3.2 / 03 §5.3 |
| K4 | L4 白名单判定不一致：`03` §2.4 要求 allow 的 `subject_type='user'`；`03` §6.3 的第 4.2 步对任意主体 allow 就 `return True`，L4 检查（第 5 步）永远到不了 | 03 §2.4 vs 03 §6.3 |
| K5 | 文档级 ACL 的 `expire_at` 是否参与判定：分片路由 SQL 有 `expire_at` 条件，`can_read_document` 完全没有 | 06 §2.5 vs 03 §6.3 |
| K6 | **跨部门文档在 C5 会被误拒**：`06` §2.2 用 `object_type='shard'` 的 allow 授予其他部门，但 C5 只看 document 级 ACL 与 `visible_dept_ids` → 用户能召回、却过不了闸门。C5 是否需要同时接受分片级 allow？ | 06 §2.2 vs 03 §6.3 |
| K7 | `03` §4.1(d)「任一 ACL deny 条目命中 → 从列表移除」未限定 `object_type='shard'`；文档级 deny 不应影响分片路由 | 03 §4.1 vs 06 §2.5 |
| K8 | rerank 语义冲突：`00` §2.1 写「输入 15 / 输出 5」、`01` §7 注释 `top_n` 为**输入候选数**；`02` §797 与 `05` §1587 写「30 候选 → top_n 15 → 保留 5」。进 rerank 的是 15 还是 30、`top_n` 是输入还是返回条数，未定 | 00 §2.1 / 01 §7 vs 02 §4.4 / 05 |
| K9 | rerank 阈值取值：`01` §7 `min_score=0.30`；`05` §2330 示例显示 `rerank ≥ 0.35` | 01 §7 vs 05 §9 |
| K10 | 双路 topK：`00` §2.1 与 `01` §7 为 30/30；`05` §2330 参数快照显示 `top_k=50/50` | 00 §2.1 / 01 §7 vs 05 |
| K11 | RRF 的 `rank_i` 起点未写明（1-based 还是 0-based）；不影响排序，但影响调试台展示的融合分与任何绝对阈值 | 05 §1647、03 §5.2.1 均未定义 |
| K12 | `<qhash>` 定义缺失：取原始问题还是改写后问题（多轮改写会让缓存行为不同）？另：`ekbs:cache:embed:<text_sha256>` **未包含 embedding_fingerprint**，换模型后会命中旧向量，与 C3 冲突 | 03 §4.4 vs 00 C3 |
| K13 | C5 与父块扩展的先后/次数：`00` §2.1 的行序是 rerank → C5 → 父块扩展，且要求「扩展后的父块仍须过 C5」；`03` §5.2.1 规则表写「rerank 之后、父块扩展完成之后、Prompt 之前」。是**两次闸门**还是**仅扩展后一次**未定 | 00 §2.1/C5 vs 03 §5.2.1 |
| K14 | 条款号校验：`06` §4.7 要求页码之外再校验「第三十二条」这类条款号，但 `00` §2.2 只定义页码区间，且 `ekbs_chunk` 无条款号字段；抽取与比对算法未定义 | 06 §4.7 vs 00 §2.2 / 01 §5.2 |
| K15 | 无可见分片/无依据时的用户提示文案与 HTTP 语义未定义（TC-SHARD-009 只要求「明确提示，非 500」） | 06 §6.4.4、03 §5.2.1 |
| K16 | doc2query 问题行的落库位置未定（29 张表内无承载表），未定前不得启用 doc2query，否则 FTS5 重建会丢失问题行 | 06 §5.4、06 §7.3 O6-6 |
| K17 | 分片缺失处置冲突：`00` §2.3 要求「FAISS 索引未加载 → 拒绝服务并告警」，`03` §5.2.1 代码为「该分片跳过并告警」。是「全部分片未加载」才拒服、还是单分片缺失也拒服，未定 | 00 §2.3 vs 03 §5.2.1 |
