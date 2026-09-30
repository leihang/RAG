"""管理台**导航行为**的冒烟测试：点侧栏菜单，内容必须真的换。

为什么单独一个文件
------------------
`/admin` 的侧栏是原生 ``<a href="#/view">``，靠浏览器的 ``hashchange`` 驱动渲染。
这套代码曾经**没有 hashchange 监听**：点击菜单只改了高亮、主区域一动不动 ——
从用户角度看就是「点了导入什么都没出现，等于没有入口」。

这类 bug 有个恶劣性质：**后端接口全绿、JS 语法也合法**（`node --check` 过），
只有"点一下"才知道坏了。所以这里用 Node + 最小 DOM 桩把页面真跑起来，
模拟点击并断言内容变化。

没有 node 时整个文件跳过 —— 不把测试绑死在 node 上。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap

import pytest

from ekbs.api.admin_ui import PAGE

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="本机没有 node，跳过导航行为冒烟"
)

#: 页面里 MENUS 用到的全部权限点，登录桩直接给全
_ALL_PERMS = [
    "sys:dashboard:read", "doc:document:read", "doc:document:create",
    "perm:user:read", "doc:chunk:read", "parse:task:read",
    "rag:playground:run", "sys:health:read", "perm:audit:read",
]

_HARNESS = textwrap.dedent(
    """
    // 最小 DOM 桩：只实现管理台真正用到的那几样，别的都不假装支持。
    const fs = require("fs"), vm = require("vm");
    const js = fs.readFileSync(process.argv[2], "utf8");

    const els = {};
    function mkEl(id) {
      if (!els[id]) {
        els[id] = {
          id, innerHTML: "", textContent: "", value: "1", checked: false,
          disabled: false, dataset: {}, style: {}, files: [],
          classList: { add(){}, remove(){}, contains(){ return false; } },
          addEventListener(){}, querySelector(){ return null; },
        };
      }
      return els[id];
    }
    const docHandlers = {}, winHandlers = {};
    const add = (bag) => (ev, fn) => { (bag[ev] = bag[ev] || []).push(fn); };

    const RESPONSES = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
    const calls = [];

    const sandbox = {
      console,
      setTimeout, clearTimeout, Promise, JSON, Math, Object, Array, String, Number, Error,
      location: { hash: "#/import/upload" },   // 直接落在导入页：同时验证"刷新不回首页"
      document: {
        getElementById: mkEl,
        querySelector: () => null,
        addEventListener: add(docHandlers),
        createElement: () => mkEl("__tmp"),
      },
      fetch: async (url) => {
        const key = String(url).replace(/^\\/api\\/v1/, "");
        calls.push(key);
        const body = RESPONSES[key.split("?")[0]] || {};
        return { ok: true, status: 200, text: async () => JSON.stringify(body),
                 json: async () => body };
      },
      confirm: () => true,
      alert: () => {},
      FormData: class { append() {} },
      addEventListener: add(winHandlers),
    };
    sandbox.window = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(js, sandbox);

    const tick = (ms) => new Promise((r) => setTimeout(r, ms));
    const fireHash = () => (winHandlers["hashchange"] || []).forEach((fn) => fn());

    function fail(msg) { console.error("FAIL: " + msg); process.exit(1); }

    (async () => {
      await tick(120);   // 等 loadUsers().then(login) 跑完

      const main = mkEl("main").innerHTML;
      if (!main.includes("导入")) fail("初次加载没有落在导入页：" + main.slice(0, 200));
      if (!main.includes("点这里选择文件")) fail("导入页缺少「选择文件」入口：" + main.slice(0, 300));
      if (!mkEl("menu").innerHTML.includes("导入")) fail("侧栏没有「导入」菜单项");

      // ① 点侧栏「文档管理」：这就是原来坏掉的那条路径
      sandbox.location.hash = "#/docs";
      fireHash();
      await tick(120);
      const afterDocs = mkEl("main").innerHTML;
      if (!afterDocs.includes("文档管理")) fail("点菜单后内容没变（导航失效）：" + afterDocs.slice(0, 200));
      if (afterDocs.includes("点这里选择文件")) fail("点菜单后仍是导入页，主区域没重画");

      // ② 再点回「导入」，且要求落在上传页签
      sandbox.location.hash = "#/import";
      fireHash();
      await tick(120);
      if (!mkEl("main").innerHTML.includes("点这里选择文件")) fail("回到导入页失败");

      // ③ 点页签切到「服务器目录导入」（页签也走 hashchange）
      sandbox.location.hash = "#/import/dir";
      fireHash();
      await tick(120);
      const dirHtml = mkEl("main").innerHTML;
      if (!dirHtml.includes("选择目录")) fail("页签切换到目录导入失败：" + dirHtml.slice(0, 200));

      if (!calls.some((u) => u.startsWith("/import/meta"))) fail("没调用 /import/meta");
      if (!calls.some((u) => u.startsWith("/import/dirs"))) fail("目录页签没调用 /import/dirs");
      console.log("OK");
    })();
    """
)


def _responses() -> dict[str, object]:
    return {
        "/auth/dev-token": {"token": "stub-token"},
        "/auth/me": {"user_id": 1, "nick": "桩用户", "permissions": _ALL_PERMS},
        "/docs/documents": {"items": [], "total": 0},
        "/import/meta": {
            "raw_root": "/tmp/ekbs/raw",
            "dir_extensions": [f"e{i}" for i in range(63)],
            "upload_extensions": ["pdf", "docx", "doc", "pptx", "xlsx", "xls",
                                  "txt", "md", "png", "jpg", "jpeg"],
            "upload_blocked": ["html", "ppt"],
            "max_file_size_mb": 100,
            "knowledge_bases": [{"kb_id": 1, "code": "demo", "name": "演示知识库"}],
            "queue": {},
        },
        "/import/dirs": {"rel_path": "", "parent": None, "dirs": [], "root": "/tmp/ekbs/raw"},
        "/import/queue": {"counts": {}, "recent": []},
    }


def test_admin_navigation_actually_renders(tmp_path) -> None:
    """点侧栏菜单 → 主区域必须换内容；导入页必须有「选择文件」入口。"""
    js = PAGE.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    (tmp_path / "page.js").write_text(js, encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    meta = tmp_path / "responses.json"
    meta.write_text(json.dumps(_responses(), ensure_ascii=False), encoding="utf-8")

    proc = subprocess.run(
        ["node", str(harness), str(tmp_path / "page.js"), str(meta)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"导航冒烟失败：\n{proc.stdout}\n{proc.stderr}"
    assert "OK" in proc.stdout


def test_page_registers_hashchange_and_uses_native_links() -> None:
    """结构断言（作为行为用例的补充）：原生锚点 + hashchange 监听必须都在。

    缺监听时锚点能改 hash 但没人渲染；缺锚点则退化成只能靠 JS 跳转。
    """
    js = PAGE.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    assert 'addEventListener("hashchange"' in js, "没有 hashchange 监听 → 点菜单不会重画"
    assert 'href="#/${m.id}"' in js or 'href="#/' in js, "菜单项不是原生锚点"
    # 常驻快捷入口必须在 #menu 之外，否则会被 buildMenu() 重画冲掉
    html = PAGE.split("<script>", 1)[0]
    assert 'id="quickImport"' in html
    assert html.index('id="quickImport"') < html.index('id="menu"')
