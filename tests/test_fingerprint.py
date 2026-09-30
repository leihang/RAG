"""权限指纹契约（**逐字对齐 `00` §3.4**）。

这些用例是「契约测试」：任何一项失败都意味着缓存隔离假设被破坏，
会引起跨用户缓存污染 —— 属于红线，不是风格问题。
"""

from __future__ import annotations

import pytest

from ekbs.permission.fingerprint import normalize_clearance, perm_fingerprint

#: 用独立实现算出的期望值（`00` §3.4 的五项拼接 → sha256）
EXPECTED_DIGEST = "078a3b116a5781d9ce663f86a24e97ad784b4d4da81b88b8e46ae47353e49e33"


def test_exact_digest_matches_contract() -> None:
    assert (
        perm_fingerprint(7, ["kb_hr_L2", "kb_all_L1"], 2, ["102", "1"], 3) == EXPECTED_DIGEST
    )


def test_input_order_does_not_matter() -> None:
    """第 2/4 项内部排序是契约的一部分，因此输入顺序不影响结果。"""
    a = perm_fingerprint(7, ["kb_hr_L2", "kb_all_L1"], 2, ["102", "1"], 3)
    b = perm_fingerprint(7, ["kb_all_L1", "kb_hr_L2"], 2, ["1", "102"], 3)
    assert a == b == EXPECTED_DIGEST


def test_clearance_accepts_int_and_numeric_string() -> None:
    assert perm_fingerprint(7, ["a"], 2, [], 1) == perm_fingerprint(7, ["a"], "2", [], 1)


@pytest.mark.parametrize("bad", ["L2", "confidential", "", "  ", 0, 5, None, True])
def test_clearance_rejects_non_numeric_or_out_of_range(bad) -> None:
    """'L2' / 密级名 / 越界值都是违约：会让同一身份算出两个指纹。"""
    with pytest.raises((ValueError, TypeError)):
        perm_fingerprint(7, ["a"], bad, [], 1)


def test_every_field_changes_the_digest() -> None:
    base = perm_fingerprint(7, ["kb_a_L1"], 2, ["1"], 3)
    assert perm_fingerprint(8, ["kb_a_L1"], 2, ["1"], 3) != base  # user_id
    assert perm_fingerprint(7, ["kb_b_L1"], 2, ["1"], 3) != base  # 分片集合
    assert perm_fingerprint(7, ["kb_a_L1"], 3, ["1"], 3) != base  # clearance
    assert perm_fingerprint(7, ["kb_a_L1"], 2, ["2"], 3) != base  # dept_chain
    assert perm_fingerprint(7, ["kb_a_L1"], 2, ["1"], 4) != base  # policy_version


def test_delimiter_ambiguity_is_unreachable_for_valid_ids() -> None:
    """契约用 ``|`` 连接五项、``,`` 连接项内元素，**没有转义**。

    因此若某个 ``shard_id`` 或部门标识里含逗号，就可能与「两个元素」拼出同一串
    （下面第一段断言的就是这个事实）。这不是可以靠改代码绕过的 —— 它是 `00` §3.4
    的逐字契约。安全性来自**值域约束**：分片 ID 由 `00` §3.1 限定为
    ``kb_<dept_code>_L<level>``（``[a-z0-9_]``），部门标识是钉钉数字 ID，都不会含逗号。
    """
    ambiguous_two = perm_fingerprint(1, ["a", "b"], 1, [], 1)
    ambiguous_one = perm_fingerprint(1, ["a,b"], 1, [], 1)
    assert ambiguous_two == ambiguous_one  # 契约事实（无转义）

    from ekbs.index.shard import make_shard_id, parse_shard_id

    for shard_id in ("kb_all_L1", "kb_hr_L2", make_shard_id("finance_2", 3)):
        assert "," not in shard_id
        dept_code, level = parse_shard_id(shard_id)
        assert "," not in dept_code and 1 <= level <= 4


@pytest.mark.parametrize("bad_dept_code", ["hr,finance", "HR", "a", "部门", ""])
def test_dept_code_rejects_comma_and_illegal_chars(bad_dept_code: str) -> None:
    """值域约束是上面那条「歧义不可达」的保证，因此必须由代码守住。"""
    from ekbs.errors import ConfigError
    from ekbs.index.shard import validate_dept_code

    with pytest.raises(ConfigError):
        validate_dept_code(bad_dept_code)


def test_normalize_clearance_returns_int() -> None:
    assert normalize_clearance("3") == 3
    assert normalize_clearance(3) == 3
