# 阿里云百炼（DashScope）模型价格与限流核实表

- **核实日期**：2026-09-27（本文件所有结论均来自当日实际抓取的阿里云官方页面，未使用记忆）
- **默认地域**：华北 2（北京）—— 官方价格表默认以北京为「公共云」主列；新加坡/其他地域差异在表内单独标注
- **单位口径**：
  - 嵌入模型官方表格用 **元/千输入 Token**（如 `0.0005 元`）
  - 重排与 LLM 官方表格用 **元/百万 Token**（如 `0.5 元` / `12 元`）
  - 本表两列都写，**不做隐式换算**；`0.0005 元/千 Token` = `0.5 元/百万 Token`（同一数值，仅单位不同）
- **限流口径**（依据 [限流](https://help.aliyun.com/zh/model-studio/rate-limit)）：**按阿里云主账号维度合并计算**（账号下所有 RAM 子账号、业务空间、API Key 的调用量合并），同时**不同模型限流额度相互独立**。即「额度按模型定义、计量按账号合并」。限流可能按秒级 RPS（RPM/60）与 TPS（TPM/60）执行；超出返回 429，通常 1 分钟内恢复。
- **限流豁免**：支持批量推理（Batch API）的文本生成模型，用 Batch API 调用时**不受实时限流限制**（嵌入模型的 Batch 接口同理见各页）。
- **免费额度通则**（依据 [新人免费额度](https://help.aliyun.com/zh/model-studio/new-free-quota)）：仅**华北 2（北京）**地域模型享有；有效期为开通百炼/模型发布/申请通过之日起 **90 天**（以较晚者为准）；每模型独立，通常 100 万 Token；**仅抵扣实时推理**，Batch 调用/模型调优/部署/OSS 等不抵扣。

---

## 一、主表

> 说明：为可读性，单元格内多条信息用 `<br>` 分行；「依据 URL」列给出该行所有字段的官方出处。

| 模型 | 类型 | 单价 | 单位 | 限流 | 上限 | 依据 URL |
|---|---|---|---|---|---|---|
| `text-embedding-v4` | 嵌入（文本向量，Qwen3-Embedding 系列） | 文本输入 0.0005 元<br>Batch 接口调用 0.00025 元（半价）<br>新加坡：0.000514 元 | 元/**千输入 Token** | 北京 RPM 1,800 / TPM 1,200,000<br>新加坡 RPM 1,800 / TPM 1,000,000<br>中国香港 RPM 1,800 / TPM 1,200,000 | 单行（单条）最大 8,192 Token<br>单次请求最多 10 条<br>单批次最大处理 Token 33,000<br>向量维度 2,048、1,536、1,024（默认）、768、512、256、128、64 | <https://help.aliyun.com/zh/model-studio/text-embedding-v4><br><https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api><br><https://help.aliyun.com/zh/model-studio/embedding> |
| `text-embedding-v3` | 嵌入（文本向量） | 文本输入 0.0005 元<br>Batch 接口调用 0.00025 元（半价）<br>新加坡：0.000514 元 | 元/**千输入 Token** | 北京 RPM 1,800 / TPM 1,200,000<br>新加坡 RPM 6,000 / TPM 24,000,000 | 单行（单条）最大 8,192 Token<br>单次请求最多 10 条<br>单批次最大处理 Token 33,000<br>向量维度 1,024（默认）、768、512、256、128、64 | <https://help.aliyun.com/zh/model-studio/text-embedding-v3><br><https://help.aliyun.com/zh/model-studio/embedding> |
| `qwen3.7-text-embedding` | 嵌入（文本向量，Qwen3.7 新一代） | 文本输入 0.0005 元<br>Batch 接口调用 0.00025 元（半价）<br>新加坡：0.000525 元 | 元/**千输入 Token** | 北京 RPM 24,000 / TPM 1,000,000<br>新加坡 RPM 2,000 / TPM 1,000,000 | 单行（单条）最大 128,000 Token<br>单次请求最多 20 条<br>单批次最大处理 Token 128,000<br>上下文长度 131,072<br>向量维度 2,560、2,048、1,536、1,024（默认）、768、512、256 | <https://help.aliyun.com/zh/model-studio/qwen3-7-text-embedding><br><https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api><br><https://help.aliyun.com/zh/model-studio/embedding> |
| `qwen3.7-text-embedding-flash` | 嵌入（轻量文本向量） | 文本输入 0.000125 元<br>Batch 接口调用 0.000063 元（半价） | 元/**千输入 Token** | 北京 RPM 24,000 / TPM 1,000,000 | 单行（单条）最大 128,000 Token<br>单次请求最多 20 条<br>单批次最大处理 Token 128,000<br>上下文长度 131,072<br>向量维度 1,024（默认）、768、512、256 | <https://help.aliyun.com/zh/model-studio/qwen3-7-text-embedding-flash><br><https://help.aliyun.com/zh/model-studio/embedding> |
| `gte-rerank-v2` | 重排（Rerank） | 文本输入 0.8 元<br>无 Batch 折扣（官方「批量推理」标注为**不支持**） | 元/**百万 Token** | 北京 RPM 5,040 / TPM 4,980,000,000 | 单次请求最大文档数 500<br>单条（Query 或 Document）最大 4,000 Token<br>单次请求最大输入 Token 30,000<br>上下文长度 30,000 | <https://help.aliyun.com/zh/model-studio/gte-rerank-v2><br><https://help.aliyun.com/zh/model-studio/rerank><br><https://help.aliyun.com/zh/model-studio/text-rerank-api> |
| `qwen3-rerank` | 重排（Rerank） | 文本输入 0.5 元<br>新加坡：0.749 元<br>无 Batch 折扣（批量推理不支持） | 元/**百万 Token** | 北京 RPM 5,400 / TPM 5,000,000,000<br>新加坡 RPM 5,400 / TPM 5,000,000,000 | 单次请求最大文档数 500<br>单条最大 4,000 Token<br>单次请求最大输入 Token 120,000<br>上下文长度 30,000 | <https://help.aliyun.com/zh/model-studio/qwen3-rerank><br><https://help.aliyun.com/zh/model-studio/rerank> |
| `qwen3.7-text-rerank` | 重排（Rerank，Qwen3.7 新一代） | 文本输入 0.5 元<br>无 Batch 折扣（批量推理不支持） | 元/**百万 Token** | 北京 RPM 3,000 / TPM 9,000,000 | 单次请求最大文档数 500<br>单条最大 30,000 Token<br>单次请求最大输入 Token 120,000（官方标注为**建议值**）<br>上下文长度 32,768 | <https://help.aliyun.com/zh/model-studio/qwen3-7-text-rerank><br><https://help.aliyun.com/zh/model-studio/rerank> |
| `qwen-max` | LLM（文本生成） | 输入 2.4 元 / 输出 9.6 元<br>输入（缓存命中）0.48 元<br>输入（Batch File）1.2 元 / 输出（Batch File）4.8 元（半价）<br>新加坡：输入 11.743 / 输出 46.971 元<br>**无阶梯计价** | 元/**百万 Token** | 北京 RPM 1,200 / TPM 1,000,000<br>新加坡 RPM 600 / TPM 1,000,000 | 上下文长度 32,768<br>最大输入长度 30,720<br>最大输出长度 8,192 | <https://help.aliyun.com/zh/model-studio/qwen-max><br><https://help.aliyun.com/zh/model-studio/model-pricing><br><https://help.aliyun.com/zh/model-studio/rate-limit> |
| `qwen-plus` | LLM（文本生成，滚动更新，当前等同于 `qwen-plus-2025-12-01`） | **有阶梯计价**（按单次请求输入 Token 总量整档结算）：<br>0<Token≤128K：输入 0.8 / 输出 2（思考输出 8）<br>128K<Token≤256K：输入 2.4 / 输出 20（思考输出 24）<br>256K<Token≤1M：输入 4.8 / 输出 48（思考输出 64）<br>输入（缓存命中）0.16 / 0.48 / 0.96 元<br>Batch File 全部为实时推理价 **50%**<br>Batch Chat 另有「限时 5 折」标注<br>新加坡 0<Token≤256K：输入 2.936 / 输出 8.807（思考输出 29.357） | 元/**百万 Token** | 北京 RPM 30,000 / TPM 5,000,000<br>`qwen-plus-latest` 北京 RPM 15,000 / TPM 1,200,000<br>`qwen-plus-2025-12-01` 北京 RPM 120 / TPM 1,000,000 | 上下文长度 1,000,000<br>最大输入长度 997,952（思考模式 995,904）<br>最大输出长度 32,768<br>最大思维链长度 81,920 | <https://help.aliyun.com/zh/model-studio/qwen-plus><br><https://help.aliyun.com/zh/model-studio/model-pricing><br><https://help.aliyun.com/zh/model-studio/rate-limit> |
| `qwen3.7-max` | LLM（文本生成） | 输入 12 元 / 输出 36 元<br>输入（缓存命中）2.4 元<br>显式缓存创建 15 元 / 显式缓存命中 1.2 元<br>输入（Batch File）6 / 输出（Batch File）18（半价）<br>输入（Batch Chat）12 / 输出（Batch Chat）36（无折扣）<br>新加坡：输入 18.736 / 输出 56.207 元 | 元/**百万 Token** | 北京 RPM 30,000 / TPM 5,000,000<br>新加坡 RPM 600 / TPM 1,000,000<br>（快照版限流更低：`qwen3.7-max-2026-06-08` 与 `-2026-05-20` 北京 600/1,000,000） | 上下文长度 1,000,000<br>最大输入长度 991,808（思考模式 983,616）<br>最大输出长度 131,072<br>最大思维链长度 262,144 | <https://help.aliyun.com/zh/model-studio/qwen3-7-max><br><https://help.aliyun.com/zh/model-studio/model-pricing><br><https://help.aliyun.com/zh/model-studio/rate-limit> |
| `qwen3.8-max` | LLM（文本生成，2.4 万亿参数 MoE，原生视觉） | 输入 12 元 / 输出 36 元<br>输入（缓存命中）1.5 元<br>显式缓存创建 15 元 / 显式缓存命中 1 元<br>输入（Batch File）6 / 输出（Batch File）18（半价）<br>新加坡：输入 14.988 / 输出 44.965 元 | 元/**百万 Token** | 北京与新加坡采用**动态限流**：TPM 按百炼月消费档位调整，RPM 限额较高、正常使用不触发（具体档位数值本次未获取，见「未核实到」清单）<br>其他地域（法兰克福/弗吉尼亚/东京/香港）RPM 30,000 / TPM 5,000,000 | 上下文长度 1,000,000<br>最大输入长度 991,808（思考模式 983,616）<br>最大输出长度 131,072<br>最大思维链长度 262,144 | <https://help.aliyun.com/zh/model-studio/qwen3-8-max><br><https://help.aliyun.com/zh/model-studio/model-pricing><br><https://help.aliyun.com/zh/model-studio/rate-limit> |

### 免费额度（仅华北 2 北京，有效期 90 天）

| 模型 | 免费额度 | 依据 URL |
|---|---|---|
| `text-embedding-v4` | 100 万 Token | <https://help.aliyun.com/zh/model-studio/embedding> |
| `text-embedding-v3` | **50 万 Token**（`/embedding` 页把 v3 与 v2/v1 合并为「各 50 万」）<br>⚠️ 冲突：`text-embedding-synchronous-api` 页把 v3 与 v4 合并为「各 100 万 Token」 | <https://help.aliyun.com/zh/model-studio/embedding><br><https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api> |
| `qwen3.7-text-embedding` | 100 万 Token（新加坡：无免费额度） | <https://help.aliyun.com/zh/model-studio/embedding><br><https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api> |
| `qwen3.7-text-embedding-flash` | 100 万 Token | <https://help.aliyun.com/zh/model-studio/embedding> |
| `gte-rerank-v2` | **未核实到** | — |
| `qwen3-rerank` | **未核实到** | — |
| `qwen3.7-text-rerank` | **未核实到** | — |
| `qwen-max` | 100 万 Token | <https://help.aliyun.com/zh/model-studio/model-pricing> |
| `qwen-plus` | 100 万 Token | <https://help.aliyun.com/zh/model-studio/model-pricing> |
| `qwen3.7-max` | 100 万 Token | <https://help.aliyun.com/zh/model-studio/model-pricing> |
| `qwen3.8-max` | 100 万 Token | <https://help.aliyun.com/zh/model-studio/model-pricing> |

---

## 二、「未核实到」清单

1. **`gte-rerank-v2` 当前是否仍可调用 —— 未核实到（官方文档自相矛盾，需实测）**
   - 官方[模型下线机制说明](https://help.aliyun.com/zh/model-studio/model-depreciation)把 **2026 年 5 月 30 日列为「已下线」**，指向公告 <https://www.aliyun.com/notice/118217>（标题「【大模型服务平台百炼】GTE-RERANK模型下线通知」）。
   - 但截至 2026-09-27 抓取时：① [排序模型（Rerank）](https://help.aliyun.com/zh/model-studio/text-rerank-api) 页仍将 `gte-rerank-v2` 列入「模型概览」与「支持的模型」，并给出完整 curl / dashscope CLI 示例；② `gte-rerank-v2` 独立模型页仍在架，并给出 0.8 元/百万 Token 价格与 RPM 5040 / TPM 4.98e9；③ 模型列表中仍然只有 `gte-rerank-v2`、没有 `gte-rerank`。官方未解释该矛盾。
2. **`gte-rerank-v2`、`qwen3-rerank`、`qwen3.7-text-rerank` 的免费额度 —— 未核实到具体数值**（重排模型的官方价格表无「免费额度」列；仅能引用通则「每模型通常 100 万 Token」）。
3. **`qwen3.8-max` 在北京/新加坡的具体 RPM、TPM 数值 —— 未核实到**（仅说明「动态限流」：TPM 按百炼月消费档位分档、RPM 较高不触发；档位数值在 [动态限流](https://help.aliyun.com/zh/model-studio/quota-management) 页，本次未抓取到该页数值）。
4. **各官方页面的「更新时间」字段 —— 未核实到**（阿里云帮助中心页首「更新时间：」由前端渲染，抓取结果为空。本文件所有结论以 2026-09-27 抓取到的页面正文为准）。
5. **`text-embedding-v4` / `text-embedding-v3` 的请求 Token 上限口径 —— 冲突未解决**：同步接口 API 页写「单行最大 8,192 Token、最多 10 行」；`/embedding` 页写「单批次最大处理 Token 数 33,000」。二者是「单条上限」与「单批总量上限」的不同口径，还是文档不一致 —— 未核实到官方说明。
6. **`qwen3.7-text-embedding` 的 `dimensions` 是否真的支持 2048 / 1536 —— 冲突未解决**：模型页与 `/embedding` 概览表写「2560、2,048、1,536、1,024（默认）、768、512、256」；但同步接口 API 页的 `dimensions` 参数说明写「2048（**仅适用于** `text-embedding-v4`）、1536（**仅适用于** `text-embedding-v4`）」。未核实到官方澄清。
7. **`text-embedding-v3` 的免费额度 —— 两页冲突未解决**：50 万 Token（`/embedding` 页）vs 100 万 Token（同步接口 API 页）。
8. **`qwen3.7-text-rerank` 新加坡地域的价格与限流 —— 未核实到**（模型页只列华北 2（北京）一列）。（`qwen3-rerank` 有新加坡数据：0.749 元/百万 Token、RPM 5400 / TPM 5,000,000,000。）
9. **`qwen3.7-text-embedding-flash`、`gte-rerank-v2` 的新加坡价格/限流 —— 未核实到**（模型页只列北京）。
10. **2026 年 10 月 10 日下线批次的官方模型名单正文 —— 未核实到**：[模型下线机制说明](https://help.aliyun.com/zh/model-studio/model-depreciation) 只给出 6 个公告链接（118177 / 118434 / 118344 / 118345 / 118331 / 118332），公告页正文由前端渲染、抓取为空。据阿里云开发者社区文章（**非官方文档，仅作线索**，<https://developer.aliyun.com/article/1765739>，2026-09-23）称该批次 179 条覆盖「qwen3-max 全系、qwen-image 系、**qwen-plus 四个快照**、语音模型与 DeepSeek/GLM/Kimi 等第三方条目」。因此本任务核对的 **`qwen-plus` 主线未见于该批次描述**，但无法从官方公告正文确认。
11. **`qwen-max`、`text-embedding-v3` 是否在 2026-10-10 下线名单中 —— 未核实到**（同上，公告正文抓取为空；已查到的官方页面仍正常在架并标价）。

---

## 三、官方弃用时间表

### 3.1 机制（依据 [模型下线机制说明](https://help.aliyun.com/zh/model-studio/model-depreciation)）

- **通知时间**：**快照**模型（名称带日期，如 `qwen-max-2025-01-25`）在正式下线前 **30 天**发布下线通知；**主线**模型在正式下线前 **3 个月**发布下线通知。
- **通知方式**：短信、邮件、站内信、官网公告（短信/邮件/站内信仅面向近 3 个月有待下线模型调用记录的用户）。
- **下线影响**：① 自**下线通知发布之日起**，逐步缩减待下线模型的 QPM 与 TPM（申请过扩容的先恢复至默认限流再缩减），期间 API 与控制台功能仍正常；② 自**正式下线之日起**停止模型推理服务（调用无法返回结果）、不再支持新的调优与部署、控制台相关功能与官方文档同步下线。

### 3.2 官方列出的下线批次（照抄该页「下线模型列表」）

| 批次 | 状态 | 官方公告 |
|---|---|---|
| 2026 年 10 月 10 日 | **将下线** | <https://www.aliyun.com/notice/118177>、<https://www.aliyun.com/notice/118434>、<https://www.aliyun.com/notice/118344>、<https://www.aliyun.com/notice/118345>、<https://www.aliyun.com/notice/118331>、<https://www.aliyun.com/notice/118332> |
| 2026 年 5 月 30 日 | **已下线** | <https://www.aliyun.com/notice/118217> |
| 2026 年 5 月 13 日 | 已下线 | <https://www.aliyun.com/notice/118178> |
| 2026 年 3 月 30 日 | 已下线 | 该批次未发布专项公告 |
| 2026 年 1 月 30 日 | 已下线 | <https://www.aliyun.com/notice/117814> |
| 2025 年 7 月 30 日 | 已下线 | <https://www.aliyun.com/notice/117351> |
| 2025 年 7 月 2 日 | 已下线 | <https://www.aliyun.com/notice/117140> |
| 2025 年 5 月 8 日 | 已下线 | <https://www.aliyun.com/notice/117139> |
| 2024 年 4 月 22 日 | 已下线 | 该批次未发布专项公告 |

### 3.3 与本任务 11 个模型直接相关的条目（照抄）

**① GTE-RERANK 模型下线通知（对应 2026 年 5 月 30 日批次）**

- 公告标题：`【大模型服务平台百炼】GTE-RERANK模型下线通知`
- 公告 URL：<https://www.aliyun.com/notice/118217>（国际站同文：<https://www.alibabacloud.com/en/notice/detail?id=1873>；文档化版本：<https://docs.qwencloud.com/changelog/model-deprecations/gte-rerank>）
- 公告发布时间：`2026-04-28 16:11:12`
- 影响产品：`大模型服务平台百炼`
- 影响时间：`北京时间 2026-05-30 00:00:00 【以实际变更时间为准】`
- 变更内容（国际站/文档版正文原文）：
  > "To continuously enhance model performance and service experience, while optimizing underlying resource management, QwenCloud will deprecate the gte-rerank model on May 30, 2026. We recommend migrating from the gte-rerank model to the qwen3-rerank model as a replacement."
- 影响（原文）：
  > "After the model is deprecated, inference services will no longer be supported, and any applications or services currently calling this model will fail to return results properly. If you are currently using this model, please update your application before May 30, 2026 to avoid disruptions to your production services."
- 帮助中心同步提示（[排序模型（Rerank）](https://help.aliyun.com/zh/model-studio/text-rerank-api) 页顶部 Tip 原文）：
  > 「gte-rerank模型将于2026年05月30日下线，推荐使用qwen3-rerank模型替代。详情请参见官网公告。」
- **命名歧义提醒**：公告与提示写的是 `gte-rerank`（未写 `-v2`）；但该提示被放在同时列出 `gte-rerank-v2` 的排序模型页上，且当前官方模型列表里**只有 `gte-rerank-v2`、没有 `gte-rerank`**。因此「下线的是 v1 还是 v2」无法从公告文本确认。

**② 部分历史主线模型下线通知（对应 2026 年 10 月 10 日批次）**

- 公告标题：`【大模型服务平台百炼】部分历史主线模型下线通知`
- 公告 URL：<https://www.aliyun.com/notice/118177>
- 公告发布时间：`2026-04-13 16:49:54`
- 影响产品：`大模型服务平台百炼`
- 影响时间：`北京时间 2026-10-10 00:00:00 【以实际变更时间为准】`
- ⚠️ 公告正文的具体模型清单未能抓取到（页面正文前端渲染）。该批次共 6 个公告链接，需人工在浏览器打开核对。

**③ 部分历史快照模型下线通知（对应 2026 年 5 月 13 日批次）**

- 公告标题：`【大模型服务平台百炼】部分历史快照模型下线通知`
- 公告 URL：<https://www.aliyun.com/notice/118178>
- 影响时间：`2026-05-13`（该页在「模型下线机制说明」中列为已下线批次）

### 3.4 本任务 11 个模型与上述时间表的对照

| 模型 | 是否出现在已查到的官方下线清单中 |
|---|---|
| `gte-rerank-v2` | **是（存在命名歧义）**：2026-05-30 批次公告标题为 GTE-RERANK，但正文写 `gte-rerank`；`gte-rerank-v2` 目前仍在文档与模型列表中 |
| `text-embedding-v4` | 未出现在已查到的清单中 |
| `text-embedding-v3` | 未出现在已查到的清单中（10-10 批次官方名单正文未核实到） |
| `qwen3.7-text-embedding` | 未出现（2026-07-15 上架新品） |
| `qwen3.7-text-embedding-flash` | 未出现（2026-09-01 上架新品） |
| `qwen3-rerank` | 未出现 |
| `qwen3.7-text-rerank` | 未出现（2026-09-01 上架新品） |
| `qwen-max` | 未出现在已查到的清单中（10-10 批次官方名单正文未核实到） |
| `qwen-plus`（主线） | 未出现；**其快照版本**据非官方线索可能位于 10-10 批次 |
| `qwen3.7-max` | 未出现 |
| `qwen3.8-max` | 未出现（2026-08-02 上架新品） |

上架时间依据 [模型上下架与更新](https://help.aliyun.com/zh/model-studio/newly-released-models)：`qwen3.7-text-embedding` 2026-07-15、`qwen3.7-text-embedding-flash` 2026-09-01、`qwen3.7-text-rerank` 2026-09-01、`qwen3.8-max` 2026-08-02、`gte-rerank-v2` 2025-03-20。

---

## 四、附：两条「特别核实」结论

### 4.1 `qwen3.7-text-embedding` 的 `dimensions` 取值、默认值，及与 `text-embedding-v4` 的关系

- **支持的 `dimensions` 取值**（模型页与 `/embedding` 概览表）：`2560、2048、1536、1024（默认）、768、512、256`
- **默认值**：`1024`
- **输入长度**：单条最长 128,000 Token（`text-embedding-v4` 为 8,192）；单次请求最多 20 条（v4 为 10 条）；上下文长度 131,072
- **与 `text-embedding-v4` 的关系 —— 官方定位为「新一代升级/替代」，但两者当前并列在架、同价**：
  - 模型页原文：「是通义实验室基于 Qwen3.7 训练的多语言文本统一向量模型，**相较 text-embedding-v4 版本在文本检索、聚类、分类性能大幅提升；在 MTEB 多语言、中英、Code 检索等评测任务上效果提升 20%**；支持 256~2560 维用户自定义向量维度。」
  - `/embedding` 页「模型选择」原文：「处理纯文本或代码：**推荐使用 `qwen3.7-text-embedding`。它是当前性能最强的模型**，支持任务指令（instruct）、稀疏向量等高级功能……若更关注成本与吞吐，可使用轻量版 `qwen3.7-text-embedding-flash`。」
  - 同页 instruct 小节原文：「……其中 `qwen3.7-text-embedding` 在指令遵循能力上对比 text-embedding-v4 提升了 16.4%，**建议优先使用**。」
  - 二者**同价**（均为 0.0005 元/千输入 Token，Batch 0.00025 元），且都提供 100 万 Token 免费额度；`text-embedding-v4` 被标注「属于 Qwen3-Embedding 系列」，仍作为在架模型保留。
  - 结论：`qwen3.7-text-embedding` 是官方推荐的**新一代替代品**（性能更高、上下文 8K→128K、批大小 10→20、语种 100+→201），但 `text-embedding-v4` **并未被官方公告下线**，二者当前为「新旧并列、同价」状态。
- **依据 URL**：<https://help.aliyun.com/zh/model-studio/qwen3-7-text-embedding>、<https://help.aliyun.com/zh/model-studio/embedding>、<https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api>

### 4.2 是否存在「Embedding / Rerank 模型弃用公告」页列出各模型下线时间表

- **存在**，官方页面为 [**模型下线机制说明**](https://help.aliyun.com/zh/model-studio/model-depreciation)（属于「产品动态」章节），其「下线模型列表」按批次列出下线日期与对应官网公告链接 —— 完整照抄见上文 **3.2 / 3.3** 两节。
- 与本任务最相关的两条：**2026 年 5 月 30 日已下线**（公告 118217 = GTE-RERANK 模型下线通知）与 **2026 年 10 月 10 日将下线**（6 个公告链接，官方名单正文未抓取到）。
- 该页**只按批次给公告链接，不逐模型列出**；逐模型的下线时间需在官网公告正文或控制台模型广场核对。公告正文由前端渲染，本次抓取为空。
