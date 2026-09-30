"""管理台页面（`/admin`）自身的回归：**它是内嵌在 Python 字符串里的单文件 JS**。

为什么值得单独测：这个页面的一切都在 ``ekbs/api/admin_ui.py::PAGE`` 里，
一旦 JS 语法坏了（最典型的是在 Python 普通字符串里写了裸 ``\\n``，Python 先把它变成
真换行，JS 字符串就断了），**接口全绿、页面全白**，而且没有任何后端用例会红。
本文件把它按住：

1. JS 语法（有 node 时真跑 ``node --check``，没有就跳过 —— 不把 CI 绑死在 node 上）；
2. 菜单 ↔ 视图一一对应（菜单点进去白屏是最容易漏的一类）；
3. 每个菜单项都声明了权限点（否则会显示成"人人可见"，点进去 403）。
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from ekbs.api.admin_ui import PAGE


def _js() -> str:
    assert "<script>" in PAGE, "管理台页面结构变了：找不到 <script>"
    return PAGE.split("<script>", 1)[1].rsplit("</script>", 1)[0]


def test_page_is_self_contained_single_file() -> None:
    """免构建单页：不引外部 JS/CSS（部署机上没有前端工具链）。"""
    assert PAGE.strip().startswith("<!DOCTYPE html>")
    assert "<script src=" not in PAGE, "管理台不应依赖外部脚本文件"
    assert "<link rel=\"stylesheet\"" not in PAGE, "管理台不应依赖外部样式文件"


def test_js_braces_are_balanced() -> None:
    js = _js()
    assert js.count("{") == js.count("}"), "JS 大括号不配平（多半是插入代码时漏了半边）"
    assert js.count("(") == js.count(")"), "JS 小括号不配平"


@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node，跳过真实语法检查")
def test_js_passes_node_syntax_check(tmp_path) -> None:
    """**真解析一遍**：字符串配平说明不了语法正确（比如未转义的换行）。"""
    script = tmp_path / "admin_ui.js"
    script.write_text(_js(), encoding="utf-8")
    proc = subprocess.run(
        ["node", "--check", str(script)], capture_output=True, text=True
    )
    assert proc.returncode == 0, f"管理台 JS 语法错误：\n{proc.stderr}"


def test_every_menu_has_a_view() -> None:
    """菜单里出现的 id 必须有对应 ``VIEWS.<id>``，否则点进去是空白页。"""
    js = _js()
    menu_ids = re.findall(r"\{id:\s*\"(\w+)\"[^}]*\}", js)
    view_ids = set(re.findall(r"VIEWS\.(\w+)\s*=", js))
    assert menu_ids, "没解析到菜单项（MENUS 结构变了？）"
    missing = [i for i in menu_ids if i not in view_ids]
    assert not missing, f"这些菜单没有对应视图：{missing}"


def test_every_menu_declares_a_permission() -> None:
    """菜单必须按权限点过滤（`buildMenu` 就是这么用的）。

    漏写 ``perm`` 会让菜单对所有人可见，点进去再吃 403 —— 权限没问题，
    但体验和"看起来能用"的误导都很糟。
    """
    js = _js()
    items = re.findall(r"\{id:\s*\"(\w+)\",\s*name:\s*\"[^\"]*\",\s*perm:\s*\"([^\"]+)\"\}", js)
    menu_ids = re.findall(r"\{id:\s*\"(\w+)\"", js)
    assert len(items) == len(menu_ids), "有菜单项没写 perm（或格式变了）"
    assert all(p.count(":") >= 1 for _, p in items), f"权限点格式可疑：{items}"


def test_import_page_wires_the_two_lists() -> None:
    """导入页必须同时给出「目录导入 63 种」与「上传 11 种」，且都来自后端元信息。

    前端写死数字就会与后端白名单漂移 —— 所以这里断言它读的是 ``/import/meta``。
    """
    js = _js()
    assert 'api("/import/meta")' in js
    assert "dir_extensions" in js and "upload_extensions" in js
    # 三个动作齐备：浏览目录、扫描预览、执行导入
    for act in ("impGo", "impScan", "impRun"):
        assert f'"{act}"' in js, f"导入页缺少动作 {act}"
