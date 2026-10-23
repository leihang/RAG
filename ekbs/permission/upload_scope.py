"""文档上架范围校验（部门 × 密级）—— 「谁可以往哪个部门、哪个密级传文档」。

## 为什么需要它

评估发现的两个缺口（代码事实，非文档问题）：

1. **「管理范围」从未生效**：``ekbs_user_role`` 里有 ``scope_type/scope_id``，
   分配角色时也能填，但权限判定（``ekbs.authz``）**完全没读它** —— 于是任何有
   ``doc:document:create`` 权限点的人，都能给任何部门、任何知识库传文档；
2. **不校验上传者密级**：上传路径里一处密级判断都没有 —— 密级 1 的人也能传 L4。

两者合起来的风险是「写操作越界」：文档可能被传进不该管的部门、或把高密级内容
带进系统。（读的越界由检索侧 C1/D18/C5 兜着，这里管的是"谁能写"。）

## 规则（`super_admin` 例外，与 `00` §3.3 的「全部权限」定义一致）

| 规则 | 判定 |
|---|---|
| 密级 | ``文档 level ≤ 上传者 clearance`` |
| 部门 | ``visible_dept_ids`` 必须落在上传者的**可管理部门**内：自己部门 + 其所有下级部门 |
| 全公司公开 | ``visible_dept_ids`` 为空 = 全员可见，属管理动作 → **仅 super_admin** |

刻意**不**允许"向上级部门"发布：``dept_chain`` 里的上级只代表"我能看那里"，
不代表"我能往那里写"。要跨部门发布，走 super_admin 或后续的管理范围（scope）实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from ekbs.db import Database
from ekbs.permission.console import is_console_role

SUPER_ADMIN = "super_admin"


@dataclass(frozen=True)
class ScopeDecision:
    allowed: bool
    reason: str = ""  # "" | user | level | global | unknown_dept | dept
    detail: str = ""


def _manageable_dept_ids(db: Database, primary_dept_id: int | None) -> set[int]:
    """可管理部门 = 自己所在部门 + 其所有下级。

    ``ekbs_dept_closure`` 的列是 ``(dept_id, ancestor_id, depth)`` —— 按祖先查，
    取 ``dept_id`` 即得该祖先的全部后代（含 depth=0 的自身行）。
    """
    if primary_dept_id is None:
        return set()
    with db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT dept_id FROM ekbs_dept_closure WHERE ancestor_id = ?",
            (int(primary_dept_id),),
        ).fetchall()
    return {int(r["dept_id"]) for r in rows} | {int(primary_dept_id)}


def check_document_scope(
    db: Database, *, user_id: int, level: int, visible_dept_ids: Sequence[str]
) -> ScopeDecision:
    """判定某用户能否上架一份「密级 = level、可见部门 = visible_dept_ids」的文档。"""
    with db.sync_connect() as conn:
        user: Any = conn.execute(
            "SELECT clearance, primary_dept_id, status, deleted_at FROM ekbs_user"
            " WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
        if user is None or str(user["status"]) != "active" or user["deleted_at"] is not None:
            return ScopeDecision(False, "user", "用户不存在或已停用")
        roles = {
            str(r["code"])
            for r in conn.execute(
                "SELECT r.code FROM ekbs_user_role ur JOIN ekbs_role r ON r.role_id = ur.role_id"
                " WHERE ur.user_id = ? AND r.status = 1",
                (int(user_id),),
            ).fetchall()
        }

    if SUPER_ADMIN in roles:
        return ScopeDecision(True)

    clearance = int(user["clearance"])
    if int(level) > clearance:
        return ScopeDecision(
            False, "level", f"文档密级 L{level} 高于你的密级上限 L{clearance}"
        )

    targets = [str(d) for d in (visible_dept_ids or [])]
    # 后台角色不按钉钉部门收窄。不存在的部门仍然拒绝。全公司公开允许。
    if is_console_role(roles):
        if not targets:
            return ScopeDecision(True)
        with db.sync_connect() as conn:
            rows = conn.execute(
                "SELECT dingtalk_dept_id FROM ekbs_dept"
                f" WHERE dingtalk_dept_id IN ({','.join('?' for _ in targets)})",
                tuple(targets),
            ).fetchall()
        known = {str(r["dingtalk_dept_id"]) for r in rows}
        unknown = [d for d in targets if d not in known]
        if unknown:
            return ScopeDecision(False, "unknown_dept", f"部门不存在：{', '.join(unknown)}")
        return ScopeDecision(True)

    manageable = _manageable_dept_ids(db, user["primary_dept_id"])

    if not targets:
        # 不指定部门 = 全公司公开：这是**跨部门**动作，要求管理范围覆盖全部在用部门
        # （总部/根部门管理员天然满足），否则单部门主管不该发全员文档。
        with db.sync_connect() as conn:
            all_depts = {
                int(r["dept_id"])
                for r in conn.execute("SELECT dept_id FROM ekbs_dept WHERE status = 'active'")
            }
        if all_depts and not all_depts.issubset(manageable):
            return ScopeDecision(
                False,
                "global",
                "不指定部门的文档等于全公司公开，需要管理范围覆盖全部部门（如总部管理员）"
                "或超级管理员",
            )
        return ScopeDecision(True)

    with db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT dept_id, dingtalk_dept_id FROM ekbs_dept"
            f" WHERE dingtalk_dept_id IN ({','.join('?' for _ in targets)})",
            tuple(targets),
        ).fetchall()
    known = {str(r["dingtalk_dept_id"]): int(r["dept_id"]) for r in rows}
    unknown = [d for d in targets if d not in known]
    if unknown:
        return ScopeDecision(False, "unknown_dept", f"部门不存在：{', '.join(unknown)}")
    outside = [d for d in targets if known[d] not in manageable]
    if outside:
        return ScopeDecision(
            False,
            "dept",
            f"部门 {', '.join(outside)} 不在你的管理范围内（只能发布到本人所在部门及其下级部门）",
        )
    return ScopeDecision(True)
