"""编号/日期类整词自动抽取（不依赖用户词典）。

背景：jieba 会把 `XJ-01-11-1F02` 切成 7 个碎片；`_analyze()` 在**写入与查询两侧对称**
抽出这类整词，等价于一个「零维护的自动词典」——不需要 `userdict.txt`。
"""

from __future__ import annotations

from ekbs.index.fts import to_match_query, tokenize, tokenize_for_fts


def test_code_is_whole_token_on_both_sides() -> None:
    """编号在写入侧是整词、查询侧也是整词（双端一致）。"""
    code = "XJ-01-11-1F02"
    assert code.lower() in tokenize_for_fts(f"点位 {code} 开关时间").split()
    assert f'"{code.lower()}"' in to_match_query(code)


def test_date_is_whole_token() -> None:
    """日期（`2026-11-03`）同样被抽成整词，避免被切成 `2026 / - / 11 / - / 03`。"""
    assert "2026-11-03" in tokenize_for_fts("生效日期 2026-11-03").split()
    assert '"2026-11-03"' in to_match_query("2026-11-03")


def test_plain_chinese_unaffected() -> None:
    """普通中文不受影响：没有编号/日期时，口径与纯 jieba 完全一致。"""
    assert tokenize_for_fts("能耗分析表") == " ".join(tokenize("能耗分析表"))


def test_content_and_query_share_the_same_code_set() -> None:
    """双端一致：写入侧抽到的整词，查询侧一定也抽到（否则「写进去搜不到」）。"""
    text = "点位 XJWY-GC-01-01 / 生效日期 2026-11-03"
    written = set(tokenize_for_fts(text).split())
    matched = {part.strip('"') for part in to_match_query(text).split(" OR ")}
    assert {"xjwy-gc-01-01", "2026-11-03"} <= written
    assert {"xjwy-gc-01-01", "2026-11-03"} <= matched
