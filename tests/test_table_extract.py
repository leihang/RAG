"""精排块确定性提取 + 数字计算意图门闸。"""

from __future__ import annotations

from types import SimpleNamespace

from ekbs.retrieval.structured_query import (
    is_document_stated_value_intent,
    is_numeric_calc_intent,
    is_structured_query_intent,
    wants_document_prose,
)
from ekbs.retrieval.table_extract import (
    extract_from_table_chunks,
    module_diff,
    module_sum,
)


def test_is_numeric_calc_intent_covers_detail_and_period_metric() -> None:
    assert is_numeric_calc_intent("2026年9月公共用电量明细")
    assert is_numeric_calc_intent("熙丰2026年2月差异电费")
    assert is_numeric_calc_intent("2025年1-3月锅炉燃气单价平均值")
    assert not is_numeric_calc_intent("年假怎么请")
    assert not is_numeric_calc_intent("交接班要注意什么")


def test_ppt_prose_basis_allows_numeric_prose_fallback() -> None:
    """PPT 分析页正文已写明同比结论时，旁路失败应回退散文，不能硬拒。"""
    from ekbs.retrieval.pipeline import RetrievalPipeline, RetrievalResult, RetrievedChunk

    prose = RetrievedChunk(
        chunk_id=1,
        doc_id=238,
        shard_id="kb_all_L1",
        content=(
            "第7页\n1. 能耗管理1-9月电费（工程部）\n"
            "总用电费累计:2026比2025+0.08%差异小（原因为商户住宅 +1.93%，公司承担 -19.47%）"
        ),
        chunk_type="parent",
        doc_title="工程部/2026年10月工作汇报-工程",
        page_from=7,
        page_to=7,
        source_path="/data/汇报.pptx",
    )
    placeholder = RetrievedChunk(
        chunk_id=2,
        doc_id=238,
        shard_id="kb_all_L1",
        content="第6页\n1. 能耗管理1-9月电费\n\n[表格 3]\n\n[表格 4]\n\n（备注）三年数据对比",
        chunk_type="parent",
        doc_title="工程部/2026年10月工作汇报-工程",
        page_from=6,
        page_to=6,
        source_path="/data/汇报.pptx",
    )
    table = RetrievedChunk(
        chunk_id=3,
        doc_id=238,
        shard_id="kb_all_L1",
        content="类别=公司承担用电/费; 2025年=287340; 2026年=244532",
        chunk_type="table",
        doc_title="工程部/2026年10月工作汇报-工程",
        page_from=6,
        page_to=6,
        source_path="/data/汇报.pptx",
    )
    assert RetrievalPipeline._chunk_has_substantive_prose(prose)
    assert not RetrievalPipeline._chunk_has_substantive_prose(placeholder)
    assert not RetrievalPipeline._chunk_has_substantive_prose(table)
    with_prose = RetrievalResult(query="q", chunks=[placeholder, prose, table])
    tables_only = RetrievalResult(query="q", chunks=[placeholder, table])
    assert RetrievalPipeline._retrieval_has_prose_basis(with_prose)
    assert not RetrievalPipeline._retrieval_has_prose_basis(tables_only)


def test_structured_bypass_only_for_spreadsheet_sources() -> None:
    """旁路唯一规则：命中电子表格才尝试；PPT 内嵌表不算。"""
    from ekbs.retrieval.numeric import MODE_AUTO, MODE_NUMERIC, MODE_PROSE
    from ekbs.retrieval.pipeline import RetrievalPipeline, RetrievalResult, RetrievedChunk

    q = "2026年1-9月公司承担电费与2025年同期相比变化如何？"
    assert is_numeric_calc_intent(q)

    ppt = RetrievedChunk(
        chunk_id=1,
        doc_id=238,
        shard_id="s",
        content="公司承担 -19.47%",
        chunk_type="parent",
        source_path="/raw/工程汇报.pptx",
    )
    xlsx = RetrievedChunk(
        chunk_id=2,
        doc_id=235,
        shard_id="s",
        content="类别=公司承担用电/费; 2026年=244532",
        chunk_type="table",
        source_path="/raw/能耗分析.xlsx",
    )
    pipe = RetrievalPipeline.__new__(RetrievalPipeline)
    ppt_ret = RetrievalResult(query=q, chunks=[ppt])
    xlsx_ret = RetrievalResult(query=q, chunks=[xlsx])
    assert not RetrievalPipeline._retrieval_has_spreadsheet(ppt_ret)
    assert RetrievalPipeline._retrieval_has_spreadsheet(xlsx_ret)
    assert not pipe._should_try_structured_plan(q, ppt_ret, MODE_AUTO)
    assert not pipe._should_try_structured_plan(q, ppt_ret, MODE_NUMERIC)
    assert pipe._should_try_structured_plan(q, xlsx_ret, MODE_PROSE)
    assert pipe._should_try_structured_plan(q, xlsx_ret, MODE_NUMERIC)


def test_entity_from_how_many_transformers() -> None:
    """「一共有多少台变压器」必须抽出设备名，否则会 COUNT(*) 全库行。"""
    from ekbs.retrieval.structured_query import _entity_from_question

    assert _entity_from_question("一共有多少台变压器") == "变压器"
    assert _entity_from_question("变压器一共有多少台") == "变压器"
    assert _entity_from_question("有几台变压器？") == "变压器"
    assert _entity_from_question("一共有多少台") is None


def test_location_entity_variants_match_switchroom_aliases() -> None:
    from ekbs.retrieval.structured_query import (
        _entity_from_question,
        entity_match_variants,
    )

    variants = entity_match_variants("1期4号配电房")
    assert "一期4#专配室" in variants
    assert "1期4号配电房" in variants
    assert _entity_from_question("二期15#专配室2026年9月变压器损耗合计") == "二期15#专配室"
    assert _entity_from_question("1期4号配电房2026年9月变压器损耗是多少") == "1期4号配电房"


def test_extract_switchroom_transformer_loss_ignores_daily_loss_column() -> None:
    """问损耗时不得把理论表「日损耗」当成实测「损耗」。"""
    chunk = SimpleNamespace(
        chunk_id=1,
        doc_id=10,
        content=(
            "相关表头: 序号 | 安装地点 | 日损耗 | 每月损耗 | 损耗\n"
            "序号=1; 安装地点=一期4#专配室; 日损耗=193.8; 每月损耗=5814; 损耗=-2569"
        ),
    )
    hit = extract_from_table_chunks(
        [chunk],
        entity="1期4号配电房",
        period="2026-09",
        metric="变压器损耗",
        columns=["安装地点", "损耗"],
        prefer_doc_ids=[10],
    )
    assert not hit.is_na
    assert float(hit.value) == -2569.0


def test_extract_strips_trailing_value_suffix_on_entity() -> None:
    """问「变压器损耗值」应对上表内「项目=变压器损耗」。"""
    from ekbs.retrieval.structured_query import _entity_from_question

    chunk = SimpleNamespace(
        chunk_id=1,
        doc_id=10,
        content=(
            "相关表头: 类别 | 项目 | 9用量（kw.h)\n"
            "类别=公共; 项目=变压器损耗; 9用量（kw.h)=91064.80"
        ),
    )
    assert _entity_from_question("变压器损耗值2026年9月是多少") == "变压器损耗"
    hit = extract_from_table_chunks(
        [chunk],
        entity="变压器损耗值",
        period="2026-09",
        columns=["项目", "用量"],
        prefer_doc_ids=[10],
    )
    assert not hit.is_na
    assert float(hit.value) == 91064.8


def test_extract_fallback_ignores_wrong_plan_metric_column() -> None:
    """规划把「每月损耗」当 metric 时，仍应回退抽到用电分析 9用量。"""
    from ekbs.retrieval.structured_query import _extract_fallback_from_chunks

    chunk = SimpleNamespace(
        chunk_id=1,
        doc_id=10,
        content=(
            "相关表头: 类别 | 项目 | 9用量（kw.h)\n"
            "类别=公共; 项目=变压器损耗; 9用量（kw.h)=91064.80"
        ),
    )
    hit = _extract_fallback_from_chunks(
        [chunk],
        entity="变压器损耗",
        period=None,
        metric="每月损耗",
        attrs_contains="变压器损耗",
        columns=["安装地点", "每月损耗"],
        prefer_doc_ids=[10],
        question="变压器损耗值2026年9月是多少",
    )
    assert not hit.is_na
    assert float(hit.value) == 91064.8


def test_renovation_plan_stated_values_use_prose_not_sum() -> None:
    """Word/方案类：散文 LLM（父块+表叶齐全）；禁止当抄表 SUM；不进旁路 structured。"""
    from ekbs.retrieval.structured_query import stated_value_extract_columns

    prose_qs = [
        "2026年中央空调节能改造，改造方案、成本、节约费用",
        "2026年中央空调节能改造的改造方案、成本和节约费用是多少",
        "熙街中央空调IOT节能改造的预算、预计年节约电费和投资回收期分别是多少？",
        "中央空调IOT节能改造方案改造成本是多少",
    ]
    for q in prose_qs:
        assert is_document_stated_value_intent(q), q
        assert not is_numeric_calc_intent(q), q
        assert not is_structured_query_intent(q), q
    assert wants_document_prose(prose_qs[0])
    assert stated_value_extract_columns(prose_qs[2]) == [
        "改造预算",
        "预计年节约电费",
        "投资回收期",
    ]
    # 主机节约金额：不靠意图旁路，靠父块补表叶后由散文 LLM 读表
    assert not is_numeric_calc_intent("空调主机改造后预计节约多少钱")
    assert not is_structured_query_intent("空调主机改造后预计节约多少钱")
    # 年度预算分月单元格：散文读表，禁止当观测 SUM 后拒答
    budget_q = "2026年预算工程费用2月预算额度为多少"
    assert is_document_stated_value_intent(budget_q)
    assert not is_numeric_calc_intent(budget_q)
    assert not is_structured_query_intent(budget_q)
    # 真正的用量合计仍走计算
    assert is_numeric_calc_intent("2026年9月公共用电量合计")
    assert is_structured_query_intent("2026年9月公共用电量合计")


def test_extract_scheme_kv_with_yuan_units() -> None:
    """序列化方案表：``改造预算=22,000 元`` 必须被计算模块抽出，不能因单位丢弃。"""
    content = (
        "表名: 表 1\n"
        "相关表头: 改造预算 | 预计年节约电费 | 投资回收期 | 计划工期\n"
        "改造预算=22,000 元; 预计年节约电费=约 40,848 元; "
        "投资回收期=约 7 个月; 计划工期=30 天\n"
    )
    chunk = SimpleNamespace(chunk_id=1, doc_id=236, content=content, chunk_type="table")
    hit = extract_from_table_chunks(
        [chunk],
        columns=["改造预算", "预计年节约电费", "投资回收期", "计划工期"],
        prefer_doc_ids=[236],
    )
    assert not hit.is_na
    assert hit.detail
    row = hit.detail[0]
    assert row.get("改造预算") == 22000
    assert row.get("预计年节约电费") == 40848
    assert row.get("投资回收期") == 7
    assert row.get("计划工期") == 30
    assert "改造预算=22000" in str(hit.surface)
    assert "预计年节约电费=40848" in str(hit.surface)


def test_extract_scheme_kv_ignores_unrelated_project_rows() -> None:
    content = (
        "改造预算=22,000 元; 预计年节约电费=约 40,848 元; 投资回收期=约 7 个月\n"
        "项目=2025 年用电量（度）; 公共风柜=464,500; 合计=1,283,960\n"
    )
    chunk = SimpleNamespace(chunk_id=1, doc_id=236, content=content, chunk_type="table")
    hit = extract_from_table_chunks(
        [chunk],
        columns=["改造预算", "预计年节约电费", "投资回收期"],
        prefer_doc_ids=[236],
    )
    assert len(hit.detail) == 1
    assert hit.detail[0].get("改造预算") == 22000
    assert "用电量" not in str(hit.surface)


def test_extract_rejects_short_column_substring_false_positive() -> None:
    """「冷却塔节约比例」不得命中宽表列「冷却塔=47,940」（用电量）。"""
    content = (
        "项目=2025 年用电量（度）; 公共风柜=464,500; 空调主机=771,520; "
        "冷却塔=47,940; 合计=1,283,960\n"
        "改造预算=22,000 元; 预计年节约电费=约 40,848 元\n"
    )
    chunk = SimpleNamespace(chunk_id=1, doc_id=236, content=content, chunk_type="table")
    bad = extract_from_table_chunks(
        [chunk], columns=["冷却塔节约比例"], prefer_doc_ids=[236]
    )
    assert bad.is_na
    ok = extract_from_table_chunks(
        [chunk], columns=["改造预算"], prefer_doc_ids=[236]
    )
    assert ok.detail[0].get("改造预算") == 22000


def test_extract_from_kv_rows_filters_public_category() -> None:
    content = (
        "表名: 用电分析\n"
        "列1=成本; 类别=公共; 项目=活动用电; 9用量（kw.h)=1343.00; 金额=1030.51; "
        "用量同比=229.17%\n"
        "列1=成本; 类别=公共; 项目=光彩照明; 9用量（kw.h)=17357.00; 金额=13318.43\n"
        "列1=成本; 类别=经营; 项目=商铺A; 9用量（kw.h)=999.00; 金额=1\n"
    )
    chunk = SimpleNamespace(chunk_id=1, doc_id=235, content=content, chunk_type="table")
    hit = extract_from_table_chunks(
        [chunk],
        entity="公共",
        period="2026-09",
        metric="用电量",
        attrs_contains="公共",
        columns=["项目", "用量"],
        prefer_doc_ids=[235],
    )
    assert not hit.is_na
    assert hit.rows_touched >= 2
    names = {str(r.get("项目")) for r in hit.detail}
    assert "活动用电" in names
    assert "光彩照明" in names
    assert "商铺A" not in names
    by_name = {str(r.get("项目")): r for r in hit.detail}
    assert by_name["活动用电"]["用量"] == 1343 or by_name["活动用电"]["用量"] == 1343.0
    # 未请求同比列
    assert all("同比" not in r for r in hit.detail)


def test_extract_respects_doc_scope() -> None:
    chunk = SimpleNamespace(
        chunk_id=2,
        doc_id=99,
        content="类别=公共; 项目=活动用电; 9用量=1",
        chunk_type="table",
    )
    hit = extract_from_table_chunks(
        [chunk],
        attrs_contains="公共",
        columns=["项目", "用量"],
        prefer_doc_ids=[235],
    )
    assert hit.is_na
    assert hit.reason == "not_found"


def test_module_sum_and_diff_ratio() -> None:
    total, surface = module_sum([1.5, 2.5])
    assert total == 4.0
    assert "4" in surface
    ratio, text = module_diff(120.0, 100.0, as_ratio=True)
    assert abs(ratio - 20.0) < 1e-9
    assert text.endswith("%")
