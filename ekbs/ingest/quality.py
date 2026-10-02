"""入库质量指标 —— 解析/清洗准出 + 分片内在统计（`06` §1.7、`01` §12）。

与「解析成功率」（``ekbs.tools.import`` 已卡）互补：本模块看**内容质量**，
不看「文件是否跑完」。阈值与方案一致：

* 空页率 ≤ 1%
* 乱码率（``U+FFFD`` 占比）≤ 0.5%
* 表格存活率 ≥ 95%（已入库 table 块中正文非空的比例；无表则 N/A）
* 页码连续性：有页码时缺失区间记 WARN，不阻断发布

分片内在指标（L2）不设硬闸，只出报告，便于投入测试时盯回归。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Sequence

#: `06` §1.7 准出门槛
EMPTY_PAGE_RATE_MAX = 0.01
GARBLED_RATE_MAX = 0.005
TABLE_SURVIVAL_MIN = 0.95

#: 页上聚合正文低于该字符数视为「空页」
EMPTY_PAGE_CHAR_THRESHOLD = 20

#: 可检索块过短 / 过长（token）；短节叶子允许低于 CHILD_MIN，故用更松的观察阈值
LEAF_SHORT_TOKENS = 32
LEAF_LONG_TOKENS = 512

#: figure 检索文本过短（字符）
FIGURE_TEXT_SHORT_CHARS = 8

REPLACEMENT_CHAR = "\ufffd"


@dataclass(frozen=True)
class Thresholds:
    empty_page_rate_max: float = EMPTY_PAGE_RATE_MAX
    garbled_rate_max: float = GARBLED_RATE_MAX
    table_survival_min: float = TABLE_SURVIVAL_MIN
    empty_page_char_threshold: int = EMPTY_PAGE_CHAR_THRESHOLD


@dataclass
class ParseQuality:
    """单篇文档的解析/清洗指标（L1）。"""

    doc_id: int
    title: str = ""
    status: str = ""
    total_chars: int = 0
    replacement_chars: int = 0
    garbled_rate: float = 0.0
    pages_applicable: bool = False
    page_count: int = 0
    empty_pages: int = 0
    empty_page_rate: float = 0.0
    page_min: int | None = None
    page_max: int | None = None
    missing_pages: list[int] = field(default_factory=list)
    page_continuity_ok: bool = True
    #: 有内容、但内容只有图或表（正文短）的页 —— **不算空页**（见 empty 的判定）
    visual_pages: int = 0
    #: 源文件页数（`ekbs_document.page_count`）。有它才能算**真丢页**：
    #: 只按"块覆盖到的页码"算，尾部的空白/未解析页是看不见的。
    source_page_count: int | None = None
    #: 源文件页数范围内**一个块都没有**的页（不是"正文短"，是"完全没产出"）→ 需人工对源文件核
    lost_pages: list[int] = field(default_factory=list)
    lost_page_rate: float | None = None
    table_count: int = 0
    table_nonempty: int = 0
    table_survival_rate: float | None = None
    checks: dict[str, bool | None] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """硬闸全过（N/A 项视为通过）。"""
        for key in ("garbled_ok", "empty_page_ok", "table_survival_ok"):
            value = self.checks.get(key)
            if value is False:
                return False
        return True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChunkStats:
    """单篇文档的分片内在统计（L2）。"""

    doc_id: int
    title: str = ""
    total_chunks: int = 0
    leaf_chunks: int = 0
    parent_containers: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    leaf_short: int = 0
    leaf_long: int = 0
    leaf_short_rate: float = 0.0
    leaf_long_rate: float = 0.0
    dual_index_pairs: int = 0
    dual_index_rate: float = 0.0
    empty_heading_leaves: int = 0
    empty_heading_rate: float = 0.0
    figure_short_text: int = 0
    figure_count: int = 0
    figure_short_rate: float = 0.0
    token_p50: int | None = None
    token_p90: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DocQualityReport:
    parse: ParseQuality
    chunks: ChunkStats

    def as_dict(self) -> dict[str, Any]:
        return {"parse": self.parse.as_dict(), "chunks": self.chunks.as_dict()}


@dataclass
class BatchQualityReport:
    docs: list[DocQualityReport] = field(default_factory=list)
    thresholds: dict[str, float] = field(default_factory=dict)
    failed_doc_ids: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed_doc_ids

    def as_dict(self) -> dict[str, Any]:
        return {
            "doc_count": len(self.docs),
            "failed_doc_ids": self.failed_doc_ids,
            "ok": self.ok,
            "thresholds": self.thresholds,
            "docs": [d.as_dict() for d in self.docs],
        }


def garbled_rate(text: str) -> tuple[int, int, float]:
    """返回 ``(总字符, U+FFFD 数, 乱码率)``。空串乱码率为 0。"""
    total = len(text)
    if total <= 0:
        return 0, 0, 0.0
    bad = text.count(REPLACEMENT_CHAR)
    return total, bad, bad / total


def _percentile(sorted_vals: Sequence[int], p: float) -> int | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return int(sorted_vals[0])
    idx = int(round((len(sorted_vals) - 1) * p))
    idx = max(0, min(len(sorted_vals) - 1, idx))
    return int(sorted_vals[idx])


def _page_coverage_chars(
    rows: Sequence[Mapping[str, Any]],
) -> dict[int, int]:
    """按页汇总正文长度（同一页多块累加）。无页码的块忽略。"""
    coverage: dict[int, int] = {}
    for row in rows:
        page_from = row.get("page_from")
        page_to = row.get("page_to")
        if page_from is None:
            continue
        start = int(page_from)
        end = int(page_to) if page_to is not None else start
        if end < start:
            start, end = end, start
        content = str(row.get("content") or "")
        # 跨页块：字符均摊到覆盖页，避免单页被重复计满长
        span = max(1, end - start + 1)
        per = max(1, len(content.strip()) // span) if content.strip() else 0
        for page in range(start, end + 1):
            coverage[page] = coverage.get(page, 0) + per
    return coverage


def assess_parse_quality(
    *,
    doc_id: int,
    title: str,
    status: str,
    chunks: Sequence[Mapping[str, Any]],
    source_page_count: int | None = None,
    slide_based: bool = False,
    thresholds: Thresholds | None = None,
) -> ParseQuality:
    """从已入库分块评估解析/清洗质量（不依赖磁盘 parse 产物）。"""
    thr = thresholds or Thresholds()
    joined = "\n".join(str(c.get("content") or "") for c in chunks)
    total, bad, rate = garbled_rate(joined)

    coverage = _page_coverage_chars(chunks)
    pages_applicable = bool(coverage)
    empty_pages = 0
    missing: list[int] = []
    page_min: int | None = None
    page_max: int | None = None
    continuity_ok = True
    visual_page_set: set[int] = set()
    if pages_applicable:
        page_min = min(coverage)
        page_max = max(coverage)
        # 「有图 / 有表」的页不算空页：正文短不等于没内容。
        # PPT 的章节过渡页、扫描件的插图页都是这一类（实测 doc 33/41 的闸门假警报全来自这里）。
        for row in chunks:
            if str(row.get("chunk_type") or "") not in ("figure", "table"):
                continue
            pf = row.get("page_from")
            if pf is None:
                continue
            pt = row.get("page_to")
            start = int(pf)
            end = int(pt) if pt is not None else start
            if end < start:
                start, end = end, start
            visual_page_set.update(range(start, end + 1))
        empty_pages = sum(
            1
            for page, chars in coverage.items()
            if chars < thr.empty_page_char_threshold and page not in visual_page_set
        )
        expected = set(range(page_min, page_max + 1))
        missing = sorted(expected - set(coverage))
        continuity_ok = not missing

    empty_rate = (empty_pages / len(coverage)) if coverage else 0.0

    # 真丢页：以**源文件页数**为分母（而不是"块覆盖范围"），尾部空白/未解析页才看得见
    lost_pages: list[int] = []
    lost_rate: float | None = None
    if pages_applicable and source_page_count and int(source_page_count) > 0:
        total = int(source_page_count)
        have = set(coverage)
        lost_pages = [p for p in range(1, total + 1) if p not in have]
        lost_rate = len(lost_pages) / total

    table_rows = [c for c in chunks if str(c.get("chunk_type") or "") == "table"]
    table_nonempty = sum(1 for c in table_rows if str(c.get("content") or "").strip())
    table_count = len(table_rows)
    table_rate: float | None
    if table_count == 0:
        table_rate = None
    else:
        table_rate = table_nonempty / table_count

    report = ParseQuality(
        doc_id=doc_id,
        title=title,
        status=status,
        total_chars=total,
        replacement_chars=bad,
        garbled_rate=rate,
        pages_applicable=pages_applicable,
        visual_pages=len(visual_page_set & set(coverage)),
        source_page_count=source_page_count,
        lost_pages=lost_pages,
        lost_page_rate=lost_rate,
        page_count=len(coverage),
        empty_pages=empty_pages,
        empty_page_rate=empty_rate,
        page_min=page_min,
        page_max=page_max,
        missing_pages=missing,
        page_continuity_ok=continuity_ok,
        table_count=table_count,
        table_nonempty=table_nonempty,
        table_survival_rate=table_rate,
    )

    garbled_ok = rate <= thr.garbled_rate_max
    empty_ok: bool | None
    if slide_based or not pages_applicable:
        # 幻灯片类（.ppt/.pptx）**不做空页率准出**：幻灯片以图/标题为主，
        # 拿"页面正文长度"这条为页面型文档设计的准出线去卡它是**指标误用**
        # （实测 doc 33 的章节过渡页反复触发假警报）。
        # 记 N/A 而不是 True —— 不假装它通过了，报告里写清为什么。
        empty_ok = None
    else:
        empty_ok = empty_rate <= thr.empty_page_rate_max
    table_ok: bool | None
    if table_rate is None:
        table_ok = None
    else:
        table_ok = table_rate >= thr.table_survival_min

    report.checks = {
        "garbled_ok": garbled_ok,
        "empty_page_ok": empty_ok,
        "table_survival_ok": table_ok,
        "page_continuity_ok": continuity_ok if pages_applicable else None,
    }
    if not continuity_ok and pages_applicable:
        report.warnings.append(
            f"页码不连续：缺失 {missing[:20]}{'…' if len(missing) > 20 else ''}（WARN，不阻断）"
        )
    if slide_based:
        report.warnings.append(
            "幻灯片类文档（.ppt/.pptx）：空页率**不作准出**（该指标为页面型文档设计）；"
            "请改看零产出页与图表抽取情况"
        )
    if empty_ok is False:
        report.warnings.append(
            f"空页率 {empty_rate:.2%} > {thr.empty_page_rate_max:.0%}"
            f"（空页 {empty_pages}/{len(coverage)}；已排除只有图/表的页 {len(visual_page_set & set(coverage))} 页）"
        )
    if lost_pages:
        report.warnings.append(
            f"零产出页 {len(lost_pages)}/{source_page_count}：{lost_pages[:20]}"
            " —— 这些页**一个块都没有**，可能是源文件空白页，也可能是解析丢失，需对源文件核"
            "（WARN，不阻断）"
        )
    if not garbled_ok:
        report.warnings.append(
            f"乱码率 {rate:.2%} > {thr.garbled_rate_max:.1%}（U+FFFD {bad}/{total}）"
        )
    if table_ok is False:
        report.warnings.append(
            f"表格存活率 {table_rate:.2%} < {thr.table_survival_min:.0%}（非空 {table_nonempty}/{table_count}）"
        )
    return report


def _has_children(chunk_id: int, children_of: Mapping[int, list[int]]) -> bool:
    return bool(children_of.get(chunk_id))


def assess_chunk_stats(
    *,
    doc_id: int,
    title: str,
    chunks: Sequence[Mapping[str, Any]],
) -> ChunkStats:
    """分片内在统计：长度分布、同文双索引、heading/figure 异常。"""
    by_id = {int(c["chunk_id"]): c for c in chunks if c.get("chunk_id") is not None}
    children_of: dict[int, list[int]] = {}
    for c in chunks:
        parent_id = c.get("parent_id")
        if parent_id is None:
            continue
        children_of.setdefault(int(parent_id), []).append(int(c["chunk_id"]))

    by_type: dict[str, int] = {}
    for c in chunks:
        t = str(c.get("chunk_type") or "")
        by_type[t] = by_type.get(t, 0) + 1

    leaves: list[Mapping[str, Any]] = []
    containers = 0
    for cid, c in by_id.items():
        if _has_children(cid, children_of):
            containers += 1
        else:
            # 与 RETRIEVABLE_TYPES 对齐：summary 等非检索类型不计入叶子观察
            if str(c.get("chunk_type") or "") in {"parent", "child", "table", "figure"}:
                leaves.append(c)

    leaf_tokens = [int(c.get("token_count") or 0) for c in leaves]
    leaf_short = sum(1 for t in leaf_tokens if t < LEAF_SHORT_TOKENS)
    leaf_long = sum(1 for t in leaf_tokens if t > LEAF_LONG_TOKENS)
    n_leaves = len(leaves)

    # 同文双索引：父块恰有 1 个 child，且正文 strip 后相同
    dual = 0
    parents_with_kids = 0
    for parent_id, kids in children_of.items():
        parent = by_id.get(parent_id)
        if parent is None or str(parent.get("chunk_type") or "") != "parent":
            continue
        child_nodes = [by_id[k] for k in kids if k in by_id and str(by_id[k].get("chunk_type")) == "child"]
        if not child_nodes:
            continue
        parents_with_kids += 1
        if len(child_nodes) == 1:
            ptxt = str(parent.get("content") or "").strip()
            ctxt = str(child_nodes[0].get("content") or "").strip()
            if ptxt and ptxt == ctxt:
                dual += 1

    empty_heading = sum(
        1 for c in leaves if not str(c.get("heading_path") or "").strip()
    )
    figures = [c for c in leaves if str(c.get("chunk_type") or "") == "figure"]
    figure_short = sum(
        1 for c in figures if len(str(c.get("content") or "").strip()) < FIGURE_TEXT_SHORT_CHARS
    )

    sorted_tokens = sorted(leaf_tokens)
    return ChunkStats(
        doc_id=doc_id,
        title=title,
        total_chunks=len(chunks),
        leaf_chunks=n_leaves,
        parent_containers=containers,
        by_type=by_type,
        leaf_short=leaf_short,
        leaf_long=leaf_long,
        leaf_short_rate=(leaf_short / n_leaves) if n_leaves else 0.0,
        leaf_long_rate=(leaf_long / n_leaves) if n_leaves else 0.0,
        dual_index_pairs=dual,
        dual_index_rate=(dual / parents_with_kids) if parents_with_kids else 0.0,
        empty_heading_leaves=empty_heading,
        empty_heading_rate=(empty_heading / n_leaves) if n_leaves else 0.0,
        figure_short_text=figure_short,
        figure_count=len(figures),
        figure_short_rate=(figure_short / len(figures)) if figures else 0.0,
        token_p50=_percentile(sorted_tokens, 0.50),
        token_p90=_percentile(sorted_tokens, 0.90),
    )


def assess_document(
    *,
    doc_id: int,
    title: str,
    status: str,
    chunks: Sequence[Mapping[str, Any]],
    source_page_count: int | None = None,
    slide_based: bool = False,
    thresholds: Thresholds | None = None,
) -> DocQualityReport:
    return DocQualityReport(
        parse=assess_parse_quality(
            doc_id=doc_id,
            title=title,
            status=status,
            chunks=chunks,
            source_page_count=source_page_count,
            slide_based=slide_based,
            thresholds=thresholds,
        ),
        chunks=assess_chunk_stats(doc_id=doc_id, title=title, chunks=chunks),
    )


def assess_batch(
    docs: Iterable[tuple[Mapping[str, Any], Sequence[Mapping[str, Any]]]],
    thresholds: Thresholds | None = None,
) -> BatchQualityReport:
    """``docs`` 每项为 ``(doc_row, chunk_rows)``。"""
    thr = thresholds or Thresholds()
    report = BatchQualityReport(
        thresholds={
            "empty_page_rate_max": thr.empty_page_rate_max,
            "garbled_rate_max": thr.garbled_rate_max,
            "table_survival_min": thr.table_survival_min,
        }
    )
    for doc, chunks in docs:
        item = assess_document(
            doc_id=int(doc["doc_id"]),
            title=str(doc.get("title") or ""),
            status=str(doc.get("status") or ""),
            chunks=chunks,
            # 源文件页数（`ekbs_document.page_count`）→ 真丢页率的分母
            source_page_count=(
                int(doc["page_count"]) if doc.get("page_count") is not None else None
            ),
            # 幻灯片类文档由**源文件扩展名**判定（`source_path` 由调用方放进 doc 行）
            slide_based=str(doc.get("source_path") or "").lower().endswith((".ppt", ".pptx")),
            thresholds=thr,
        )
        report.docs.append(item)
        if not item.parse.ok:
            report.failed_doc_ids.append(item.parse.doc_id)
    return report
