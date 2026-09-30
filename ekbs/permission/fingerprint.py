"""权限指纹 —— **唯一权威定义见 `00` §3.4**。

契约（顺序、分隔符、是否排序、类型，四项都不可改）：

.. code-block:: text

    raw = "|".join([
        str(user_id),
        ",".join(sorted(visible_shard_ids)),
        str(clearance),                 # 整数 1..4 → "1".."4"；"L2"/"confidential" 均违约
        ",".join(sorted(dept_chain)),
        str(policy_version),
    ])
    fingerprint = sha256(raw.encode("utf-8")).hexdigest()

为什么用 ``visible_shard_ids`` 而不是 ``kb_ids``：检索实际加载的是**分片**，用分片集合可让
「指纹相同 ⇒ 可见分片集合相同」成为恒等式（`00` §3.4、`06` §2.6）。

**全仓库只允许本文件实现一次**，其余位置一律 import（`03` §4.3 与 `04` §4.6 是同一函数的落地）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence


def normalize_clearance(clearance: int | str) -> int:
    """把密级规整为整数序号 1..4。

    只接受整数或 ``"1".."4"``；``"L2"`` / ``"internal"`` 这类写法一律报错 ——
    它们会让同一身份算出两个不同指纹（`00` §3.4 注）。
    """
    if isinstance(clearance, bool):  # bool 是 int 的子类，单独拦掉
        raise TypeError("clearance 必须是整数密级序号 1..4，不接受布尔值")
    if isinstance(clearance, int):
        level = clearance
    elif isinstance(clearance, str) and clearance.strip().isdigit():
        level = int(clearance.strip())
    else:
        raise ValueError(f"clearance 非法：{clearance!r}（必须是整数 1..4；禁止 'L2'/密级名）")
    if not 1 <= level <= 4:
        raise ValueError(f"clearance 越界：{level}（必须是 1..4）")
    return level


def perm_fingerprint(
    user_id: int,
    visible_shard_ids: Sequence[str] | Iterable[str],
    clearance: int | str,
    dept_chain: Sequence[str] | Iterable[str],
    policy_version: int,
) -> str:
    """`00` §3.4 的逐字实现。"""
    raw = "|".join(
        [
            str(user_id),
            ",".join(sorted(visible_shard_ids)),
            str(normalize_clearance(clearance)),
            ",".join(sorted(dept_chain)),
            str(policy_version),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
