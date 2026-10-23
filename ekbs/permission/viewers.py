"""入库时点名的额外可查看人。

部门可见范围不变。这里只给写在文件上的个人编号加一条文档级允许，
检索再按这些文件所在的分片把范围补上。个人库不接受这份名单。
"""

from __future__ import annotations

from typing import Any, Sequence

from ekbs.errors import ConfigError
from ekbs.personal import personal_user_id_from_code

MAX_VIEWERS = 50


def normalize_viewer_user_ids(raw: Sequence[Any] | None) -> list[int]:
    """去重并保持传入顺序。空名单表示不额外放行任何人。"""
    seen: set[int] = set()
    out: list[int] = []
    for item in raw or []:
        try:
            user_id = int(item)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"可查看的人必须是用户编号：{item}") from exc
        if user_id <= 0:
            raise ConfigError(f"可查看的人必须是用户编号：{item}")
        if user_id in seen:
            continue
        seen.add(user_id)
        out.append(user_id)
    if len(out) > MAX_VIEWERS:
        raise ConfigError(f"额外可查看的人最多 {MAX_VIEWERS} 个")
    return out


def assert_viewers_assignable(
    conn: Any,
    *,
    kb_id: int | None = None,
    kb_code: str | None = None,
    viewer_user_ids: Sequence[Any] | None,
) -> list[int]:
    """个人库拒绝点名；其余库只接受在职用户。返回规范化后的用户编号。"""
    ids = normalize_viewer_user_ids(viewer_user_ids)
    if not ids:
        return []
    code = str(kb_code or "").strip()
    if not code and kb_id is not None:
        row = conn.execute("SELECT code FROM ekbs_kb WHERE kb_id = ?", (int(kb_id),)).fetchone()
        code = str(row["code"]) if row else ""
    if personal_user_id_from_code(code) is not None:
        raise ConfigError("个人库只对主人开放，不能再指定其他人查看")
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT user_id FROM ekbs_user WHERE user_id IN ({marks}) "
        "AND deleted_at IS NULL AND status = 'active'",
        tuple(ids),
    ).fetchall()
    found = {int(row["user_id"]) for row in rows}
    missing = [str(user_id) for user_id in ids if user_id not in found]
    if missing:
        raise ConfigError(f"这些用户不存在或已停用：{', '.join(missing)}")
    return ids


def grant_document_viewers(
    conn: Any,
    *,
    doc_id: int,
    viewer_user_ids: Sequence[int],
    operator_id: int | None,
    reason: str,
) -> None:
    """把点名的人写成文档级允许，并抬高策略版本。调用方负责提交事务。"""
    ids = list(viewer_user_ids)
    if not ids:
        return
    from ekbs.permission.router import bump_policy_version

    for user_id in ids:
        conn.execute(
            "INSERT INTO ekbs_acl_entry(object_type, object_id, subject_type, subject_id, "
            "permission, effect, granted_by, created_at) "
            "VALUES ('document', ?, 'user', ?, 'read', 'allow', ?, datetime('now')) "
            "ON CONFLICT(object_type, object_id, subject_type, subject_id, permission) "
            "DO UPDATE SET effect = 'allow', granted_by = excluded.granted_by",
            (str(int(doc_id)), str(int(user_id)), operator_id),
        )
    version = bump_policy_version(conn, reason=f"viewers:doc={int(doc_id)}")
    conn.execute(
        "INSERT INTO ekbs_perm_change_log(operator_id, object_type, object_id, change_type, "
        "field, old_value, new_value, reason, policy_version_before, policy_version_after) "
        "VALUES (?, 'document', ?, 'acl_grant', 'viewer_user_ids', '', ?, ?, ?, ?)",
        (
            operator_id,
            str(int(doc_id)),
            ",".join(str(user_id) for user_id in ids),
            reason,
            version - 1,
            version,
        ),
    )
