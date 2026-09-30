# ekbs-admin-web —— 管理台前端（实现契约）

> 依据：`方案/05-管理台UI设计方案.md`（EKBS-DOC-05 v3.0）
> 技术栈（方案 §1.1）：React 18 + TypeScript 5 + Vite 5 + Ant Design Pro 5（`antd` + `@ant-design/pro-components`）
> + TanStack Query v5 + Zustand + React Router 6 + ECharts 5

## 1. 跑起来

```bash
cd ekbs-admin-web
npm install          # 首次
npm run dev          # http://127.0.0.1:3000（/api 代理到 127.0.0.1:8000）
npm run typecheck    # tsc -b --noEmit —— 改完必须过
npm test             # vitest run
npm run build        # 产出 dist/，由 ekbs-api 以 /admin 前缀托管
```

管理台挂在 **`/admin`**（`vite.config.ts` 的 `base` 与 router 的 `basename` 都是它）。

## 2. 首期范围（**只做这 6 个模块**）

方案 §1.5 定的「自研 6」；其余模块**不建入口**（§3.4 约束 2：预留权限点的入口首期不渲染）。

| # | 模块 | 路由 | 页面文件 |
|---|---|---|---|
| ① | 权限管理 | `/iam/users` `/iam/roles` `/iam/depts` `/iam/acl` `/iam/perm-changes` | `src/pages/iam/*` |
| ② | 问答日志与审计 | `/logs/qa` `/logs/qa/:sessionId` `/logs/audit` `/logs/doc-access` | `src/pages/logs/*` |
| ③ | 检索调试台 | `/rag/playground` | `src/pages/rag/Playground.tsx` |
| ④ | 概览看板 | `/dashboard` | `src/pages/dashboard.tsx` |
| ⑤ | 文档管理（含上传） | `/docs/list` `/docs/upload` `/docs/:docId` | `src/pages/docs/*` |
| ⑥ | 解析任务中心（含分块预览与编辑） | `/parse/tasks` `/chunks/:docId` | `src/pages/parse/*`、`src/pages/chunks/*` |

**不在首期**：知识库管理、目录树、模型/Prompt 配置、评估标注、数据源接入、系统设置、告警配置。
这些的后端接口也大多不存在（见 `docs/管理台-方案符合性检查.md`），不要为它们写页面。

## 3. 可用的共享设施（**只读，不要改**）

### API 层（`src/api/`，函数签名即契约）

| 模块 | 函数 |
|---|---|
| `auth` | `getMe()` `getPermContext()` `devToken(userId)` |
| `doc` | `listDocuments({limit,offset,kb_id,status,level,q})` `getDocument(id)` `updateDocument(id,body)` `publishDocument(id,reason?)` `unpublishDocument(id,reason?)` `offlineDocument(id,reason?)` `deleteDocument(id)` `batchDocuments(ids,action,reason?)` `uploadInit(body)` `uploadChunk(uploadId,index,blob,filename)` `uploadComplete(uploadId)` |
| `parse` | `listParseTasks({limit,offset,status,kb_id})` `getParseTask(id)` `getParseResult(id)` `retryParseTask(id)` `cancelParseTask(id)` `getParseConfig()` `getChunkTree(docId)` `getChunk(chunkId)` `updateChunk(id,{content,reason?})` `splitChunk(id,body?)` `mergeChunk(id,body?)` `reembedChunk(id)` `revectorizeDocument(docId)` |
| `rag` | `debugQuery(body)` `debugRetrieve(body)` `debugRewrite(body)` `debugSimulate(body)` `debugShards()` `debugHistory()` |
| `iam` | `listUsers` `updateUser` `setClearance` `listRoles` `createRole` `updateRole` `deleteRole` `assignRole` `listDepts` `setDeptMapping` `listAcl` `createAcl` `deleteAcl` `simulateAcl` `listPermChanges` |
| `log` | `listQaSessions` `getQaSession(id)` `listDocAccess` `listSecurityEvents` |
| `system` | `getOverview()` `getHealth()` `listShards()` `listAlertRules()` `rebuildIndex(body?)` |

DTO 类型在 `src/api/types.ts`（**按实测响应手写**，字段与后端逐字对应）。

### 共享组件 / Hook

| 名称 | 用途 |
|---|---|
| `@/components/ClearanceTag` | 密级徽标 L1–L4（`<ClearanceTag level={2} clearance={3} />`）—— **所有列表/详情都必须用**（方案 §5.1 强制 + 原则 P4） |
| `@/components/StatusTag` | 状态语义标签（`published/parsing/pending/failed/offline`…） |
| `@/components/Auth` | 按钮级权限：`<Auth code="doc:document:publish">…</Auth>`（无权限**不渲染**，不是置灰） |
| `@/components/EmptyState` | 统一空态（§5.4 文案模板） |
| `@/components/ErrorBoundary` | 路由级错误边界（已挂在 router 上） |
| `@/hooks/usePermission` | `{has,hasAll,hasAny,isRole,clearance,canAccessLevel,fingerprint}` |
| `@/hooks/useAuth` | `{me,token,clearance,refresh,loginAsDev,logout,isAuthed}` |
| `@/stores/authStore` | `accessToken / me / clearance / policyVersion / permFingerprint / visibleShards` |
| `@/utils/format` | `formatBytes formatNumber formatPercent formatTime formatMs` |

### 后端报文的**真实形状**（方案 §6.3 设想的 `{code,message,data}` 包体**不存在**）

`ekbs-api` 多数端点返回裸 JSON；错误是 `{"detail": "..."}`（字符串，偶尔是校验数组）。
请求层 `src/api/client.ts` 已把两种情况都归一化，并抛 `ApiError{message,status,code,traceId}`。

列表端点统一返回 `{ items: T[], total: number, limit?, offset? }`：

| 端点 | 额外字段 |
|---|---|
| `GET /parse/tasks` | `by_status`、`success_rate`、`success_rate_target`、`note` |
| `GET /perm/roles` | `builtin_codes: string[]` |
| `GET /perm/audit` | `retention_days` |
| `GET /chunks/documents/{id}/tree` | `document`、`parents`、`children`、`tree`、`orphan_children` |
| `GET /dashboard/overview` | `qa_total refused_total refusal_rate documents shards disk_used_pct metrics active_alerts recent_alert_events` |
| `GET /rag/debug/shards` | `user_id clearance dept_chain visible_shards acl_allow_shards acl_deny_shards matched_dept_codes registered_shards warnings` |

`GET /docs/documents` 的文档字段：`doc_id kb_id kb_code title level status visible_dept_ids page_count doc_version effective_date expire_date updated_at shard_id chunk_count size_bytes source_path`。

## 4. 权限点（**照抄，别自己发明**）

命名规范（§3.1）：`<模块>:<对象>:<动作>`，模块前缀是 10 个封闭集合
（`kb doc parse rag perm model eval audit ds sys`）——**不得新增前缀**。

首期这 6 个模块会用到的：`sys:dashboard:read`、`doc:document:read/create/update/delete/publish/offline/reparse`、
`doc:chunk:read/update`、`parse:task:read/manage`、`rag:playground:run`、`perm:user:read/update`、
`perm:role:read`、`perm:dept:read`、`perm:acl:read/create/delete`、`perm:audit:read`、
`audit:qa:read`、`audit:qa:read_all`、`audit:trace:read`。

> ⚠️ 后端目前**只有 41 个权限点**，首期方案要求 57 个（缺 22 个，见符合性检查报告）。
> 因此某些按钮的权限点在后端可能还不存在 —— **不要因此自己造权限码**；照方案写，
> 后端补齐后即生效；页面要能容忍"无权限"（`Auth` 组件不渲染即可）。

## 5. 设计规范（**硬性**，来自 §5）

| 项 | 要求 |
|---|---|
| 密级徽标 | §5.1「强制」：L1 绿 / L2 蓝 / L3 琥珀+⚠ / L4 红+🔒；密级不足整行 `opacity:.55` + 锁 |
| 状态语义色 | 已发布 success / 进行中 processing / 待审 warning / 失败 error / 停用 default |
| 表格 | 分页默认 20（20/50/100）；空态用 `EmptyState`；长文本两行省略（`.ekbs-ellipsis-2`）；失败行左侧红条（`.ekbs-row-failed`） |
| 表单 | `layout="vertical"`，必填项前置红 `*`，控件间距 16px |
| 抽屉/弹窗 | 表单抽屉 480px，详情抽屉 720px，对比 960px；确认弹窗 420px |
| 破坏性确认 | §5.5 分三级：L-A 常规 Modal；L-B 展示影响清单 + 勾选"我已知晓影响"；L-C 输入对象名 + 原因 ≥10 字 |
| 空态文案 | §5.4：`还没有{对象}，{下一步动作}`；筛选无结果用 `没有找到符合条件的{对象}` |
| 无权限 | 页面级 `/403`；按钮级 Toast `无操作权限（{code}），请联系知识库管理员` |
| 加载 | 首屏骨架（ProTable 自带），翻页用表头 loading，不整页 Spin |
| 危险按钮 | 不放在主操作首位；行内危险操作收进 `···` |

## 6. 开发约定

* **只改自己负责的 `src/pages/**` 文件**；共享层（`api/ components/ hooks/ stores/ router/ layouts/ utils/`）如需改动，
  在答复里说明原因，不要直接改（避免并行开发互相踩）。
* 改完必须过 `npx tsc -b --noEmit`；新增纯函数（如数据整形/权限判断）请补 `*.test.ts`（vitest）。
* 中文 UI 文案；不要在 JSX 里硬编码英文。
* 不要为了让页面"看起来有数据"而造假数据 —— 接口没有的字段就不显示。
