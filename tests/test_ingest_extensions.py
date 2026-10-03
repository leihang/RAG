"""入库「能收哪些格式」的一致性测试。

为什么单独一个文件：这条不变量曾经**三方各写一份、互不一致**，而且没有任何用例守着 ——

| 来源 | pdf | png/jpg | doc/ppt/xls | pptx |
|---|---|---|---|---|
| CLI 默认白名单（`ekbs.tools.import`） | ❌ | ❌ | ❌ | ✅（解析器没实现） |
| 配置 `limits.file_ext_whitelist` | ✅ | ✅ | ✅（解析器没实现） | ✅ |
| 解析器实际能力（`parser.SUFFIX_ENGINES`） | ✅ | ✅ | ❌ | ❌ |

后果（都实测踩到过）：

* **PDF 不在默认白名单** ⇒ 不加 `--ext .pdf` 时 PDF **根本不被发现**，输出只有
  `发现文件 0`，连一条"跳过"都没有 —— 操作者会以为"没报错=没问题"；
* **`.pptx` 在白名单但解析器没实现** ⇒ 走纯文本分支，把二进制当 UTF-8 读成乱码入库
  （实测 24 KB 的 `.doc` 读出来是 24450 字符的乱码）。

现在 CLI 默认白名单直接引用解析器常量，本文件把这条对齐关系锁死。
"""

from __future__ import annotations

import importlib
from pathlib import Path

from ekbs.ingest.parser import SUFFIX_ENGINES

#: 传给 ``load_settings`` 的仓库配置（与 conftest 同一份）
REPO_CONFIG = Path(__file__).resolve().parent.parent / "config" / "application.dev.yaml"


def _import_tool():
    # `import` 是关键字，不能写成 import 语句
    return importlib.import_module("ekbs.tools.import")


def test_cli_default_extensions_match_parser_capability() -> None:
    """CLI 默认白名单 == 解析器真正实现的格式（单点对齐，不再各写一份）。"""
    tool = _import_tool()
    assert set(tool.DEFAULT_EXTENSIONS) == set(SUFFIX_ENGINES), (
        "CLI 默认白名单与解析器能力不一致：\n"
        f"  仅白名单有：{sorted(set(tool.DEFAULT_EXTENSIONS) - set(SUFFIX_ENGINES))}\n"
        f"  仅解析器有：{sorted(set(SUFFIX_ENGINES) - set(tool.DEFAULT_EXTENSIONS))}"
    )


def test_pdf_is_in_default_whitelist() -> None:
    """**回归**：PDF 曾经不在默认白名单里 —— 不加 `--ext .pdf` 就静默一个都不导入。

    PDF 是方案里的主力格式（`01` §4 引入 MinerU 就是为中文制度 PDF 服务的），
    它必须默认就能被发现。
    """
    tool = _import_tool()
    assert ".pdf" in tool.DEFAULT_EXTENSIONS


def test_legacy_office_formats_are_now_supported() -> None:
    """**回归**：`.doc/.pptx/.xls` 曾经只在白名单里、没有解析分支 → 放进来变乱码。

    现在它们各有专门分支（`textutil` 转 docx / `python-pptx` / `xlrd`），必须出现在白名单里。
    """
    tool = _import_tool()
    for suffix in (".doc", ".pptx", ".xls"):
        assert suffix in tool.DEFAULT_EXTENSIONS, f"{suffix} 已实现解析，应进默认白名单"
        assert suffix in SUFFIX_ENGINES


def test_formats_without_a_parser_branch_are_not_whitelisted() -> None:
    """配置白名单里有、但**解析器仍未实现**的格式，不能进默认白名单。

    `.ppt`（需 LibreOffice）目前仍没有分支；`.html` 已于 2026-10-03 实现
      （标准库解析，剥标签 + 保留标题/列表/表格）—— 它们进白名单
    只会把原始内容当文本塞进知识库。等实现了对应分支，把它加进 `SUFFIX_ENGINES`，
    本用例会自动放行（同一份名单驱动 CLI 与 API 两侧，见 `docs.py` 的取交集）。
    """
    tool = _import_tool()
    unimplemented = {".ppt"}
    leaked = sorted(unimplemented & set(tool.DEFAULT_EXTENSIONS))
    assert not leaked, f"这些格式没有解析分支，不该进默认白名单：{leaked}"


def test_every_whitelisted_suffix_has_an_engine() -> None:
    """白名单里每个扩展名都要能映射到引擎，且引擎名是已知的四种之一。"""
    # 引擎名必须与 parser.parse_file 里的分支一一对应（新增引擎时同步这里）
    known = {"text", "docx", "xlsx", "mineru", "doc", "pptx", "xls", "html"}
    for suffix, engine in SUFFIX_ENGINES.items():
        assert suffix.startswith("."), f"扩展名要带点：{suffix}"
        assert engine in known, f"{suffix} 指向未知引擎 {engine}"


# ---------------------------------------------------------------------------
# 两张「支持清单」：批量导入（CLI）与上传接口（HTTP）口径不同，别混为一谈
# ---------------------------------------------------------------------------

#: 引擎 → 该引擎负责的后缀数。这份数字是**对外承诺的清单**，改动必须是有意的：
#: 新增引擎/后缀时同步这里，并同步 README 的「支持的文件类型」表。
_EXPECTED_ENGINE_COUNTS = {
    "mineru": 4,   # pdf/png/jpg/jpeg
    "doc": 1,      # 旧版 Word（textutil）
    "docx": 1,
    "xls": 1,      # 旧版 Excel（xlrd）
    "xlsx": 1,
    "pptx": 1,
    "html": 2,     # .html/.htm（标准库解析，保留标题/列表/表格）
    "text": 54,    # 51 种代码后缀 + .md/.markdown/.txt
}


def test_parser_capability_inventory_is_pinned() -> None:
    """解析器能力清单（65 种）与各引擎数量锁定，防止清单被悄悄改动。"""
    from collections import Counter

    counts = Counter(SUFFIX_ENGINES.values())
    assert dict(counts) == _EXPECTED_ENGINE_COUNTS, (
        f"解析器能力清单变了：{dict(counts)} ≠ {_EXPECTED_ENGINE_COUNTS}"
    )
    assert len(SUFFIX_ENGINES) == sum(_EXPECTED_ENGINE_COUNTS.values()) == 65


def test_code_suffixes_are_all_parseable_as_text() -> None:
    """51 种代码后缀必须全部落在 text 引擎上，否则永远进不了代码分块。"""
    from ekbs.ingest.code_chunker import CODE_SUFFIXES

    assert len(CODE_SUFFIXES) == 51
    missing = sorted(s for s in CODE_SUFFIXES if s not in SUFFIX_ENGINES)
    assert not missing, f"代码后缀没登记进 SUFFIX_ENGINES：{missing}"
    wrong = sorted(s for s in CODE_SUFFIXES if SUFFIX_ENGINES[s] != "text")
    assert not wrong, f"代码后缀的引擎不是 text：{wrong}"


def test_upload_api_is_config_intersection_and_is_narrower_than_cli(tmp_path) -> None:
    """**上传接口（HTTP）比批量导入（CLI）窄** —— 这条差异必须写在用例里，不能靠记忆。

    * CLI：白名单 = `SUFFIX_ENGINES`（65 种）
    * HTTP：白名单 = `配置 file_ext_whitelist` ∩ `SUFFIX_ENGINES` = **12 种**

    配置里只有 13 项且**不含任何代码后缀**，所以经 HTTP 上传不了代码文件
    （`.py` 等会被 422 拒掉），代码知识库目前只能走 CLI 批量导入。
    这不是 bug，但如果哪天要支持「管理台上传代码」，改的是**配置**（加后缀即可，
    取交集那行会自动放行），不需要动解析器。
    """
    from ekbs.config import load_settings

    settings = load_settings(
        REPO_CONFIG,
        data_dir=tmp_path / "var",
        db_path=tmp_path / "var" / "index" / "ekbs.db",
    )
    configured = {str(e).lower().lstrip(".") for e in settings.limits["file_ext_whitelist"]}
    parseable = {s.lstrip(".") for s in SUFFIX_ENGINES}
    uploadable = configured & parseable

    assert uploadable == {
        "pdf", "docx", "doc", "pptx", "xlsx", "xls", "txt", "md", "html",
          "png", "jpg", "jpeg",
    }, f"上传接口可收格式变了：{sorted(uploadable)}"
    # 配置允许但解析器没实现 —— 它会在接口层被 422 且给出原因
    assert configured - parseable == {"ppt"}
    # 关键差异：代码后缀在解析器里、却不在配置里 ⇒ HTTP 收不了
    code_like = {"py", "ts", "go", "java", "sql", "sh"}
    assert code_like <= parseable and not (code_like & configured)
    assert len(uploadable) < len(parseable), "上传口径不应等于 CLI 口径（否则此用例已失效）"


def test_config_whitelist_entries_are_all_known_gaps_or_implemented() -> None:
    """配置白名单里每一项要么解析器已实现，要么是**已知未实现**（`.ppt`）。

    新增"配置里有、解析器没有"的格式时，这里会失败 —— 提醒你它会在上传时被
    422（而不是静默收下再变乱码），要么补解析分支，要么从配置里删掉。
    """
    from ekbs.config import load_settings

    settings = load_settings(REPO_CONFIG)
    configured = {str(e).lower().lstrip(".") for e in settings.limits["file_ext_whitelist"]}
    parseable = {s.lstrip(".") for s in SUFFIX_ENGINES}
    assert configured - parseable == {"ppt"}, (
        "配置里出现了新的『允许但未实现』格式："
        f"{sorted(configured - parseable)}（补解析分支或改本用例的已知清单）"
    )

