"""临时测试台（单个 HTML 页面，无构建步骤）。

**这不是 `05-管理台UI设计方案.md` 里规划的管理台** —— 那个是 React 18 + TS + Vite +
Ant Design Pro 5，本仓库**一行都没实现**（`find` 无任何前端文件）。这个页面只是为了
「起个界面点一点、验证 RAG 链路与权限红线」而临时提供的最小可用界面：

* 用户切换（走 `POST /auth/dev-token`，**仅 dev 环境**签发令牌；生产该端点 404）；
* 权限视图（`GET /perm/me`）：密级、可见分片、权限指纹 —— 用来肉眼确认 C1 与红线；
* 问答（`POST /qa/chat`）：答案 + 引用 + 拒答原因 + 分阶段耗时；
* 检索诊断（`POST /qa/retrieve`）：逐条候选的 dense/sparse 名次与 rerank 分数。

放在 `/ui`（不带前缀，避免与 `/api/v1`、`/v1` 两套前缀纠缠）。
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse, Response

router = APIRouter()

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EKBS 测试台（临时）</title>
<style>
  :root { --bg:#0f1115; --card:#171a21; --line:#262b36; --fg:#e6e9ef; --dim:#8b93a7;
          --ok:#3fb950; --bad:#f85149; --warn:#d29922; --accent:#4493f8; }
  * { box-sizing:border-box; }
  body { margin:0; padding:20px; background:var(--bg); color:var(--fg);
         font:14px/1.6 -apple-system,"PingFang SC",Helvetica,sans-serif; }
  h1 { font-size:17px; margin:0 0 4px; }
  .sub { color:var(--dim); font-size:12px; margin-bottom:16px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:14px 16px; margin-bottom:14px; }
  .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  label { color:var(--dim); font-size:12px; }
  select,input,button,textarea { background:#0d1017; color:var(--fg);
          border:1px solid var(--line); border-radius:7px; padding:7px 10px; font-size:13px; }
  input[type=text] { flex:1; min-width:240px; }
  button { cursor:pointer; background:var(--accent); border-color:var(--accent);
           color:#06101f; font-weight:600; }
  button.ghost { background:transparent; color:var(--fg); border-color:var(--line); font-weight:400; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  pre { white-space:pre-wrap; word-break:break-word; margin:8px 0 0; }
  .tag { display:inline-block; padding:1px 7px; border-radius:20px; font-size:11px;
         border:1px solid var(--line); color:var(--dim); margin-right:6px; }
  .ok { color:var(--ok); } .bad { color:var(--bad); } .warn { color:var(--warn); }
  .cite { border-left:3px solid var(--accent); padding:6px 10px; margin:6px 0;
          background:#12151c; border-radius:0 6px 6px 0; font-size:13px; }
  .kv { display:grid; grid-template-columns:auto 1fr; gap:2px 14px; font-size:12px; }
  .kv div:nth-child(odd) { color:var(--dim); }
  table { width:100%; border-collapse:collapse; font-size:12px; margin-top:8px; }
  th,td { text-align:left; padding:5px 8px; border-bottom:1px solid var(--line); }
  th { color:var(--dim); font-weight:500; }
  .hidden { display:none; }
</style>
</head>
<body>
<h1>EKBS 测试台 <span class="tag">临时页面 · 非 05 规划的管理台</span></h1>
<div class="sub">这里只做四件事：切用户、看权限、问问题、看检索候选。管理台（React）尚未实现。</div>

<div class="card">
  <div class="row">
    <label>以谁的身份</label>
    <select id="user"></select>
    <button id="login">签发令牌（dev）</button>
    <span id="loginState" class="warn">未登录</span>
  </div>
</div>

<div class="card" id="permCard">
  <div class="row"><b>权限视图</b><button class="ghost" id="loadPerm">刷新 /perm/me</button></div>
  <div id="perm" class="kv" style="margin-top:8px"></div>
</div>

<div class="card">
  <div class="row">
    <input type="text" id="q" placeholder="问点什么，例如：年假有多少天 / 公司财务报表在哪里看">
    <button id="ask">提问</button>
    <button class="ghost" id="debugBtn">只看检索</button>
  </div>
  <div class="row" style="margin-top:8px">
    <label><input type="checkbox" id="useCache"> 允许答案缓存</label>
    <span class="sub" style="margin:0">（默认关闭，便于反复测试同一问题）</span>
  </div>
  <div id="answerWrap" class="hidden">
    <div id="meta" style="margin-top:12px"></div>
    <pre id="answer"></pre>
    <div id="cites"></div>
    <div id="timings" class="sub" style="margin-top:8px"></div>
  </div>
</div>

<div class="card hidden" id="debugCard">
  <div class="row"><b>检索候选</b><span class="sub" style="margin:0" id="debugMeta"></span></div>
  <table id="debugTable"><thead><tr>
    <th>#</th><th>文档</th><th>标题路径</th><th>类型</th>
    <th>dense</th><th>sparse</th><th>RRF</th><th>rerank</th>
  </tr></thead><tbody></tbody></table>
</div>

<script>
const PREFIX = "/api/v1";
let token = null;

async function api(path, options = {}) {
  const headers = Object.assign({"Content-Type": "application/json"}, options.headers || {});
  if (token) headers["Authorization"] = "Bearer " + token;
  const res = await fetch(PREFIX + path, Object.assign({}, options, {headers}));
  const text = await res.text();
  let body; try { body = JSON.parse(text); } catch (e) { body = {detail: text}; }
  if (!res.ok) throw Object.assign(new Error(body.detail || res.statusText), {status: res.status, body});
  return body;
}

async function loadUsers() {
  const sel = document.getElementById("user");
  try {
    const data = await api("/perm/users");
    const items = data.items || data.users || data;
    sel.innerHTML = items.map(u =>
      `<option value="${u.user_id}">${u.user_id} · ${u.nick || u.dingtalk_userid || ""}</option>`).join("");
    if (!items.length) throw new Error("empty");
  } catch (e) {
    // /perm/users 需要管理员令牌；未登录时退化为内置演示用户
    sel.innerHTML = [[1,"Carol · L1"],[2,"Alice · L2"],[3,"Bob · L3"]]
      .map(([id,nick]) => `<option value="${id}">${id} · ${nick}</option>`).join("");
  }
}

async function signIn() {
  const userId = document.getElementById("user").value;
  const state = document.getElementById("loginState");
  try {
    const data = await api("/auth/dev-token", {method:"POST", body: JSON.stringify({user_id: Number(userId)})});
    token = data.token;
    state.textContent = "已登录 user_id=" + userId;
    state.className = "ok";
    document.getElementById("perm").innerHTML = "";
    await loadPerm();
    return true;
  } catch (e) {
    state.textContent = "签发失败：" + e.message + (e.status === 404 ? "（非 dev 环境无此端点）" : "");
    state.className = "bad";
    return false;
  }
}
document.getElementById("login").onclick = signIn;
document.getElementById("user").onchange = () => { token = null; signIn(); };

// 打开页面就自动以默认身份登录：否则用户直接点「提问」会拿到 401（实测踩到），
// 那条 401 看起来像服务坏了，其实只是没令牌。
window.addEventListener("load", async () => { if (document.getElementById("user").value) await signIn(); });

async function loadPerm() {
  const box = document.getElementById("perm");
  if (!token) { box.innerHTML = "<div>—</div><div>先签发令牌</div>"; return; }
  try {
    const d = await api("/perm/me");
    // 字段名以 /perm/me 的实际返回为准（visible_shards / visible_kb_ids / policy_version）
    const rows = [
      ["user_id", d.user_id],
      ["clearance", d.clearance],
      ["可见分片（C1 路由范围）", (d.visible_shards || []).join(", ") || "（空 → 拒答）"],
      ["可见 KB", (d.visible_kb_ids || []).join(", ") || "（空）"],
      ["策略版本", d.policy_version],
      ["权限指纹", (d.fingerprint || "").slice(0, 24) + "…"],
      ["警告", (d.warnings || []).join("；") || "无"],
    ];
    box.innerHTML = rows.map(([k,v]) => `<div>${k}</div><div>${v}</div>`).join("");
  } catch (e) { box.innerHTML = `<div>错误</div><div class="bad">${e.message}</div>`; }
}
document.getElementById("loadPerm").onclick = loadPerm;

function renderAnswer(d) {
  document.getElementById("answerWrap").classList.remove("hidden");
  const refused = d.refused;
  document.getElementById("meta").innerHTML =
    `<span class="tag ${refused ? "bad" : "ok"}">${refused ? "拒答" : "已回答"}</span>` +
    (d.refusal_reason ? `<span class="tag">${d.refusal_reason}</span>` : "") +
    (d.model ? `<span class="tag">${d.model}</span>` : "") +
    (d.rewritten_query && d.rewritten_query !== d.question ?
      `<span class="tag">改写：${d.rewritten_query}</span>` : "");
  document.getElementById("answer").textContent = d.answer || "（空）";
  const cites = d.citations || [];
  document.getElementById("cites").innerHTML = cites.map((c, i) =>
    `<div class="cite">[${i+1}] <b>${c.doc_title}</b>
       ${c.heading_path ? "· " + c.heading_path : ""}
       ${c.page_from ? "· p" + c.page_from : ""}
       <span class="tag">rerank ${c.rerank_score != null ? c.rerank_score.toFixed(3) : "-"}</span>
       <span class="tag">chunk ${c.chunk_id}</span></div>`).join("");
  const t = d.timings_ms || {};
  document.getElementById("timings").textContent = Object.entries(t)
    .map(([k,v]) => `${k}=${typeof v === "number" ? v.toFixed(1) : v}ms`).join("  ");
}

document.getElementById("ask").onclick = async () => {
  const q = document.getElementById("q").value.trim();
  if (!q) return;
  const btn = document.getElementById("ask"); btn.disabled = true; btn.textContent = "生成中…";
  try {
    const d = await api("/qa/chat", {method:"POST", body: JSON.stringify({
      question: q, use_cache: document.getElementById("useCache").checked,
    })});
    renderAnswer(d);
  } catch (e) {
    document.getElementById("answerWrap").classList.remove("hidden");
    const hint = e.status === 401 ? "（未登录或令牌失效 → 点「签发令牌（dev）」）"
               : e.status === 403 ? "（当前身份没有该权限点）"
               : e.status === 429 ? "（触发限流：单用户 2 QPS / 全局 20 QPS）" : "";
    document.getElementById("answer").textContent = "请求失败：" + e.message + hint;
    document.getElementById("meta").innerHTML = `<span class="tag bad">HTTP ${e.status||"?"}</span>`;
  } finally { btn.disabled = false; btn.textContent = "提问"; }
};

document.getElementById("debugBtn").onclick = async () => {
  const q = document.getElementById("q").value.trim();
  if (!q) return;
  const tbody = document.querySelector("#debugTable tbody");
  try {
    const d = await api("/qa/retrieve", {method:"POST", body: JSON.stringify({query: q})});
    document.getElementById("debugCard").classList.remove("hidden");
    document.getElementById("debugMeta").textContent =
      `候选 ${d.candidate_count} 条 · C5 丢弃 ${d.c5_dropped} · top ${d.top_score != null ? d.top_score.toFixed(3) : "-"}` +
      (d.refused ? ` · 拒答（${d.refusal_reason}）` : "");
    tbody.innerHTML = (d.chunks || []).map((c, i) => `<tr>
      <td>${i+1}</td><td>${c.doc_title}</td><td>${c.heading_path || ""}</td><td>${c.chunk_type}</td>
      <td>${c.dense_rank ?? "-"}</td><td>${c.sparse_rank ?? "-"}</td>
      <td>${c.rrf_score != null ? c.rrf_score.toFixed(4) : "-"}</td>
      <td>${c.rerank_score != null ? c.rerank_score.toFixed(3) : "-"}</td></tr>`).join("");
  } catch (e) {
    document.getElementById("debugCard").classList.remove("hidden");
    document.getElementById("debugMeta").textContent = "失败：" + e.message;
    tbody.innerHTML = "";
  }
};

loadUsers();
</script>
</body>
</html>
"""


@router.get("/ui", response_class=HTMLResponse, include_in_schema=False)
async def test_console() -> HTMLResponse:
    """临时测试台（见模块 docstring：这不是 05 规划的管理台）。"""
    return HTMLResponse(PAGE)


LANDING = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EKBS 服务入口</title>
<style>
 body { background:#0f1115; color:#e6e9ef; font:14px/1.8 -apple-system,"PingFang SC",sans-serif;
        padding:40px; max-width:720px; margin:0 auto; }
 h1 { font-size:18px; } a { color:#4493f8; text-decoration:none; } a:hover { text-decoration:underline; }
 li { margin:4px 0; } code { background:#171a21; padding:1px 6px; border-radius:5px; color:#8b93a7; }
 .note { color:#8b93a7; font-size:12px; margin-top:18px; }
</style></head><body>
<h1>EKBS 企业知识中枢（部门级 RAG 试点）</h1>
<ul>
  <li><a href="/ui">测试台</a> <code>/ui</code> —— 切用户、看权限、提问、看检索候选（临时页面）</li>
  <li><a href="/docs">接口文档</a> <code>/docs</code> —— 中文 Swagger，可直接「试一下」</li>
  <li><a href="/openapi.json">OpenAPI 3.1 契约</a> <code>/openapi.json</code></li>
  <li><a href="/livez">存活</a> <code>/livez</code> ·
      <a href="/healthz">健康</a> <code>/healthz</code> ·
      <a href="/readyz">就绪</a> <code>/readyz</code> ·
      <a href="/health/deps">依赖</a> <code>/health/deps</code></li>
</ul>
<div class="note">管理台（<code>05</code> 规划的 React 应用）尚未实现；上面的「测试台」是临时替代页面。</div>
</body></html>
"""


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def landing() -> HTMLResponse:
    """根路径落地面。

    原先根路径没有路由 → 直接 404，只拿到一个 `{"detail":"Not Found"}`，
    对「访问服务」的人来说等于没有入口（实测踩到）。
    """
    return HTMLResponse(LANDING)


@router.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    """浏览器必定会请求它；不处理就会在开发者工具的 Network 里留下一条 404，
    排查真问题时容易误判成「服务有问题」。"""
    return Response(status_code=204)


@router.get("/redoc", include_in_schema=False)
async def redoc_redirect() -> RedirectResponse:
    """ReDoc 没有中文 i18n，故不在本服务提供；这里重定向到中文文档页而不是 404
    （`/redoc` 曾在交付说明里被提到过，直接 404 会让人以为服务坏了）。"""
    return RedirectResponse("/docs", status_code=307)
