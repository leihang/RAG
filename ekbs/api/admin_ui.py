"""常规管理台（免构建单页）—— 挂在 `/admin`。

背景与定位
----------
`方案/05-管理台UI设计方案.md` 规划的是 React 18 + TS + Vite + Ant Design Pro 5 的独立
前端工程，**本仓库一行都没实现**。为了先把「常规管理」从敲 curl 变成点界面，这里提供一个
**零依赖、零构建、随 API 一起部署**的单页管理台：后端 47 条管理接口全都已就绪并有测试，
缺的只是前端。

与原方案的关系
--------------
* 这是**功能原型 + 日常运维界面**，不是 `05` 的像素级实现（无设计稿、无组件库、无 E2E）；
* 钉钉远程打开 `/admin`（SPA 或本页回落）均可做扫描件人工复核（与 React 管理台同一 API）；
* 菜单按**权限点**过滤：登录后读 `/auth/me` 的 `permissions[]`，没有的点位直接不显示；
* 接口返回 403 时页面显示「无权限」而不是崩掉（如 `kb_admin` 看不到安全事件与访问日志）。

安全边界
--------
鉴权完全复用后端：`/auth/dev-token`（**仅 dev 环境**）或钉钉免登换来的 JWT。
页面自身不含任何密钥，也不放宽任何权限判断 —— 它只是后端接口的调用方。
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EKBS 管理台</title>
<style>
  /* 设计令牌逐字取自 方案 05 §5.1（Ant Design Pro 5 默认令牌 + 企业视觉）。
     注意：方案规定的是**浅色**主题，此处不再是暗色自配色。 */
  :root{--bg:#f5f7fa;--card:#ffffff;--line:#f0f0f0;--fg:rgba(0,0,0,.88);--dim:rgba(0,0,0,.45);
        --ok:#52c41a;--bad:#ff4d4f;--warn:#faad14;--accent:#1677ff;--side:#ffffff;
        --shadow:0 1px 2px rgba(0,0,0,.06)}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei","Source Han Sans SC",Helvetica,sans-serif}
  .layout{display:flex;min-height:100vh}
  /* 侧栏 240px（收起 64px，ICON 模式）—— 方案 §2.2 布局规格 */
  aside{width:240px;flex:none;background:var(--side);border-right:1px solid var(--line);padding:14px 0}
  aside h1{font-size:15px;margin:0 16px 14px;color:var(--fg)}
  aside .who{font-size:12px;color:var(--dim);margin:0 16px 14px;line-height:1.5}
  aside a{display:block;padding:9px 16px;color:var(--fg);text-decoration:none;font-size:13.5px;
          border-radius:6px;margin:0 8px}
  aside a:hover{background:#f5f7fa}
  aside a.on{background:#e6f4ff;color:var(--accent);font-weight:600}
  main{flex:1;padding:16px 24px;min-width:0}
  h2{font-size:16px;font-weight:600;margin:0 0 12px}
  /* 卡片：圆角 8px + 阴影，**无边框**（§5.2） */
  .card{background:var(--card);border-radius:8px;padding:16px 24px;margin-bottom:16px;
        box-shadow:var(--shadow)}
  .breadcrumb{height:40px;display:flex;align-items:center;gap:6px;color:var(--dim);font-size:13px}
  .breadcrumb b{color:var(--fg);font-weight:600}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px}
  .kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
  .kpi b{display:block;font-size:22px;font-weight:600;margin-top:2px}
  .kpi span{color:var(--dim);font-size:12px}
  label{color:var(--fg);font-size:14px}
  select,input,button,textarea{background:#fff;color:var(--fg);border:1px solid #d9d9d9;
       border-radius:6px;padding:5px 11px;font-size:14px;height:32px}
  select:focus,input:focus{outline:none;border-color:var(--accent);
       box-shadow:0 0 0 2px rgba(22,119,255,.12)}
  input[type=text]{min-width:200px}
  button{cursor:pointer;background:var(--accent);border-color:var(--accent);color:#fff;
       font-weight:400;box-shadow:0 2px 0 rgba(5,145,255,.1)}
  button.ghost{background:transparent;color:var(--fg);border-color:var(--line);font-weight:400}
  button.mini,a.mini{padding:2px 8px;font-size:12px;font-weight:400;display:inline-block;
       text-decoration:none;line-height:1.4;height:auto}
  a.mini{background:var(--accent);border:1px solid var(--accent);color:#fff;border-radius:6px}
  a.mini.ghost{background:transparent;color:var(--fg);border-color:var(--line)}
  button:disabled{opacity:.5;cursor:not-allowed}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:12px 12px;border-bottom:1px solid var(--line);vertical-align:middle}
  th{color:var(--fg);font-weight:600;position:sticky;top:0;background:#fafafa}
  tbody tr:hover{background:#f5f7fa}
  .scroll{max-height:60vh;overflow:auto;border-radius:8px}
  .tag{display:inline-block;padding:1px 7px;border-radius:20px;font-size:11px;border:1px solid var(--line);color:var(--dim);margin-right:5px}
  .ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
  pre{white-space:pre-wrap;word-break:break-word;font-size:12px;color:var(--dim);margin:6px 0 0}
  .tabs{display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap}
  .tabs button{background:transparent;color:var(--dim);border-color:var(--line);font-weight:400}
  .tabs button.on{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600}
  /* 侧栏常驻快捷入口：比普通菜单项更醒目（主行动按钮） */
  aside a.quick{display:block;margin:0 12px 10px;padding:9px 12px;border-radius:6px;
       background:var(--accent);color:#fff;font-weight:600;text-align:center}
  aside a.quick:hover{filter:brightness(1.08);background:var(--accent)}
  /* 拖拽上传区：虚线框 + 悬停高亮，让"选文件"这件事一眼可见 */
  .drop{border:1.5px dashed var(--line);border-radius:10px;padding:22px 16px;text-align:center;
        color:var(--dim);cursor:pointer;transition:border-color .15s,background .15s}
  .drop:hover,.drop.over{border-color:var(--accent);background:#f0f7ff;color:var(--fg)}
  .drop b{color:var(--fg);font-size:14px}
  .drop .hint{font-size:12px;margin-top:4px}
  .field{display:flex;flex-direction:column;gap:4px;margin-bottom:10px}
  .grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}
  .toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:4px}
  .filelist{margin-top:10px;font-size:12.5px}
  .filelist div{padding:3px 0;border-bottom:1px dashed var(--line)}
  /* 视觉隐藏但**仍可被 label 触发**的原生文件选择框；
     用 display:none 在个别浏览器上会点不开，所以用 1px + opacity */
  .sr{position:absolute;width:1px;height:1px;opacity:0;overflow:hidden}
  /* 密级徽标与状态色（方案 05 §5.1 的色值，逐字取自该表） */
  .lvl{display:inline-block;padding:0 7px;border-radius:4px;font-size:12px;line-height:20px;
       border:1px solid transparent;white-space:nowrap}
  .lvl.l1{color:#389e0d;background:#f6ffed;border-color:#b7eb8f}
  .lvl.l2{color:#0958d9;background:#e6f4ff;border-color:#91caff}
  .lvl.l3{color:#d46b08;background:#fffbe6;border-color:#ffe58f}
  .lvl.l4{color:#cf1322;background:#fff1f0;border-color:#ffa39e}
  .lvl.locked{opacity:.55}
  .st{display:inline-block;padding:0 7px;border-radius:10px;font-size:12px;line-height:20px}
  .st.success{color:#389e0d;background:#f6ffed}
  .st.processing{color:#0958d9;background:#e6f4ff}
  .st.warning{color:#d46b08;background:#fffbe6}
  .st.error{color:#cf1322;background:#fff1f0}
  .st.default{color:var(--dim);background:#f0f0f0}
  .hidden{display:none}
  .muted{color:var(--dim);font-size:12px}
</style>
</head>
<body>
<div class="layout">
  <aside>
    <h1>EKBS 管理台</h1>
    <div class="who" id="who">未登录</div>
    <!-- 常驻快捷入口：放在 #menu 之外，避免 buildMenu() 重画时被冲掉。
         「导入」曾只作为一个菜单项且导航失效，用户反馈"选导入文件没有入口"。 -->
    <a class="quick" id="quickImport" href="#/import/upload" data-menu="import">＋ 导入文件</a>
    <div id="menu"></div>
    <div style="padding:10px 14px">
      <div class="muted" style="margin-bottom:6px">切换身份（dev）</div>
      <select id="userSel" style="width:100%"></select>
      <button class="ghost mini" id="relogin" style="margin-top:6px;width:100%">重新登录</button>
    </div>
  </aside>
  <main id="main"><div class="card">正在登录…</div></main>
</div>
<script>
const API = "/api/v1";
let token = null, me = null, current = null;

/* 菜单 → 需要的权限点（没有该点位就不显示） */
const MENUS = [
  {id:"overview", name:"概览看板", perm:"sys:dashboard:read"},
  {id:"docs",     name:"文档管理", perm:"doc:document:read"},
  {id:"import",   name:"导入", perm:"doc:document:create"},
  {id:"perm",     name:"权限管理", perm:"perm:user:read"},
  {id:"chunks",   name:"分块编辑", perm:"doc:chunk:read"},
  {id:"parse",    name:"解析任务", perm:"parse:task:read"},
  {id:"debug",    name:"检索调试台", perm:"rag:playground:run"},
  {id:"system",   name:"系统与索引", perm:"sys:health:read"},
  {id:"audit",    name:"审计与日志", perm:"perm:audit:read"},
];

/* 导入页的跨渲染状态：render() 会整页重画，所以表单值必须存在这里，
   否则每点一次「进入目录」用户填的密级/部门就被清空了。 */
const IMP = {rel:"", meta:null, scan:null, level:1, dept:"", kbId:"", recursive:true, busy:false};

async function api(path, opts = {}) {
  const headers = Object.assign({"Content-Type":"application/json"}, opts.headers || {});
  if (token) headers["Authorization"] = "Bearer " + token;
  const res = await fetch(API + path, Object.assign({}, opts, {headers}));
  const text = await res.text();
  let body; try { body = JSON.parse(text); } catch(e) { body = {detail:text}; }
  if (!res.ok) throw Object.assign(new Error(body.detail || res.statusText), {status:res.status, body});
  return body;
}

const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const cell = (v) => {
  if (v == null || v === "") return "";
  if (typeof v === "object") return esc(JSON.stringify(v)).slice(0, 160);
  if (typeof v === "boolean") return v ? '<span class="ok">是</span>' : '<span class="warn">否</span>';
  return esc(v);
};

/* 通用表格：列从第一行的键推导，避免为十几个接口逐个手写表头 */
function table(rows, opts = {}) {
  if (!rows || !rows.length) return '<div class="muted">（无数据）</div>';
  const keys = opts.cols || Object.keys(rows[0]);
  const head = keys.map(k => `<th>${esc(k)}</th>`).join("");
  const body = rows.map((r, i) => "<tr>" + keys.map(k => `<td>${cell(r[k])}</td>`).join("") +
    (opts.actions ? `<td>${opts.actions(r, i)}</td>` : "") + "</tr>").join("");
  const actHead = opts.actions ? "<th>操作</th>" : "";
  return `<div class="scroll"><table><thead><tr>${head}${actHead}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function kpi(label, value, extra) {
  return `<div class="kpi"><span>${esc(label)}</span><b>${esc(value)}</b>${extra ? `<span>${esc(extra)}</span>` : ""}</div>`;
}

/* 密级色标（方案 05 §5.1「强制」+ 原则 P4「密级可见」）：
   L1 公开 / L2 内部 / L3 秘密(⚠) / L4 机密(🔒)。
   **任何列表里的文档都必须带徽标**，不允许"密级不可见"。 */
const LEVEL_META = {
  1: {name:"公开", cls:"l1"},
  2: {name:"内部", cls:"l2"},
  3: {name:"秘密", cls:"l3", icon:"⚠"},
  4: {name:"机密", cls:"l4", icon:"🔒"},
};
function levelTag(level, clearance) {
  const lv = Number(level);
  const meta = LEVEL_META[lv];
  if (!meta) return esc(level == null ? "" : String(level));
  const icon = meta.icon ? " " + meta.icon : "";
  // 密级不足（文档 level > 当前用户 clearance）：加锁并说明所需密级
  const insufficient = clearance != null && lv > Number(clearance);
  const title = insufficient ? ` title="需 L${lv} 及以上密级（当前 L${clearance}）"` : "";
  return `<span class="lvl ${meta.cls}${insufficient ? " locked" : ""}"${title}>L${lv} ${meta.name}${icon}${insufficient ? " 🔒" : ""}</span>`;
}

/* 状态语义色（§5.1 状态语义色表） */
const STATUS_META = {
  published: ["success", "已发布"], parsing: ["processing", "解析中"],
  indexing: ["processing", "索引中"], pending: ["processing", "待解析"],
  review: ["warning", "待复核"], failed: ["error", "失败"], offline: ["default", "已下线"],
};
function statusTag(status) {
  const meta = STATUS_META[String(status)] || ["default", String(status)];
  return `<span class="st ${meta[0]}">${esc(meta[1])}</span>`;
}

/* ── 各页面 ───────────────────────────────────────────────────────────── */
const VIEWS = {};

VIEWS.overview = async () => {
  const [ov, health] = await Promise.all([api("/dashboard/overview"), api("/system/health")]);
  const cards = [
    kpi("问答总数", ov.qa_total), kpi("拒答数", ov.refused_total),
    kpi("拒答率", (ov.refusal_rate == null ? "-" : (ov.refusal_rate * 100).toFixed(1) + "%")),
    kpi("文档数", ov.documents), kpi("分片数", ov.shards),
    kpi("磁盘占用", ov.disk_used_pct + "%"), kpi("活跃告警", ov.active_alerts),
  ].join("");
  const metrics = ov.metrics && typeof ov.metrics === "object"
    ? table(Object.entries(ov.metrics).map(([k, v]) => ({ 指标: k, 值: v })), {cols:["指标","值"]}) : "";
  const alertEvents = (ov.recent_alert_events || []).length
    ? table(ov.recent_alert_events) : '<div class="muted">近期无告警事件</div>';
  return `<h2>概览看板</h2>
    <div class="cards">${cards}</div>
    <div class="card" style="margin-top:14px"><b>平台健康</b>${table([health])}</div>
    <div class="card"><b>运行指标</b>${metrics}</div>
    <div class="card"><b>近期告警</b>${alertEvents}</div>`;
};

VIEWS.docs = async () => {
  const data = await api("/docs/documents?limit=100");
  const raw = data.items || [];
  const rows = raw.map(r => Object.assign({}, r, {
    // 密级徽标 + 状态语义色（方案 05 §5.1「强制」/ 原则 P4：列表里必须看得到密级）
    level: levelTag(r.level, me && me.clearance),
    status: statusTag(r.status),
  }));
  const actions = (r) => {
    const id = r.doc_id ?? r.id;
    const st = String(r.status);
    const pub = st === "published" ? "" : `<button class="mini ghost" data-act="publish" data-id="${id}">发布</button>`;
    const off = st === "offline" ? "" : `<button class="mini ghost" data-act="offline" data-id="${id}">下线</button>`;
    return pub + " " + off + ` <button class="mini ghost" data-act="tree" data-id="${id}">分块</button>`;
  };
  const cols = ["doc_id","title","level","status","page_count","chunk_count","doc_version","updated_at"];
  return `<h2>文档管理 <span class="muted">共 ${data.total} 篇，显示 ${rows.length}</span></h2>
    <div class="card">${table(rows, {actions, cols})}</div>
    <div class="muted">上传入口见左栏「＋ 导入文件」（按方案 §4.4 的上传页设计）。</div>`;
};

VIEWS.perm = async (sub) => {
  const tabs = [["users","用户"],["roles","角色"],["depts","部门"],["acl","ACL"],["audit","变更审计"],["simulate","权限模拟"]];
  const tabHtml = tabs.map(([k,n]) => `<button data-tab="${k}" class="${sub===k?"on":""}">${n}</button>`).join("");
  let body = "";
  if (sub === "roles") {
    const d = await api("/perm/roles");
    body = table(d.items || []) + `<div class="muted">内置角色（禁删）：${esc((d.builtin_codes||[]).join("、"))}</div>`;
  } else if (sub === "depts") {
    const d = await api("/perm/depts");
    body = table(d.items || []) + `<pre>dept_code_map = ${esc(JSON.stringify(d.dept_code_map))}</pre>
      <div class="muted">${esc(d.note || "")}（只读；写接口 PUT /perm/depts/{id}/mapping 见方案争议项）</div>`;
  } else if (sub === "acl") {
    const d = await api("/perm/acl");
    body = table(d.items || []) + `<div class="muted">共 ${d.total} 条</div>`;
  } else if (sub === "audit") {
    const d = await api("/perm/audit");
    body = table(d.items || []) + `<div class="muted">保留 ${d.retention_days} 天</div>`;
  } else if (sub === "simulate") {
    body = `<div class="row"><label>用户 id</label><input id="simUser" type="text" value="2" style="min-width:70px">
      <label>文档 id（逗号分隔）</label><input id="simDocs" type="text" value="1,2,3">
      <button data-act="simulate">模拟</button></div><div id="simOut"></div>`;
  } else {
    const d = await api("/perm/users?limit=100");
    const actions = (r) => {
      const id = r.user_id ?? r.id;
      return `<button class="mini ghost" data-act="clearance" data-id="${id}" data-cur="${r.clearance}">改密级</button>`;
    };
    body = table(d.items || [], {actions}) + `<div class="muted">共 ${d.total} 人</div>`;
  }
  return `<h2>权限管理</h2><div class="tabs">${tabHtml}</div><div class="card">${body}</div>`;
};

VIEWS.chunks = async (sub) => {
  const docs = await api("/docs/documents?limit=100");
  const id = sub || (docs.items && docs.items[0] && (docs.items[0].doc_id ?? docs.items[0].id));
  const options = (docs.items || []).map(d => {
    const did = d.doc_id ?? d.id;
    return `<option value="${did}" ${String(did)===String(id)?"selected":""}>${esc(did)} · ${esc(d.title)}</option>`;
  }).join("");
  let body = '<div class="muted">选择文档查看分块树</div>';
  if (id) {
    try {
      const tree = await api(`/chunks/documents/${id}/tree`);
      body = table(tree.items || tree.nodes || (Array.isArray(tree) ? tree : []));
    } catch (e) { body = `<div class="bad">${esc(e.message)}</div>`; }
  }
  return `<h2>分块编辑</h2><div class="card"><div class="row">
      <label>文档</label><select id="chunkDoc">${options}</select>
      <button class="ghost" data-act="reloadChunks">加载</button>
      <span class="muted">改子块 PUT /chunks/{id}；拆/合/重嵌入见后端接口</span>
    </div>${body}</div>`;
};

VIEWS.parse = async () => {
  /* 钉钉远程打开 /admin（免构建页或 SPA 未构建时）也必须能做扫描件人工复核 */
  const d = await api("/parse/tasks?limit=100");
  const reviewN = (d.by_status && d.by_status.review) || 0;
  const actions = (r) => {
    const id = r.task_id ?? r.id;
    // 列表展示会把 status 换成徽标 HTML，动作判断用原始字段 _rawStatus
    const st = String(r._rawStatus || "");
    const reviewBtn = st === "review"
      ? `<a class="mini" href="/admin/parse/review/${id}">查看清洗稿</a>
         <button class="mini ghost" data-act="reject" data-id="${id}">驳回</button>`
      : `<a class="mini ghost" href="/admin/chunks/${id}">打开分块</a>`;
    return `${reviewBtn}
            <button class="mini ghost" data-act="retry" data-id="${id}">重跑</button>
            <button class="mini ghost" data-act="cancel" data-id="${id}">取消</button>`;
  };
  const rows = (d.items || []).map(r => Object.assign({}, r, {
    _rawStatus: r.status,
    status: statusTag(r.status),
  }));
  const byStatus = d.by_status ? Object.entries(d.by_status).map(([k,v]) => ({状态:k, 数量:v})) : [];
  const reviewBanner = reviewN
    ? `<div class="warn" style="margin:8px 0">有 <b>${reviewN}</b> 份扫描件待人工复核：点「查看清洗稿」进入 SPA 双栏对照（原件↔清洗稿），确认通过后才会分块入库。</div>`
    : "";
  return `<h2>解析任务</h2>
    <div class="card"><div class="row">
      <span class="tag">总数 ${d.total ?? 0}</span>
      <span class="tag">待复核 ${reviewN}</span>
      <span class="tag">成功率 ${d.success_rate != null ? (d.success_rate*100).toFixed(1)+"%" : "-"}</span>
      <span class="tag">目标 ${d.success_rate_target != null ? (d.success_rate_target*100).toFixed(0)+"%" : "-"}</span>
    </div>${reviewBanner}${table(byStatus, {cols:["状态","数量"]})}
    <pre>${esc(d.note || "")}</pre>
    <div class="muted">扫描件复核已迁至管理台 SPA：<code>/admin/parse/review/&lt;doc_id&gt;</code>。本页不再弹全文预览。</div></div>
    <div class="card">${table(rows, {actions})}</div>`;
};

VIEWS.debug = async () => {
  const shards = await api("/rag/debug/shards");
  const info = `<div class="card"><b>当前身份的分片路由</b>
    <div class="row" style="margin-top:6px">
      <span class="tag">user ${shards.user_id}</span><span class="tag">密级 L${shards.clearance}</span>
      <span class="tag">可见分片 ${(shards.visible_shards||[]).join(", ") || "无"}</span>
      <span class="tag">ACL 允许 ${(shards.acl_allow_shards||[]).length} / 拒绝 ${(shards.acl_deny_shards||[]).length}</span>
    </div><pre>${esc(JSON.stringify(shards.registered_shards, null, 1))}</pre></div>`;
  return `<h2>检索调试台</h2>${info}
    <div class="card"><div class="row">
      <input id="dbgQ" type="text" placeholder="输入查询，例如：年假有多少天" style="flex:1">
      <label><input type="checkbox" id="dbgRerank" checked> 精排</label>
      <button data-act="debug">检索</button>
    </div><div id="dbgOut"></div></div>`;
};

VIEWS.system = async () => {
  const [health, shards, rules] = await Promise.all([
    api("/system/health"), api("/system/shards"), api("/system/alert-rules")]);
  const rulesRows = (rules.items || []).map(r => ({
    编号:r.code ?? r.id, 级别:r.severity, 名称:r.name, 阈值:r.threshold, 说明:r.description}));
  return `<h2>系统与索引</h2>
    <div class="card"><b>健康</b>${table([health])}</div>
    <div class="card"><b>分片（${shards.count ?? 0}）</b>${table(shards.items || [])}</div>
    <div class="card"><b>告警规则（${rulesRows.length}）</b>${table(rulesRows, {cols:["编号","级别","名称","阈值","说明"]})}</div>`;
};

VIEWS.audit = async (sub) => {
  const tabs = [["sec","安全事件"],["access","文档访问日志"],["qa","问答会话"]];
  const tabHtml = tabs.map(([k,n]) => `<button data-tab="${k}" class="${sub===k?"on":""}">${n}</button>`).join("");
  const path = sub === "access" ? "/logs/doc-access" : sub === "qa" ? "/logs/qa/sessions" : "/audit/security-events";
  let body;
  try {
    const d = await api(path + (path.includes("?") ? "&" : "?") + "limit=100");
    body = table(d.items || []) + (d.total != null ? `<div class="muted">共 ${d.total}</div>` : "");
  } catch (e) {
    body = `<div class="warn">无权限或读取失败：${esc(e.message)}（HTTP ${e.status || "?"}）</div>
      <div class="muted">当前身份所需权限点可能未授予；用超级管理员身份重试。</div>`;
  }
  return `<h2>审计与日志</h2><div class="tabs">${tabHtml}</div><div class="card">${body}</div>`;
};

/* ── 导入：服务器目录导入（页签一） ────────────────────────────────────── */

function impCrumb(rel) {
  const parts = (rel || "").split("/").filter(Boolean);
  const segs = [`<a href="#" data-act="impGo" data-rel="">raw</a>`];
  let acc = "";
  parts.forEach(p => {
    acc = acc ? acc + "/" + p : p;
    segs.push(`<span class="muted">/</span><a href="#" data-act="impGo" data-rel="${esc(acc)}">${esc(p)}</a>`);
  });
  return segs.join(" ");
}

function impTargetForm(meta) {
  const kbs = (meta.knowledge_bases || []).map(k =>
    `<option value="${k.kb_id}" ${String(IMP.kbId)===String(k.kb_id)?"selected":""}>${esc(k.code)} · ${esc(k.name)}</option>`).join("");
  const lv = [1,2,3,4].map(n =>
    `<option value="${n}" ${IMP.level===n?"selected":""}>L${n}${n===1?"（公司内部公开）":n===4?"（绝密）":""}</option>`).join("");
  return `<div class="card"><b>导入参数</b>
    <div style="display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end;margin-top:8px">
      <div><label>知识库</label><br><select id="impKb">${kbs}</select></div>
      <div><label>密级</label><br><select id="impLevel">${lv}</select></div>
      <div><label>可见部门 ID（逗号分隔，可空）</label><br>
        <input id="impDept" type="text" value="${esc(IMP.dept)}" placeholder="102" style="width:180px"></div>
      <div><label><input id="impRecursive" type="checkbox" ${IMP.recursive?"checked":""}> 含子目录</label></div>
      <div><button data-act="impScan">扫描预览</button>
           <button class="ghost" data-act="impRun">执行导入</button></div>
    </div>
    <div class="muted" style="margin-top:8px">目录导入支持 ${meta.dir_extensions.length} 种格式（含代码后缀）；
      解析由 worker 异步进行，进度见「解析任务」页。</div></div>`;
}

function impScanOut(scan) {
  if (!scan) return "";
  const c = scan.counts || {};
  const blockers = (scan.blockers || []).length
    ? `<div class="bad" style="margin:8px 0">⛔ 现在不能导入：<ul>${scan.blockers.map(b => `<li>${esc(b)}</li>`).join("")}</ul>
       <div class="muted">合规闸需在配置文件里把 <code>permission.corpus_egress_ack</code> 置 true
       （真实语料入库即出网，需书面确认）。</div></div>` : "";
  const warn = (scan.warn || []).length
    ? `<div class="warn">${scan.warn.map(w => esc(w)).join("<br>")}</div>` : "";
  const rows = (scan.preview || []).map(r => ({
    文件: r.rel_path,
    大小: (r.size_bytes/1024).toFixed(1) + " KB",
    结论: r.action === "new" ? '<span class="ok">将导入</span>'
        : r.action === "duplicate" ? '<span class="muted">跳过（已存在）</span>'
        : `<span class="warn">${esc(r.action)}</span>`,
    说明: r.reason || "",
  }));
  return `<div class="card"><b>扫描结果</b>${blockers}${warn}
    <div class="cards" style="margin:8px 0">
      ${kpi("目录内文件", scan.total_files)}${kpi("可导入", scan.importable)}
      ${kpi("重复跳过", c.duplicate || 0)}${kpi("超限跳过", c.too_large || 0)}
    </div>
    ${rows.length ? table(rows, {cols:["文件","大小","结论","说明"]}) : '<div class="muted">没有可预览的文件</div>'}
    ${scan.preview_truncated ? `<div class="muted">仅显示前 ${scan.preview_limit} 条（导入时按全量执行，不受此限制）</div>` : ""}
  </div>`;
}

VIEWS.import = async () => {
  // 默认落在「上传文件」：用户要的是"选文件导入"，不该先看到目录树
  const sub = location.hash.split("/")[2] || "upload";
  const tabs = [["upload","上传文件"],["dir","服务器目录导入"]];
  const tabHtml = tabs.map(([k,n]) => `<button data-tab="${k}" class="${sub===k?"on":""}">${n}</button>`).join("");
  if (!IMP.meta) IMP.meta = await api("/import/meta");
  const body = sub === "dir" ? await impDirPane() : await impUploadPane();
  return `<h2>导入 <span class="muted">文件先放进服务器 raw 目录，再从界面导入</span></h2>
    <div class="tabs">${tabHtml}</div>${body}`;
};

async function impDirPane() {
  const meta = IMP.meta;
  const listing = await api("/import/dirs?path=" + encodeURIComponent(IMP.rel));
  const dirRows = (listing.dirs || []).map(d => ({
    目录: esc(d.name),
    可导入文件: d.importable_files,
    操作: `<button class="mini" data-act="impGo" data-rel="${esc(d.rel_path)}">进入</button>`,
  }));
  const up = listing.parent === null
    ? '<span class="muted">已在 raw 根</span>'
    : `<button class="mini ghost" data-act="impGo" data-rel="${esc(listing.parent)}">↑ 上一层</button>`;
  const queue = await api("/import/queue");
  const q = queue.counts || {};
  const queueCard = `<div class="card"><b>解析队列</b>
    <div class="cards" style="margin:8px 0">
      ${kpi("待解析", q.pending || 0)}${kpi("解析中", q.parsing || 0)}
      ${kpi("已发布", q.published || 0)}${kpi("失败", q.failed || 0)}
    </div>
    <div class="muted">导入只是「登记待解析」，真正解析入库由 ekbs-worker 完成：
      待解析数与「解析任务」页一致。若数字长时间不动，先确认 worker 服务在跑。</div>
    ${table((queue.recent||[]).slice(0,5).map(r => ({doc_id:r.doc_id, 标题:r.title, 状态:r.status, 文件:r.source_path})), {cols:["doc_id","标题","状态","文件"]})}
  </div>`;
  return `<div class="card"><b>选择目录</b>
      <div class="muted" style="margin:6px 0">raw 根：<code>${esc(meta.raw_root)}</code>（只允许这个目录之内）</div>
      <div style="margin:6px 0">${impCrumb(IMP.rel)} ${up}</div>
      ${dirRows.length ? table(dirRows, {cols:["目录","可导入文件","操作"]})
        : '<div class="muted">该目录下没有子目录；可直接对当前目录扫描预览。</div>'}
      <div class="muted" style="margin-top:6px">当前目录：<code>${esc(IMP.rel || "raw")}</code></div>
    </div>
    ${impTargetForm(meta)}
    ${impScanOut(IMP.scan)}
    ${queueCard}`;
}

async function impUploadPane() {
  const meta = IMP.meta;
  const kbs = (meta.knowledge_bases || []).map(k =>
    `<option value="${k.kb_id}" ${String(IMP.kbId)===String(k.kb_id)?"selected":""}>${esc(k.code)} · ${esc(k.name)}</option>`).join("");
  const lv = [1,2,3,4].map(n =>
    `<option value="${n}" ${IMP.level===n?"selected":""}>L${n}</option>`).join("");
  const exts = (meta.upload_extensions || []).map(e => "." + e).join(" ");
  const blocked = (meta.upload_blocked || []).map(e => "." + e);
  const accept = (meta.upload_extensions || []).map(e => "." + e).join(",");
  return `<div class="card"><b>① 选文件</b>
      <label class="drop" id="impDrop" for="impFiles">
        <b>点这里选择文件</b>，或把文件拖进来
        <div class="hint">可多选；单个文件 ≤ ${meta.max_file_size_mb} MB；大于 5 MB 会自动分片上传</div>
        <input id="impFiles" type="file" multiple accept="${esc(accept)}" class="sr">
      </label>
      <div class="filelist" id="impPicked"><span class="muted">尚未选择文件</span></div>
      <div class="toolbar">
        <button data-act="impUpload">开始上传</button>
        <button class="ghost" data-act="impClear">清空选择</button>
        <span class="muted">上传后由 worker 异步解析，进度见「解析任务」页。</span>
      </div>
    </div>
    <div class="card"><b>② 目标与密级</b>
      <div class="grid2" style="margin-top:8px">
        <div class="field"><label>知识库</label><select id="impKb">${kbs}</select></div>
        <div class="field"><label>密级</label><select id="impLevel">${lv}</select></div>
        <div class="field"><label>可见部门 ID（逗号分隔，可空）</label>
          <input id="impDept" type="text" value="${esc(IMP.dept)}" placeholder="102"></div>
      </div>
    </div>
    <div class="card" id="impUpOut"><span class="muted">尚未开始上传。</span></div>
    <div class="card"><b>可上传的格式（${(meta.upload_extensions||[]).length} 种）</b>
      <div style="margin-top:6px">${esc(exts)}</div>
      ${blocked.length ? `<div class="warn" style="margin-top:8px">配置里还允许 ${esc(blocked.join(" "))}，
        但解析器尚未实现，上传会被拒（422），不会静默收下变成乱码。</div>` : ""}
      <div class="muted" style="margin-top:8px">上传路径受配置白名单限制，比服务器目录导入窄。
        若要传代码等格式：把文件放进 raw 目录，改用
        <a href="#" data-act="impGo" data-rel="">服务器目录导入</a>
        （那边支持 ${(meta.dir_extensions||[]).length} 种）。</div>
    </div>`;
}

/* 选完文件立刻回显文件名与大小，并说明"还没有上传"，避免用户以为已经导入了 */
function impRefreshPicked() {
  const box = document.getElementById("impPicked");
  if (!box) return;
  const input = document.getElementById("impFiles");
  const files = Array.from((input && input.files) || []);
  if (!files.length) { box.innerHTML = '<span class="muted">尚未选择文件</span>'; return; }
  const total = files.reduce((s, f) => s + f.size, 0);
  box.innerHTML = files.map(f =>
    `<div>${esc(f.name)} <span class="muted">${(f.size/1024).toFixed(1)} KB</span></div>`).join("")
    + `<div class="muted">共 ${files.length} 个，${(total/1024/1024).toFixed(2)} MB（尚未上传，点「开始上传」）</div>`;
}

/* 把浏览器原生 drag & drop 接上：拖进虚线框即等于选中这些文件 */
function impWireDrop() {
  const drop = document.getElementById("impDrop");
  const input = document.getElementById("impFiles");
  if (!drop || !input) return;
  ["dragenter","dragover"].forEach(ev => drop.addEventListener(ev, (e) => {
    e.preventDefault(); drop.classList.add("over");
  }));
  ["dragleave","drop"].forEach(ev => drop.addEventListener(ev, () => drop.classList.remove("over")));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
      input.files = e.dataTransfer.files;   // 原生 FileList 直接赋值
      impRefreshPicked();
    }
  });
  input.addEventListener("change", impRefreshPicked);
}

/* 大文件分片上传；小文件也走同一条路径（单请求即可）。
   秒传：浏览器先算 SHA-256 交给 init，服务端按内容命中则直接返回 doc_id。 */
async function impUploadFiles() {
  const input = document.getElementById("impFiles");
  const out = document.getElementById("impUpOut");
  const files = Array.from(input.files || []);
  if (!files.length) { alert("请先选择文件"); return; }
  IMP.kbId = (document.getElementById("impKb")||{}).value || "";
  IMP.level = Number((document.getElementById("impLevel")||{}).value || 1);
  IMP.dept = (document.getElementById("impDept")||{}).value || "";
  const deptIds = IMP.dept.split(/[,，\s]+/).map(s => s.trim()).filter(Boolean);
  if (!IMP.kbId) { alert("请选择知识库"); return; }

  const lines = [];
  const show = () => { out.innerHTML = lines.map(l => `<div>${l}</div>`).join(""); };
  const setLine = (i, html) => { lines[i] = html; show(); };

  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    setLine(i, `${esc(f.name)} — 计算校验和…`);
    let checksum = null;
    try {
      if (window.crypto && window.crypto.subtle && f.size <= 200*1024*1024) {
        const buf = await f.arrayBuffer();
        const d = await crypto.subtle.digest("SHA-256", buf);
        checksum = Array.from(new Uint8Array(d)).map(b => b.toString(16).padStart(2,"0")).join("");
      }
    } catch (e) { checksum = null; }   // http（非 localhost）下 subtle 不可用：不阻断上传
    try {
      const init = await api("/docs/upload/init", {method:"POST", body: JSON.stringify({
        filename: f.name, size_bytes: f.size, checksum, kb_id: Number(IMP.kbId),
        level: IMP.level, visible_dept_ids: deptIds})});
      if (init.instant) {
        setLine(i, `<span class="ok">${esc(f.name)} — 秒传命中（doc_id=${init.doc_id}）</span>`);
        continue;
      }
      const size = init.chunk_size_hint || f.size;
      const total = Math.max(1, Math.ceil(f.size / size));
      for (let idx = 0; idx < total; idx++) {
        const fd = new FormData();
        fd.append("file", f.slice(idx*size, Math.min((idx+1)*size, f.size)), f.name);
        fd.append("index", String(idx));
        const res = await fetch(`${API}/docs/upload/${init.upload_id}/chunk`, {
          method:"POST", headers: Object.assign({}, token ? {"Authorization":"Bearer "+token} : {}), body: fd});
        if (!res.ok) {
          let detail = res.statusText;
          try { detail = (await res.json()).detail || detail; } catch(e) {}
          throw new Error(detail);
        }
        setLine(i, `${esc(f.name)} — 上传中 ${Math.round((idx+1)/total*100)}%`);
      }
      const done = await api(`/docs/upload/${init.upload_id}/complete`, {method:"POST"});
      setLine(i, `<span class="ok">${esc(f.name)} — 已登记，doc_id=${done.doc_id}（待 worker 解析）</span>`);
    } catch (e) {
      setLine(i, `<span class="bad">${esc(f.name)} — 失败：${esc(e.message)}</span>`);
    }
  }
}

/* 原生 hash 路由：侧栏是 <a href="#/view">，靠浏览器的 hashchange 驱动渲染。
   **这里曾经缺监听**：点击只改了高亮、内容不动 —— 用户点「导入」看不到任何东西，
   表现就是"没有入口"。这类 bug 用后端用例永远抓不到，所以另外有一个 Node DOM 冒烟测试
   （tests/test_admin_ui_nav.py）盯着「点菜单 → 内容真的换了」。 */
function syncFromHash() {
  const view = location.hash.replace(/^#\/?/, "").split("/")[0];
  if (VIEWS[view]) current = view;
  buildMenu();
}

async function goTo(hash) {
  if (location.hash === hash) return render();  // 同一个 hash 不会再触发事件
  location.hash = hash;                          // 由 hashchange 负责渲染
}

window.addEventListener("hashchange", () => { syncFromHash(); render(); });

/* ── 交互 ─────────────────────────────────────────────────────────────── */
async function render() {
  const main = document.getElementById("main");
  main.innerHTML = '<div class="card">加载中…</div>';
  const sub = location.hash.split("/")[2] || "";
  try {
    main.innerHTML = await VIEWS[current]();
    // 视图返回的是 HTML 字符串，事件只能在插入 DOM 之后再挂（拖拽/回显）
    if (current === "import" && sub !== "dir") { impWireDrop(); impRefreshPicked(); }
  } catch (e) {
    main.innerHTML = `<div class="card"><span class="bad">加载失败：${esc(e.message)}</span>
      ${e.status === 403 ? '<div class="muted">当前身份缺少该接口要求的权限点。</div>' : ""}</div>`;
  }
}

function buildMenu() {
  const perms = new Set(me.permissions || []);
  const box = document.getElementById("menu");
  box.innerHTML = MENUS.filter(m => perms.has(m.perm)).map(m =>
    `<a href="#/${m.id}" data-menu="${m.id}" class="${current===m.id?"on":""}">${m.name}</a>`).join("");
  if (!box.innerHTML) box.innerHTML = '<div class="muted" style="padding:0 14px">该身份无管理权限</div>';
}

async function login() {
  const userId = document.getElementById("userSel").value;
  const t = await api("/auth/dev-token", {method:"POST", body: JSON.stringify({user_id: Number(userId)})});
  token = t.token;
  me = await api("/auth/me");
  document.getElementById("who").innerHTML =
    `#${me.user_id} ${esc(me.nick || "")}<br><span class="muted">权限点 ${(me.permissions||[]).length} 个</span>`;
  const usable = MENUS.filter(m => new Set(me.permissions||[]).has(m.perm));
  // 保留刷新前的页面：在 #/import/upload 上刷新不该被弹回概览
  const parts = location.hash.replace(/^#\/?/, "").split("/");
  const wanted = parts[0];
  const sub = parts[1] ? "/" + parts[1] : "";
  current = usable.some(m => m.id === wanted) ? wanted : (usable.length ? usable[0].id : null);
  buildMenu();
  if (current) await goTo("#/" + current + (current === wanted ? sub : ""));
  else document.getElementById("main").innerHTML =
    '<div class="card">当前身份没有任何管理权限点，请换身份。</div>';
}

async function loadUsers() {
  const sel = document.getElementById("userSel");
  sel.innerHTML = [[1,"Carol · kb_admin · L1"],[2,"Alice · end_user · L2"],[3,"Bob · end_user · L3"]]
    .map(([id, n]) => `<option value="${id}">${id} · ${n}</option>`).join("");
}

document.addEventListener("click", async (ev) => {
  const t = ev.target;
  // 侧栏/快捷入口：交给原生 hash 路由（href 已经在改 hash，这里不再手工渲染）
  if (t.dataset && t.dataset.menu) return goTo("#/" + t.dataset.menu);
  if (t.dataset && t.dataset.tab) {
    const base = current === "perm" ? "#/perm/"
               : current === "audit" ? "#/audit/"
               : current === "import" ? "#/import/" : "#/";
    return goTo(base + t.dataset.tab);
  }
  const act = t.dataset && t.dataset.act;
  if (!act) return;
  try {
    /* ── 导入页 ── */
    if (act === "impGo") {
      IMP.rel = t.dataset.rel || "";
      IMP.scan = null;              // 换目录后旧预览必须作废，否则会导错目录
      location.hash = "#/import/dir";
      return render();
    }
    if (act === "impScan" || act === "impRun") {
      if (IMP.busy) return;
      IMP.level = Number((document.getElementById("impLevel")||{}).value || 1);
      IMP.dept = (document.getElementById("impDept")||{}).value || "";
      IMP.kbId = (document.getElementById("impKb")||{}).value || "";
      IMP.recursive = !!(document.getElementById("impRecursive")||{}).checked;
      const deptIds = IMP.dept.split(/[,，\s]+/).map(s => s.trim()).filter(Boolean);
      IMP.busy = true;
      const btn = document.querySelector('[data-act="' + act + '"]');
      if (btn) { btn.disabled = true; btn.textContent = act === "impScan" ? "扫描中…" : "提交中…"; }
      try {
        if (act === "impScan") {
          IMP.scan = await api("/import/scan", {method:"POST", body: JSON.stringify({
            path: IMP.rel, level: IMP.level, recursive: IMP.recursive})});
        } else {
          if (!IMP.kbId) throw new Error("请选择知识库");
          if (!confirm(`确认导入「${IMP.rel || "raw"}」到知识库 ${IMP.kbId}（L${IMP.level}）？`
            + `导入后由 worker 解析，进度在「解析任务」页查看。`)) return;
          const r = await api("/import/run", {method:"POST", body: JSON.stringify({
            path: IMP.rel, kb_id: Number(IMP.kbId), level: IMP.level,
            dept_ids: deptIds, recursive: IMP.recursive})});
          IMP.scan = null;
          alert(`已登记待解析 ${r.queued} 份；跳过（重复）${r.skipped.length} 份；失败 ${r.failed.length} 份。`
            + (r.failed.length ? " 失败示例：" + r.failed.slice(0,3).map(f => f.rel_path + "：" + f.error).join("；") : ""));
        }
      } finally {
        IMP.busy = false;
      }
      return render();
    }
    if (act === "impClear") {
      const input = document.getElementById("impFiles");
      if (input) input.value = "";
      impRefreshPicked();
      const out = document.getElementById("impUpOut");
      if (out) out.innerHTML = '<span class="muted">尚未开始上传。</span>';
      return;
    }
    if (act === "impUpload") {
      if (IMP.busy) return;
      IMP.busy = true;
      const btn = document.querySelector('[data-act="impUpload"]');
      if (btn) { btn.disabled = true; btn.textContent = "上传中…"; }
      try { await impUploadFiles(); }
      finally { IMP.busy = false; if (btn) { btn.disabled = false; btn.textContent = "开始上传"; } }
      return;
    }
    if (act === "publish" || act === "offline") {
      if (!confirm(`确认对文档 ${t.dataset.id} 执行「${act === "publish" ? "发布" : "下线"}」？`)) return;
      await api(`/docs/documents/${t.dataset.id}/${act === "publish" ? "publish" : "offline"}`, {method:"POST"});
      return render();
    }
    if (act === "tree") { location.hash = "#/chunks/" + t.dataset.id; current = "chunks"; buildMenu(); return render(); }
    if (act === "reloadChunks") {
      location.hash = "#/chunks/" + document.getElementById("chunkDoc").value; return render();
    }
    if (act === "retry" || act === "cancel") {
      if (!confirm(`确认${act === "retry" ? "重跑" : "取消"}任务 ${t.dataset.id}？`)) return;
      await api(`/parse/tasks/${t.dataset.id}/${act}`, {method:"POST"});
      return render();
    }
    if (act === "reject") {
      if (!confirm(`确认驳回复核任务 ${t.dataset.id}？将标记为失败，可再重跑。`)) return;
      await api(`/parse/tasks/${t.dataset.id}/reject`, {method:"POST"});
      return render();
    }
    if (act === "viewResult" || act === "review") {
      /* 全文预览 / 复核抽屉已废除：一律进 SPA */
      const id = t.dataset.id;
      location.href = act === "review"
        ? `/admin/parse/review/${id}`
        : `/admin/chunks/${id}`;
      return;
    }
    if (act === "clearance") {
      const next = prompt(`用户 ${t.dataset.id} 当前密级 L${t.dataset.cur}，改为（1-4）：`, t.dataset.cur);
      if (!next) return;
      await api(`/perm/users/${t.dataset.id}/clearance`, {method:"PUT",
        body: JSON.stringify({clearance: Number(next), reason: "管理台手改"})});
      return render();
    }
    if (act === "simulate") {
      const userId = Number(document.getElementById("simUser").value);
      const docIds = document.getElementById("simDocs").value.split(",").map(s => Number(s.trim())).filter(Boolean);
      const d = await api("/perm/acl/simulate", {method:"POST",
        body: JSON.stringify({user_id: userId, doc_ids: docIds, reason: "管理台模拟"})});
      document.getElementById("simOut").innerHTML = `<pre>${esc(JSON.stringify(d, null, 2))}</pre>`;
      return;
    }
    if (act === "debug") {
      const q = document.getElementById("dbgQ").value.trim();
      if (!q) return;
      const out = document.getElementById("dbgOut");
      out.innerHTML = '<div class="muted">检索中…</div>';
      const d = await api("/rag/debug/query", {method:"POST", body: JSON.stringify({
        query: q, rerank_enabled: document.getElementById("dbgRerank").checked})});
      const rows = (d.chunks || d.candidates || []).map((c, i) => Object.assign({序号:i+1}, c));
      out.innerHTML = `<div class="row" style="margin:8px 0">
          <span class="tag">拒答 ${d.refused ? "是" : "否"}</span>
          <span class="tag">原因 ${esc(d.refusal_reason || "-")}</span>
          <span class="tag">候选 ${d.candidate_count ?? rows.length}</span>
          <span class="tag">top ${d.top_score != null ? Number(d.top_score).toFixed(3) : "-"}</span>
        </div>${table(rows)}<pre>${esc(JSON.stringify(d.timings_ms || {}, null, 1))}</pre>`;
      return;
    }
  } catch (e) {
    const msg = String(e.message || e).slice(0, 240);
    alert("操作失败：" + msg + (e.status ? `（HTTP ${e.status}）` : ""));
  }
});

document.getElementById("relogin").onclick = login;
loadUsers().then(login);
</script>
</body>
</html>
"""


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_console() -> HTMLResponse:
    """常规管理台（免构建单页；定位见模块 docstring）。"""
    return HTMLResponse(PAGE)
