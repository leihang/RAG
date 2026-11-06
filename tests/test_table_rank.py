"""表格比较/排名：意图门闩、tidy 解析、受限 max 稳定性。"""

from __future__ import annotations

from types import SimpleNamespace

from ekbs.retrieval.table_rank import (
    detect_compare_op,
    extract_year_month_from_question,
    is_compare_rank_intent,
    parse_tidy_metric_from_content,
    peer_keys_from_chunks,
    try_constrained_rank,
)


def _tidy_leaf(
    *,
    entity: str,
    year: str,
    month: int,
    value: str,
    chunk_id: int,
    category: str = "娱乐",
) -> SimpleNamespace:
    content = (
        "表名: 大户用电监视\n"
        "核心实体: 大户用电监视\n"
        "相关表头: 铺号 | 用户名 | 类别 | 年度 | 月份 | 用电量\n"
        f"铺号=X; 用户名={entity}; 类别={category}; 年度={year}; "
        f"月份={month}; 用电量={value}"
    )
    return SimpleNamespace(
        chunk_id=chunk_id,
        doc_id=231,
        chunk_type="table",
        content=content,
    )


def test_compare_intent_gate_and_op() -> None:
    assert is_compare_rank_intent("大户用电分析，9月用电量谁更高")
    assert detect_compare_op("大户用电分析，9月用电量谁更高") == "max"
    assert detect_compare_op("用电量最低是谁") == "min"
    assert detect_compare_op("9月用电量排名") == "rank"
    assert not is_compare_rank_intent("2026年9月用电量是多少")
    assert detect_compare_op("2026年9月用电量是多少") is None


def test_extract_year_month_from_question() -> None:
    assert extract_year_month_from_question("大户用电分析，9月用电量谁更高") == (None, 9)
    assert extract_year_month_from_question("2026年9月谁最高") == ("2026", 9)


def test_parse_tidy_metric_skips_rank_summary() -> None:
    leaf = parse_tidy_metric_from_content(
        _tidy_leaf(entity="海豚电竞", year="2026", month=9, value="131120", chunk_id=1).content,
        chunk_id=1,
    )
    assert leaf is not None
    assert leaf.entity == "海豚电竞"
    assert leaf.value == 131120.0
    summary = (
        "表名: 大户用电监视\n相关表头: 排名摘要\n"
        "排名类型=Top10; 年度=2026; 月份=9; 指标=用电量\n1. 海豚电竞=131120"
    )
    assert parse_tidy_metric_from_content(summary) is None


def test_constrained_max_picks_dolphin() -> None:
    chunks = [
        _tidy_leaf(entity="保利影城", year="2026", month=9, value="90000", chunk_id=10),
        _tidy_leaf(entity="海豚电竞", year="2026", month=9, value="131120", chunk_id=11),
        _tidy_leaf(entity="金乐迪", year="2026", month=9, value="15520", chunk_id=12),
        _tidy_leaf(entity="海豚电竞", year="2026", month=8, value="133680", chunk_id=13),
        # 历史年峰值更大：未写年份时应取最新年 2026，而不是 2024
        _tidy_leaf(entity="海豚电竞", year="2024", month=9, value="171360", chunk_id=14),
    ]
    result = try_constrained_rank("大户用电分析，9月用电量谁更高", chunks)
    assert result is not None
    assert result.op == "max"
    assert result.winner_entity == "海豚电竞"
    assert result.winner_surface == "131120"
    assert result.year == "2026"
    assert result.month == 9
    assert 11 in result.cite_chunk_ids


def test_peer_keys_from_hit_and_question_month() -> None:
    chunks = [
        _tidy_leaf(entity="海豚电竞", year="2026", month=8, value="1", chunk_id=1),
    ]
    # 问题只写月：year=None，按月扩全量年
    keys = peer_keys_from_chunks(chunks, "9月用电量谁更高")
    assert keys == [(231, None, 9)]


def test_non_compare_does_not_rank() -> None:
    chunks = [
        _tidy_leaf(entity="海豚电竞", year="2026", month=9, value="131120", chunk_id=1),
    ]
    assert try_constrained_rank("海豚电竞9月用电量是多少", chunks) is None
