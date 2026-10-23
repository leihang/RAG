"""把 FastAPI 自带的接口文档**中文化**（`/docs` 与 `/openapi.json` 的元数据）。

背景与边界（先看清楚再改）：

* `/docs` 页面本身是 **FastAPI 框架自带**的静态资源，本仓库没有前端代码；
  我们**能改的只有两处**：① `/openapi.json` 里的元数据（标题/分组/摘要/说明），
  ② 承载页面的 HTML（在这里注入一段把界面文案换成中文的脚本）。
* Swagger UI **没有官方 i18n**，所以界面文案靠 DOM 文本替换实现。这招对版本敏感，
  但它是**开发/联调用的文档页**，替换失败只会退回英文，不影响任何接口行为。
* `summary` 默认是**函数名机械转换**的英文（如 ``Qa Chat``）。这里改为取函数
  docstring 的第一行（本仓库的 docstring 都是中文），缺失时用下面的覆盖表兜底。

另外顺手补了 ``securitySchemes``：原先为空 → Swagger 的「Authorize」按钮**根本不出现**，
点任何需要鉴权的接口都 401。现在声明了 Bearer（`/auth/dev-token` 可取，仅 dev）与
内部令牌两种方式，页面可以真正交互联调。
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse

#: 分组标识（代码里的英文 tag）→ 中文标题。未列入的按原样显示。
ZH_TAGS: dict[str, str] = {
    "qa": "问答",
    "playground": "检索调试台",
    "docs": "文档管理",
    "kb": "知识库管理",
    "assets": "文件与资产",
    "chunks": "分块编辑",
    "parse": "解析任务",
    "import": "导入",
    "discovery": "文档发现",
    "personal": "个人私有库",
    "perm": "权限管理",
    "admin": "管理台",
    "auth": "认证与权限",
    "health": "健康检查",
    "__default__": "其它",
}

#: 分组的展示顺序（Swagger 按 schema 里的 tags 顺序渲染）
TAG_ORDER = [
    "health", "auth", "qa", "playground", "docs", "assets",
    "chunks", "parse", "import", "discovery", "personal", "kb", "perm", "admin", "__default__",
]

TAG_DESCRIPTIONS: dict[str, str] = {
    "health": "存活/就绪/依赖探测；`/readyz` 不谎报就绪（未连上时为 503）",
    "auth": "登录、令牌签发与当前用户权限视图；生产环境不提供 dev-token",
    "qa": "检索与问答主链路：混合召回 → RRF → 云端精排 → C5 门禁 → 父块扩展 → 引用校验",
    "playground": "检索调试台：逐分片召回明细、参数覆盖（并发安全，不改全局配置）",
    "docs": "文档上传、发布、下线、删除与索引重建",
    "kb": "知识库的建、改、删与文档数视图（code 进分片名，不可改）",
    "assets": "原文件与解析产物的读写",
    "chunks": "分块查看与编辑（改子块会追加向量并重写 FTS）",
    "parse": "解析任务中心：状态、重跑、取消、成功率对照",
    "import": "导入：服务器目录导入（扫描 → 登记待解析，解析交给 worker）与上传准入说明",
    "discovery": "按当前用户权限列出可见文档、示例问，以及点名发送原文",
    "personal": "钉钉单聊建立的个人私有库：上传、列出、按标题删除，仅本人可见",
    "perm": "角色、部门可见性、密级与权限变更（变更会触发分片迁移与缓存失效）",
    "admin": "管理台只读接口：会话、安全事件、访问日志、概览、告警阈值",
    "__default__": "未分组接口",
}

#: 没有 docstring 的接口用这张表兜底（否则摘要会退回函数名）
ZH_SUMMARY_OVERRIDES: dict[tuple[str, str], str] = {
    ("/livez", "get"): "存活探针（进程是否活着，不查依赖）",
    ("/healthz", "get"): "健康检查（依赖不可达时降级）",
    ("/readyz", "get"): "就绪探针（未就绪返回 503，不谎报）",
    ("/health/deps", "get"): "依赖探测（会出网，结果缓存 60 秒）",
    ("/openapi.json", "get"): "OpenAPI 3.1 契约（供 schemathesis 等工具消费）",
}

#: 按**路径后缀**兜底的中文摘要（覆盖 `/api/v1` 与 `/v1` 两套前缀）。
#: 用于极少数没有 docstring 的接口 —— 不去改接口代码，元数据集中在这个模块里。
ZH_SUMMARY_SUFFIX: dict[tuple[str, str], str] = {
    ("/qa/chat", "post"): "问答（检索 → 精排 → 生成；拒答时 refused=true 且 citations 为空）",
    ("/assets/figure/{chunk_id}", "get"): "取分块对应的图片（引用卡片里的图预览）",
}

INFO_TITLE = "EKBS 企业知识中枢 API"
INFO_DESCRIPTION = """部门级 RAG 试点的一期接口。

**权限口径**：所有检索与问答接口都按调用者的权限指纹做分片路由（C1），
无权分片**不参与计算**；引用在交给大模型前还要过一遍文档级门禁（C5）。

**鉴权**：点右上角「授权」填入 `/auth/dev-token` 取到的 Bearer 令牌即可联调
（该端点仅 dev 环境存在；生产走钉钉免登换取 JWT）。

**契约**：`/openapi.json` 是 OpenAPI 3.1，可被 schemathesis 等工具直接消费。
"""

#: 界面文案替换表。键是 Swagger UI 里的英文原文，值是中文。
#: 只覆盖高频可见项；未覆盖的会保持英文（不影响功能）。
UI_TEXT_MAP: dict[str, str] = {
    "Authorize": "授权",
    "Try it out": "试一下",
    "Cancel": "取消",
    "Execute": "执行",
    "Clear": "清空",
    "Parameters": "参数",
    "No parameters": "无参数",
    "Request body": "请求体",
    "Responses": "响应",
    "Response body": "响应体",
    "Code": "状态码",
    "Details": "详情",
    "Description": "说明",
    "Name": "名称",
    "Type": "类型",
    "Schemas": "数据模型",
    "Schema": "模型",
    "Example Value": "示例值",
    "Media type": "媒体类型",
    "Controls Accept header.": "控制 Accept 头。",
    "Server response": "服务端响应",
    "Request URL": "请求地址",
    "Request headers": "请求头",
    "Response headers": "响应头",
    "Successful Response": "成功响应",
    "Validation Error": "参数校验失败",
    "Download": "下载",
    "Links": "关联",
    "No links": "无关联",
    "Expand operation": "展开",
    "Collapse operation": "收起",
    "Loading...": "加载中…",
    "Servers": "服务地址",
    "OAS 3.1": "OpenAPI 3.1",
    "Available authorizations": "可用授权方式",
    "Close": "关闭",
    "Logout": "登出",
    "Value": "值",
    "Required": "必填",
}

_TRANSLATE_SCRIPT = """
<script>
(function () {
  // Swagger UI 没有官方 i18n，这里做 DOM 文本替换。匹配失败只会保留英文，不影响功能。
  const MAP = %(map)s;
  const SKIP = new Set(["SCRIPT", "STYLE", "CODE", "PRE", "TEXTAREA", "INPUT"]);
  function translate(root) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const jobs = [];
    let node;
    while ((node = walker.nextNode())) {
      const raw = node.nodeValue;
      const key = raw && raw.trim();
      if (!key || !MAP[key]) continue;
      if (node.parentElement && SKIP.has(node.parentElement.tagName)) continue;
      jobs.push([node, raw.replace(key, MAP[key])]);
    }
    jobs.forEach(([n, v]) => { n.nodeValue = v; });
  }
  const start = () => {
    translate(document.body);
    new MutationObserver((records) => {
      records.forEach((r) => r.addedNodes.forEach((n) => {
        if (n.nodeType === 1) translate(n); else if (n.nodeType === 3 && MAP[(n.nodeValue || "").trim()])
          n.nodeValue = n.nodeValue.replace(n.nodeValue.trim(), MAP[n.nodeValue.trim()]);
      }));
    }).observe(document.body, { childList: true, subtree: true });
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
</script>
"""


def _summary_from_docstring(app: FastAPI) -> dict[tuple[str, str], str]:
    """``(路径, 方法) -> docstring 第一行``。函数名转出来的英文摘要不如这个可读。"""
    from fastapi.routing import APIRoute

    found: dict[tuple[str, str], str] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        doc = (route.endpoint.__doc__ or "").strip()
        if not doc:
            continue
        first = doc.splitlines()[0].strip()
        for method in route.methods or []:
            found[(route.path, method.lower())] = first
    return found


def build_openapi(app: FastAPI) -> Callable[[], dict[str, Any]]:
    """返回一个把元数据中文化的 openapi 生成函数。"""
    summaries = _summary_from_docstring(app)

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=INFO_TITLE,
            version=app.version,
            description=INFO_DESCRIPTION,
            routes=app.routes,
        )
        schema["info"]["title"] = INFO_TITLE
        schema["info"]["description"] = INFO_DESCRIPTION

        for path, operations in schema.get("paths", {}).items():
            for method, operation in operations.items():
                if method not in ("get", "post", "put", "delete", "patch"):
                    continue
                # 分组：英文标识符 → 中文标题
                operation["tags"] = [
                    ZH_TAGS.get(tag, tag) for tag in (operation.get("tags") or ["__default__"])
                ]
                # 摘要：docstring 第一行 > 覆盖表 > 原值（函数名）
                key = (path, method)
                suffix_hit = next(
                    (text for (suffix, verb), text in ZH_SUMMARY_SUFFIX.items()
                     if verb == method and path.endswith(suffix)),
                    None,
                )
                operation["summary"] = (
                    summaries.get(key)
                    or ZH_SUMMARY_OVERRIDES.get(key)
                    or suffix_hit
                    or operation.get("summary")
                )

        # 分组顺序与说明（Swagger 按这里的顺序渲染）
        schema["tags"] = [
            {"name": ZH_TAGS[key], "description": TAG_DESCRIPTIONS[key]}
            for key in TAG_ORDER
        ]

        # 原来 securitySchemes 是空的 → 文档页「授权」按钮不出现、接口点不动。
        components = schema.setdefault("components", {})
        components["securitySchemes"] = {
            "BearerAuth": {
                "type": "http", "scheme": "bearer", "bearerFormat": "JWT",
                "description": "在 /auth/dev-token 取令牌（仅 dev 环境）；生产走钉钉免登",
            },
            "InternalToken": {
                "type": "apiKey", "in": "header", "name": "X-Internal-Token",
                "description": "服务间回环调用；需同时给 X-Internal-User-Id",
            },
            "InternalUserId": {
                "type": "apiKey", "in": "header", "name": "X-Internal-User-Id",
                "description": "内部调用时声明的用户（服务端会查库确认）",
            },
        }
        # 全局声明 Bearer：文档页填一次即可对所有接口生效（仅文档元数据，不改变实际鉴权）
        schema["security"] = [{"BearerAuth": []}]

        app.openapi_schema = schema
        return schema

    return custom_openapi


def render_docs() -> HTMLResponse:
    """中文标题 + 中文化脚本 + 便于联调的默认参数。"""
    import json as _json

    html = get_swagger_ui_html(
        openapi_url="/openapi.json",
        title=f"{INFO_TITLE} · 接口文档",
        swagger_ui_parameters={
            "docExpansion": "list",          # 默认展开接口列表，不展开每个接口的详情
            "defaultModelsExpandDepth": 0,   # 默认收起底部庞大的数据模型区
            "tryItOutEnabled": True,         # 打开即可直接填参数
            "displayRequestDuration": True,  # 显示耗时，便于对照压测数据
            "persistAuthorization": True,    # 刷新页面不用重新填令牌
            "filter": True,                  # 顶部搜索框
            "operationsSorter": "method",
        },
    ).body.decode("utf-8")
    script = _TRANSLATE_SCRIPT % {"map": _json.dumps(UI_TEXT_MAP, ensure_ascii=False)}
    html = html.replace("</body>", script + "</body>")
    return HTMLResponse(html)


def install_docs(app: FastAPI) -> None:
    """挂上中文文档页并把 openapi 生成函数换成中文化版本。

    **必须在 include_router 之后调用**（要遍历 app.routes 取 docstring）。
    """
    app.openapi = build_openapi(app)  # type: ignore[method-assign]

    @app.get("/docs", include_in_schema=False)
    async def chinese_docs() -> HTMLResponse:  # pragma: no cover - 纯渲染
        return render_docs()
