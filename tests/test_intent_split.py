"""多意图拆分（P0）单元测试：规则拆分 / 兜底 / 关闭 / 上限。"""

from __future__ import annotations

from ekbs.retrieval.intent import IntentSplitter


def _splitter(settings, *, enabled: bool = True, max_questions: int = 5) -> IntentSplitter:
    cfg = settings.retrieval.intent_split
    cfg.enabled = enabled
    cfg.strategy = "rules"
    cfg.max_questions = max_questions
    return IntentSplitter(settings)


def test_single_question_is_not_split(settings) -> None:
    parts = _splitter(settings).split("年假有多少天")
    assert len(parts) == 1
    assert parts[0].text == "年假有多少天"
    assert parts[0].source == "single"


def test_disabled_returns_single(settings) -> None:
    parts = _splitter(settings, enabled=False).split("年假多少天？病假怎么申请？")
    assert len(parts) == 1
    assert parts[0].source == "single"


def test_question_marks_split(settings) -> None:
    parts = _splitter(settings).split("年假多少天？年终奖怎么算？病假怎么申请？")
    assert [p.text for p in parts] == ["年假多少天", "年终奖怎么算", "病假怎么申请"]
    assert all(p.source == "rules" for p in parts)


def test_numbered_list_split(settings) -> None:
    parts = _splitter(settings).split("1. 年假多少天 2. 年终奖怎么算")
    assert [p.text for p in parts] == ["年假多少天", "年终奖怎么算"]


def test_chinese_numbered_split(settings) -> None:
    parts = _splitter(settings).split("一、年假多少天 二、病假怎么申请")
    assert len(parts) == 2


def test_conjunction_split_requires_long_parts(settings) -> None:
    long_parts = _splitter(settings).split("年假申请流程是什么以及病假怎么申请")
    assert len(long_parts) == 2

    short_parts = _splitter(settings).split("甲以及乙")
    assert len(short_parts) == 1


def test_respectively_is_多少_not_split(settings) -> None:
    """「预算、节约电费和回收期分别是多少」必须整句保留，不能拆出孤儿「是多少？」。"""
    q = "熙街中央空调IOT节能改造的预算、预计年节约电费和投资回收期分别是多少？"
    parts = _splitter(settings).split(q)
    assert len(parts) == 1
    assert parts[0].text == q
    assert parts[0].source == "single"

    assert len(_splitter(settings).split("改造预算和工期分别是多少")) == 1
    assert len(_splitter(settings).split("电费、水费分别是什么")) == 1


def test_max_questions_cap(settings) -> None:
    parts = _splitter(settings, max_questions=2).split("年假？病假？产假？婚假？")
    assert len(parts) == 2


def test_empty_question(settings) -> None:
    assert _splitter(settings).split("   ") == []
