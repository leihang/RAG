"""代码入库与图片块的**端到端接线**（不是单测分块器）。

分块器的纯函数测试在 ``test_code_chunker.py`` / ``test_chunker.py``；
这里只管一件事：**接线有没有真的生效** ——
  1. ``source_path`` 是代码文件时，``ingest_markdown`` 是否走了 ``chunk_code``，
     落库的 ``heading_path`` 是否带 ``<path> > Class.method`` 符号路径；
  2. ``figures=`` 传进来的图片是否变成 ``chunk_type='figure'`` 的可检索块
     （入向量 + 入 FTS + 记住 ``image_path`` 供回源）。

历史教训：分块器写好了、pipeline 里忘了路由，单测全绿但线上不生效。
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest

from ekbs.ingest.chunker import FigureInput
from ekbs.ingest.pipeline import IngestPipeline
from tests.conftest import user_id_of

#: 1×1 的合法 PNG，只为让文件真的存在（本用例不校验像素）
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)

PY_SOURCE = '''\
"""示例模块。"""

import os


class OrderService:
    """订单服务。"""

    def submit(self, order_id: str) -> bool:
        """提交订单，并且这句注释足够长以便被切出来。"""
        if not order_id:
            raise ValueError("order_id 不能为空")
        return os.path.exists(order_id)
'''


def _pipeline(runtime) -> IngestPipeline:
    return IngestPipeline(
        db=runtime.db,
        settings=runtime.settings,
        providers=runtime.providers,
        shards=runtime.shards,
        fts=runtime.fts,
        figure_index=runtime.figure_index,
    )


async def test_code_source_is_routed_to_code_chunker(seeded) -> None:
    """``source_path`` 是 ``.py`` → 走符号切分，heading_path 带 ``路径 > 类.方法``。"""
    runtime, summary = seeded
    before_vectors = {
        row.shard_id: row.vector_count for row in runtime.shards.registry()
    }.get("kb_all_L1", 0)
    result = await _pipeline(runtime).ingest_markdown(
        title="demo/service.py",
        markdown=PY_SOURCE,
        kb_id=int(summary["kb_id"]),
        level=1,
        visible_dept_ids=[],
        source_path="demo/service.py",
        real_corpus=False,
    )
    assert result.status == "published", result.status
    # 代码的检索单元是**符号块本身**（chunk_type='parent'，没有子块），
    # 所以 child_count 为 0 是正常的；要看 vector_count 才知道真的入了库可检索。
    assert result.vector_count > 0, "代码符号块没有入向量（=存了但搜不到）"

    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT chunk_type, heading_path, faiss_row, embedding_fingerprint "
            "FROM ekbs_chunk WHERE doc_id = ? ORDER BY seq",
            (result.doc_id,),
        ).fetchall()

    assert rows, "代码文件没有落任何块"
    paths = [row["heading_path"] or "" for row in rows]
    # 符号路径：至少一个块带着「文件 > 类.方法」的层级（这是代码 KB 与普通文档的分水岭）
    symbolic = [p for p in paths if " > " in p and "OrderService" in p]
    assert symbolic, f"没有符号层级 heading_path：{paths}"
    assert any(p.startswith("demo/service.py") for p in paths), paths
    # 每一块都必须真的入过向量（faiss_row 是「入没入」的唯一凭据）
    assert all(row["faiss_row"] is not None for row in rows), "有块没进 FAISS 分片"
    assert all(row["embedding_fingerprint"] for row in rows), "有块没记模型指纹"

    # 「存了」必须等于「搜得到」：可检索块数 == FTS 覆盖的 distinct chunk == 分片向量数。
    # Doc2Query 会让同一 chunk 多行 FTS（问题+原文），所以对账按 **distinct chunk_id**，
    # 不是 raw FTS 行数（否则会把假设问句当成多块）。
    with runtime.db.sync_connect() as conn:
        fts_rows = int(
            conn.execute(
                "SELECT COUNT(DISTINCT f.chunk_id) FROM ekbs_chunk_fts f JOIN ekbs_chunk c "
                "ON c.chunk_id = CAST(f.chunk_id AS INTEGER) WHERE c.doc_id = ?",
                (result.doc_id,),
            ).fetchone()[0]
        )
    registered = {
        row.shard_id: row.vector_count for row in runtime.shards.registry()
    }[result.shard_id]
    assert fts_rows == result.vector_count, f"FTS distinct {fts_rows} ≠ 向量 {result.vector_count}"
    assert registered == before_vectors + result.vector_count, (
        f"分片登记向量 {registered} ≠ 入库前 {before_vectors} + {result.vector_count}"
    )


async def test_code_symbol_is_actually_retrievable(seeded) -> None:
    """需求③的最终判定：**代码块要真的被检索链路召回**，不是"落了库就算"。

    用 ``OrderService`` 这种只存在于代码里的符号名提问 —— 它没有出现在任何
    Markdown 制度文档里，召回到它就只可能来自代码知识库。
    """
    runtime, summary = seeded
    result = await _pipeline(runtime).ingest_markdown(
        title="demo/service.py",
        markdown=PY_SOURCE,
        kb_id=int(summary["kb_id"]),
        level=1,
        visible_dept_ids=[],
        source_path="demo/service.py",
        real_corpus=False,
    )
    alice = await runtime.perm_context(user_id_of(runtime.db, "demo_alice"))
    hits = await runtime.pipeline.retrieve("OrderService submit 提交订单", alice)
    assert hits.refused is False, hits.refusal_reason
    code_hits = [c for c in hits.chunks if c.doc_id == result.doc_id]
    assert code_hits, "代码块召不回来：等于代码库白建"
    assert any("OrderService" in (c.heading_path or "") for c in code_hits), [
        c.heading_path for c in code_hits
    ]


async def test_markdown_without_source_path_keeps_heading_chunker(seeded) -> None:
    """没有 ``source_path`` 的普通文档仍走 Markdown 切分，不能被误判成代码。"""
    runtime, summary = seeded
    result = await _pipeline(runtime).ingest_markdown(
        title="普通制度",
        markdown="# 差旅费管理办法\n\n## 住宿标准\n\n一线城市每晚不超过 600 元。\n",
        kb_id=int(summary["kb_id"]),
        level=1,
        visible_dept_ids=[],
        real_corpus=False,
    )
    with runtime.db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT heading_path FROM ekbs_chunk WHERE doc_id = ?",
            (result.doc_id,),
        ).fetchall()
    paths = [row["heading_path"] or "" for row in rows]
    assert any("住宿标准" in p for p in paths), paths
    # 代码切分器的符号分隔符不应出现在普通文档里
    assert not any(p.startswith("普通制度 > ") for p in paths), paths


async def test_figure_becomes_bound_asset_not_retrievable(seeded) -> None:
    """``figures=`` → 资产行挂 parent；不入向量/FTS；保留回源路径。"""
    runtime, summary = seeded
    image_rel = "parse/testfig/images/fig1.png"
    image_abs = runtime.settings.data_dir / image_rel
    image_abs.parent.mkdir(parents=True, exist_ok=True)
    image_abs.write_bytes(_PNG)

    result = await _pipeline(runtime).ingest_markdown(
        title="带图文档",
        markdown="# 组织架构\n\n如图所示，公司设两个部门。\n",
        kb_id=int(summary["kb_id"]),
        level=1,
        visible_dept_ids=[],
        real_corpus=False,
        figures=(
            FigureInput(
                image_path=image_rel,
                caption="图 1 组织架构",
                ocr_text="组织架构图 总经理 下辖财务部与人力资源部",
                page=3,
                figure_id="图 1",
            ),
        ),
    )
    assert result.parent_count > 0 or result.child_count > 0, "正文块也没切出来"

    with runtime.db.sync_connect() as conn:
        figs = conn.execute(
            "SELECT chunk_id, content, figure_id, image_path, page_from, faiss_row, parent_id "
            "FROM ekbs_chunk WHERE doc_id = ? AND chunk_type = 'figure'",
            (result.doc_id,),
        ).fetchall()
        fts_rows = conn.execute(
            "SELECT COUNT(DISTINCT f.chunk_id) AS n FROM ekbs_chunk_fts f JOIN ekbs_chunk c "
            "ON c.chunk_id = CAST(f.chunk_id AS INTEGER) "
            "WHERE c.doc_id = ? AND c.chunk_type = 'figure'",
            (result.doc_id,),
        ).fetchone()["n"]
        parents = {
            int(r["chunk_id"])
            for r in conn.execute(
                "SELECT chunk_id FROM ekbs_chunk WHERE doc_id = ? AND chunk_type = 'parent'",
                (result.doc_id,),
            ).fetchall()
        }

    assert len(figs) == 1, f"期望 1 个 figure 块，实得 {len(figs)}"
    fig = figs[0]
    assert fig["figure_id"] == "图 1"
    assert fig["image_path"] == image_rel, "回源路径必须是相对 data_dir 的路径"
    assert fig["page_from"] == 3
    assert fig["content"] == "[图片]", "图块正文只占位，不写 OCR/题注"
    assert fig["faiss_row"] is None, "figure 不得进文本向量分片"
    assert fts_rows == 0, "figure 不得进 FTS"
    assert fig["parent_id"] in parents, "figure 必须挂到章节 parent"
    assert image_abs.exists(), "图片文件应当仍留在磁盘上（否则引用打不开）"


async def test_figure_without_caption_still_bound_asset(seeded) -> None:
    """无题注/OCR 仍建资产行并绑定 parent（不做识别、不入检索）。"""
    runtime, summary = seeded
    image_rel = "parse/testfig/images/blank.png"
    image_abs = runtime.settings.data_dir / image_rel
    image_abs.parent.mkdir(parents=True, exist_ok=True)
    image_abs.write_bytes(_PNG)

    result = await _pipeline(runtime).ingest_markdown(
        title="空图文档",
        markdown="# 正文\n\n这里有一张没有信息量的图。\n",
        kb_id=int(summary["kb_id"]),
        level=1,
        visible_dept_ids=[],
        real_corpus=False,
        figures=(FigureInput(image_path=image_rel),),
    )
    with runtime.db.sync_connect() as conn:
        figs = conn.execute(
            "SELECT content, image_path, parent_id FROM ekbs_chunk "
            "WHERE doc_id = ? AND chunk_type = 'figure'",
            (result.doc_id,),
        ).fetchall()
    assert len(figs) == 1, "有路径即建 figure 资产行"
    assert figs[0]["content"] == "[图片]"
    assert figs[0]["image_path"] == image_rel
    assert figs[0]["parent_id"] is not None


async def test_figure_without_image_path_is_dropped() -> None:
    """没有落盘路径的图不建块。"""
    from ekbs.ingest.chunker import chunk_markdown

    result = chunk_markdown(
        "# 正文\n\n有图。\n",
        figures=(FigureInput(image_path=""),),
        doc_title="有标题",
    )
    assert result.figures == []


@pytest.mark.parametrize("suffix", [".py", ".ts", ".go", ".java", ".sql"])
def test_code_suffixes_are_registered_as_text_engine(suffix: str) -> None:
    """代码后缀在解析层必须被认成文本，否则永远进不了 ``chunk_code``。"""
    from ekbs.ingest.parser import SUFFIX_ENGINES

    assert SUFFIX_ENGINES.get(suffix) == "text", f"{suffix} 未登记为 text 引擎"


# ---------------------------------------------------------------------------
# 防复发：可检索口径只能有一处定义
# ---------------------------------------------------------------------------

#: 允许出现字面量 ``child`` 过滤的文件 → 理由。新增条目必须写清楚为什么它
#: **不是**在重复定义"什么算可检索"。
_ALLOWED_CHILD_LITERALS = {
    "ekbs/ingest/retrievable.py": "规则的定义处",
    "ekbs/ingest/chunker.py": "ChunkingResult.children 定义『什么算 child』，与可检索口径无关",
    "ekbs/retrieval/pipeline.py": "父块扩展只对 child 回填；叶子 parent 自带全文，无需扩展",
    "ekbs/api/parse_tasks.py": "child_count 是真实子块数，可检索数另由 retrievable_count 表示",
    "ekbs/api/chunks.py": "人工切分/合并只支持 child，代码符号块不支持在线编辑",
}

#: 必须使用统一口径的模块（写入 / 重建 / 换级 / 巡检 / 发布 / 统计）。
_MUST_USE_SINGLE_SOURCE = (
    "ekbs/ingest/pipeline.py",
    "ekbs/tools/rebuild_shard.py",
    "ekbs/tools/import.py",
    "ekbs/permission/changes.py",
    "ekbs/scheduler.py",
    "ekbs/api/docs.py",
)


def _iter_ekbs_sources():
    import ekbs

    root = Path(ekbs.__file__).resolve().parent
    for path in sorted(root.rglob("*.py")):
        yield path.relative_to(root.parent).as_posix(), path.read_text(encoding="utf-8")


def test_no_module_redefines_what_is_retrievable() -> None:
    """``chunk_type = 'child'`` 不得再被当成"可检索"来用。

    这条规则曾散落在 8 个文件里：分块器一加新块类型，写入侧改了、
    重建/换级/巡检没改，就出现"存了但搜不到"或"向量与库对不上"的静默故障。
    """
    pattern = re.compile(r"chunk_type\s*(?:=|==)\s*['\"]child['\"]")
    offenders: list[str] = []
    for relpath, text in _iter_ekbs_sources():
        if relpath in _ALLOWED_CHILD_LITERALS:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{relpath}:{number}: {line.strip()}")
    assert not offenders, (
        "请改用 ekbs.ingest.retrievable 的 RETRIEVABLE_PREDICATE / retrievable_chunk_ids：\n"
        + "\n".join(offenders)
    )


def test_every_retrieval_consumer_uses_the_single_source() -> None:
    """写入、重建分片、换级重嵌、三方对账、发布闸门都必须走同一口径。"""
    from ekbs.ingest import retrievable

    _ = retrievable  # 确保模块可导入（拼写错误会在这里暴露）
    missing: list[str] = []
    for relpath, text in _iter_ekbs_sources():
        if relpath not in _MUST_USE_SINGLE_SOURCE:
            continue
        if "RETRIEVABLE_PREDICATE" not in text and "retrievable_chunk_ids" not in text:
            missing.append(relpath)
    assert not missing, f"这些模块仍在各自定义可检索口径：{missing}"


def test_retrievable_predicate_matches_leaf_rule() -> None:
    """纯函数版与 SQL 版口径一致：有子块的类型化容器不算检索单元。"""
    from ekbs.ingest.retrievable import RETRIEVABLE_TYPES, is_retrievable

    assert is_retrievable("child", has_children=False) is True
    assert is_retrievable("figure", has_children=False) is False, "图片只作附件，不入检索"
    assert is_retrievable("table", has_children=False) is True
    assert is_retrievable("parent", has_children=False) is True, "代码符号块必须可检索"
    assert is_retrievable("parent", has_children=True) is False, "Markdown 父容器不入向量"
    assert is_retrievable("summary", has_children=False) is False, "摘要块不抢检索名额"
    assert "summary" not in RETRIEVABLE_TYPES
    assert "figure" not in RETRIEVABLE_TYPES


# ---------------------------------------------------------------------------
# heading_path 里不得出现机器绝对路径
# ---------------------------------------------------------------------------


def test_display_source_path_never_leaks_absolute_machine_paths(settings) -> None:
    """代码块的 ``heading_path`` 前缀必须是**稳定可检索路径**，不是磁盘绝对路径。

    实测踩到：worker 与 CLI 传的是绝对路径，于是代码块的 heading 变成
    ``/Users/<用户名>/…/var/raw/代码样例/order_service.py > OrderService`` ——
    同一份代码在不同机器/不同 data_dir 下检索语义不同，还把服务器目录结构
    随引用卡片泄漏出去。
    """
    from ekbs.ingest.code_chunker import display_source_path

    raw = settings.data_paths()["raw"]
    # 1) raw 之下 → raw/<相对路径>
    assert display_source_path(settings, str(raw / "代码样例/order_service.py")) == (
        "raw/代码样例/order_service.py"
    )
    # 2) data_dir 其它位置（parse 产物）→ 相对 data_dir
    assert display_source_path(
        settings, str(settings.data_paths()["parse"] / "12/images/a.png")
    ) == "parse/12/images/a.png"
    # 3) 本来就是相对路径 → 原样（CLI `--dir ekbs/ingest` 的情形）
    assert display_source_path(settings, "ekbs/ingest/parser.py") == "ekbs/ingest/parser.py"
    # 4) 数据目录之外 → 只留文件名，绝不泄漏机器布局
    leaked = display_source_path(settings, "/etc/passwd")
    assert leaked == "passwd", leaked
    assert "/etc" not in leaked


async def test_code_ingested_from_absolute_path_has_stable_heading(seeded) -> None:
    """端到端：从**绝对路径**入库的代码，heading_path 里不得出现绝对路径。

    这正是管理台「服务器目录导入」的路径 —— 它交给 worker 的就是 raw 下的绝对路径。
    """
    runtime, summary = seeded
    raw = runtime.settings.data_paths()["raw"]
    target = raw / "代码样例" / "order_service.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(PY_SOURCE, encoding="utf-8")

    result = await _pipeline(runtime).ingest_markdown(
        title="order_service",
        markdown=PY_SOURCE,
        kb_id=int(summary["kb_id"]),
        level=1,
        visible_dept_ids=[],
        source_path=str(target),          # ← 绝对路径，与 worker 的做法一致
        real_corpus=False,
    )
    with runtime.db.sync_connect() as conn:
        paths = [
            str(r["heading_path"] or "")
            for r in conn.execute(
                "SELECT heading_path FROM ekbs_chunk WHERE doc_id = ?", (result.doc_id,)
            )
        ]
    assert paths, "没有落块"
    for path in paths:
        assert not path.startswith("/"), f"heading_path 泄漏了绝对路径：{path}"
        assert "/Users/" not in path and "/var/" not in path, path
    assert any(p.startswith("raw/代码样例/order_service.py") for p in paths), paths
    assert any("OrderService.submit" in p for p in paths), paths
