"""把 ``ekbs_dept`` 排成组织架构的先序，并挂上分片部门码。

库里的 ``dept_path`` / ``depth`` 对已同步的真实部门经常是空的（全是 ``/`` 和 0），
层级只在 ``parent_dept_id`` 上。页面如果按 ``dept_path`` 排序，看不出上下级。
"""

from __future__ import annotations

from typing import Any


def order_dept_tree(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 ``parent_dept_id`` 先序展开，并写入 ``tree_depth``（根为 0）。

    上级不在本批结果里、或自己指向自己，都当成根。环上没被走到的节点补在末尾。
    """
    items = [dict(row) for row in rows]
    by_id = {int(row["dept_id"]): row for row in items}
    children: dict[int | None, list[dict[str, Any]]] = {}
    for row in items:
        parent = row.get("parent_dept_id")
        parent_id = int(parent) if parent is not None else None
        if parent_id is None or parent_id not in by_id or parent_id == int(row["dept_id"]):
            key: int | None = None
        else:
            key = parent_id
        children.setdefault(key, []).append(row)
    for kids in children.values():
        kids.sort(key=lambda row: (str(row.get("name") or ""), int(row["dept_id"])))

    ordered: list[dict[str, Any]] = []
    seen: set[int] = set()

    def walk(parent_key: int | None, depth: int) -> None:
        for row in children.get(parent_key, []):
            dept_id = int(row["dept_id"])
            if dept_id in seen:
                continue
            seen.add(dept_id)
            row["tree_depth"] = depth
            ordered.append(row)
            walk(dept_id, depth + 1)

    walk(None, 0)
    for row in items:
        dept_id = int(row["dept_id"])
        if dept_id not in seen:
            row["tree_depth"] = 0
            ordered.append(row)
    return ordered


def attach_dept_codes(
    rows: list[dict[str, Any]], dept_code_map: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """先排树，再按钉钉部门 ID 反查 ``dept_code_map``（部门码 → ID 列表）。"""
    ordered = order_dept_tree(rows)
    code_by_dt: dict[str, list[str]] = {}
    for code, ids in (dept_code_map or {}).items():
        values = [ids] if isinstance(ids, str) else list(ids or [])
        for raw in values:
            code_by_dt.setdefault(str(raw), []).append(str(code))
    for row in ordered:
        codes = code_by_dt.get(str(row.get("dingtalk_dept_id") or ""), [])
        row["dept_codes"] = codes
        row["dept_code"] = codes[0] if len(codes) == 1 else ",".join(codes)
    return ordered
