"""分片命名与归属判定（`00` §3.1 命名规范、`06` §2.2/§2.5、`docs/spec/20` §4）。

命名契约：``kb_<dept_code>_L<level>``，``dept_code`` 为 ``[a-z0-9_]{2,16}``（**不用中文**），
公共分片用 ``all``。

归属判定（向量只存一份，其余可见部门靠 shard 级 ACL 授权 —— `06` §2.2「归属主部门」）：

| 情况 | 结果 |
|---|---|
| ``level == 4`` 且 ``permission.l4_enabled == false`` | 拒绝入库（`00` D11：首期 L4 不入库） |
| ``visible_dept_ids`` 为空 且 ``level == 1`` | ``kb_all_L1`` |
| ``visible_dept_ids`` 为空 且 ``level > 1`` | 配置错误（L2/L3 必须指定部门，`00` §3.2） |
| ``visible_dept_ids`` 非空 | 取第一个部门得到 ``dept_code`` → ``kb_<dept_code>_L<level>``；其余部门写入 shard 级 ACL allow |

⚠️ ``dept_code`` 与部门 ID 的映射是开放项 O6-1 / T3：本实现用
``permission.dept_code_map`` 承载（不新增 Schema），定稿后应改到权威位置。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ekbs.config import Settings
from ekbs.errors import ConfigError

#: 分片 ID 命名（`00` §3.1）
SHARD_ID_RE = re.compile(r"^kb_([a-z0-9_]{2,16})_L([1-4])$")
DEPT_CODE_RE = re.compile(r"^[a-z0-9_]{2,16}$")

#: 公共分片代号（L1 全员可见）
PUBLIC_DEPT_CODE = "all"


def validate_dept_code(dept_code: str) -> str:
    if not DEPT_CODE_RE.match(dept_code):
        raise ConfigError(
            f"dept_code 非法：{dept_code!r}（必须是 [a-z0-9_]{{2,16}}，公共分片用 'all'，不用中文）"
        )
    return dept_code


def make_shard_id(dept_code: str, level: int) -> str:
    validate_dept_code(dept_code)
    if not 1 <= int(level) <= 4:
        raise ConfigError(f"密级越界：{level}（必须是 1..4）")
    return f"kb_{dept_code}_L{int(level)}"


def parse_shard_id(shard_id: str) -> tuple[str, int]:
    """``kb_hr_L2`` → ``("hr", 2)``。"""
    match = SHARD_ID_RE.match(shard_id)
    if not match:
        raise ConfigError(f"分片 ID 不合规：{shard_id!r}（应形如 kb_hr_L2，见 00 §3.1）")
    return match.group(1), int(match.group(2))


def dept_code_from_ref(dept_ref: str, settings: Settings) -> str:
    """把「文档上的部门标识」转成分片用的 ``dept_code``。

    **顺序很重要**：``ekbs_document.visible_dept_ids`` 承载的是**部门标识**
    （钉钉部门 ID），与 ``dept_code``（``hr``/``finance``）是两个命名空间，
    因此**先查映射**，查不到才回退到「字面量本身就是合法短码」。
    反过来先认字面量会让纯数字的部门 ID（如 ``102`` 恰好符合 ``[a-z0-9_]{2,16}``）
    被误当成 dept_code，产出 ``kb_102_L2`` 这类与 `00` §3.1 命名示例不符的分片，
    并且与运行时路由（走映射）不一致。

    两者都不匹配 → 抛错并指向 O6-1，**不猜**。
    """
    ref = str(dept_ref).strip()
    for code, dept_ids in (settings.permission.dept_code_map or {}).items():
        if ref in {str(d) for d in dept_ids}:
            return validate_dept_code(code)
    if DEPT_CODE_RE.match(ref):
        return ref
    raise ConfigError(
        f"无法把部门 {ref!r} 映射为 dept_code：dept_code_map 中未找到，且它本身不是合法短码。"
        "该映射的权威存放位置是开放项 O6-1（06 §7.3），定稿前请在 "
        "permission.dept_code_map 中补上。"
    )


@dataclass(frozen=True)
class ShardPlacement:
    """入库时的分片归属结果。"""

    #: 向量写入的分片（主分片 = 第一个可见部门）
    primary_shard_id: str
    #: 需要写入 shard 级 ACL allow 的「其余可见部门 → 该分片」授权
    extra_grants: tuple[tuple[str, str], ...] = ()


def resolve_shard_for_document(
    *, level: int, visible_dept_ids: list[str] | tuple[str, ...], settings: Settings
) -> ShardPlacement:
    """`docs/spec/20` §4 的归属判定（入库路径唯一入口）。"""
    level = int(level)
    if level == 4 and not settings.permission.l4_enabled:
        raise ConfigError(
            "L4 文档入库被拒：permission.l4_enabled=false（00 D11 —— 首期 L4 不入库；"
            "如需入库须走独立分片 + 白名单）"
        )
    depts = [str(d) for d in visible_dept_ids if str(d).strip()]
    from ekbs.personal import sole_personal_owner

    owner = sole_personal_owner(depts)
    if owner is not None:
        # 私有库不进部门分片。p<用户ID> 只靠 shard ACL 授给本人。
        return ShardPlacement(primary_shard_id=make_shard_id(f"p{owner}", level))
    if not depts:
        if level == 1:
            return ShardPlacement(primary_shard_id=make_shard_id(PUBLIC_DEPT_CODE, 1))
        raise ConfigError(
            f"L{level} 文档未指定 visible_dept_ids：L2/L3 必须指定部门（00 §3.2）；"
            "若确为「内部公开」，请显式给出部门或改用 L1 + kb_all_L1。"
        )
    dept_codes = [dept_code_from_ref(d, settings) for d in depts]
    primary = make_shard_id(dept_codes[0], level)
    extras = tuple((dept, primary) for dept in depts[1:])
    return ShardPlacement(primary_shard_id=primary, extra_grants=extras)
