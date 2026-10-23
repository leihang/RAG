"""后台角色与钉钉部门权限分开。

超级管理员、知识库管理员、文档审核员、审计员是管理台身份。
他们在管理台看文档、做检索时，不按钉钉部门链收窄。
密级上限仍然有效，别人的个人库不打开。钉钉问答不走这里。
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from ekbs.permission.context import PermContext

CONSOLE_ROLES = frozenset({"super_admin", "kb_admin", "doc_reviewer", "auditor"})
_PERSONAL_SHARD = re.compile(r"^p\d+$")


def is_console_role(roles: Any) -> bool:
    return bool(CONSOLE_ROLES.intersection(set(roles or ())))


def widen_for_console(perm: PermContext, db: Any) -> PermContext:
    """管理台用：补上全部公司部门与未超密级的公司分片。"""
    with db.sync_connect() as conn:
        shard_rows = conn.execute(
            "SELECT shard_id, dept_code, level FROM ekbs_shard WHERE status = 'active'"
        ).fetchall()
        dept_rows = conn.execute(
            "SELECT dept_id, dingtalk_dept_id FROM ekbs_dept WHERE status = 'active'"
        ).fetchall()
    shards = set(perm.visible_shard_ids)
    codes = set(perm.matched_dept_codes)
    for row in shard_rows:
        if int(row["level"]) > int(perm.clearance):
            continue
        code = str(row["dept_code"])
        if _PERSONAL_SHARD.match(code):
            continue
        shards.add(str(row["shard_id"]))
        codes.add(code)
    dept_refs = {str(d) for d in perm.dept_chain}
    subjects = set(perm.dept_subjects)
    for row in dept_rows:
        subjects.add(str(row["dept_id"]))
        ding = row["dingtalk_dept_id"]
        if ding:
            dept_refs.add(str(ding))
            subjects.add(str(ding))
    return replace(
        perm,
        dept_chain=tuple(sorted(dept_refs)),
        dept_subjects=frozenset(subjects),
        visible_shard_ids=tuple(sorted(shards)),
        matched_dept_codes=frozenset(codes),
    )
