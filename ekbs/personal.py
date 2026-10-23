"""个人私有库，以及清掉种子演示账号。

私有文档的可见范围写成 ``user:<用户 ID>``。它不是部门，所以：

* 分片是 ``kb_p<用户 ID>_L2``，只通过 shard 级 ACL 授给这个人；
* 知识库 ``retrieval_config`` 标 ``personal``，不进「全员可见知识库」；
* 别人的部门链对不上 ``user:``，文档闸门按部门规则拒绝。

人员调岗只改部门链，私有库仍挂在用户 ID 上。离职后账号不再是 active，分片路由直接拒绝。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ekbs.config import Settings
from ekbs.db import Database
from ekbs.errors import ConfigError
from ekbs.ingest.dir_import import raw_root
from ekbs.ingest.parser import SUFFIX_ENGINES

PERSONAL_LEVEL = 2
_USER_REF = re.compile(r"^user:(\d+)$")
_DEMO_USER_PREFIX = "demo_"
#: 种子数据里的部门。只有在里面已经没有非演示人员时才软删。
_DEMO_DEPT_IDS = {"102", "103"}


def personal_max_file_mb(settings: Settings) -> float:
    """与管理台上传同一配置 ``limits.max_file_size_mb``，缺省 500。"""
    return float((settings.limits or {}).get("max_file_size_mb", 500) or 500)


def personal_upload_exceeds(size: int, settings: Settings) -> bool:
    return int(size) > personal_max_file_mb(settings) * 1024 * 1024


def personal_upload_reject_text(settings: Settings) -> str:
    return f"文件超过 {personal_max_file_mb(settings):g} MB，私有库暂不接收"


def personal_storage_limit_bytes(settings: Settings) -> int:
    """每人私有库的总容量。``limits.personal_storage_mb``，缺省 1024（1 GB）。"""
    mb = float((settings.limits or {}).get("personal_storage_mb", 1024) or 1024)
    return int(mb * 1024 * 1024)


def format_storage(num_bytes: int) -> str:
    """给人看的容量。整 GB、整 MB 不带小数。"""
    n = max(0, int(num_bytes))
    gb = 1024 * 1024 * 1024
    mb = 1024 * 1024
    if n >= gb and n % gb == 0:
        return f"{n // gb} GB"
    if n >= gb:
        return f"{n / gb:.2f} GB"
    if n % mb == 0:
        return f"{n // mb} MB"
    return f"{n / mb:.1f} MB"


def personal_storage_used(conn: Any, *, user_id: int) -> int:
    """该用户私有库里尚未删除的文件字节数。"""
    code = f"personal-{int(user_id)}"
    row = conn.execute(
        "SELECT COALESCE(SUM(d.size_bytes), 0) AS used "
        "FROM ekbs_document d JOIN ekbs_kb k ON k.kb_id = d.kb_id "
        "WHERE k.code = ? AND d.status != 'deleted' AND d.deleted_at IS NULL",
        (code,),
    ).fetchone()
    return int(row["used"] or 0)


def personal_storage_reject_text(settings: Settings, *, used: int, incoming: int) -> str:
    limit = personal_storage_limit_bytes(settings)
    return (
        f"私有库最多存放 {format_storage(limit)}，目前已用 {format_storage(used)}。"
        f"这份文件有 {format_storage(incoming)}，放进去会超出，没有存入。"
        "说「删除我的文件 标题」腾出空间后再发。"
    )


def assert_personal_storage(
    conn: Any, settings: Settings, *, user_id: int, incoming: int
) -> None:
    """已用 + 本次 > 上限则拒绝。刚好用满可以存。"""
    used = personal_storage_used(conn, user_id=user_id)
    if used + int(incoming) > personal_storage_limit_bytes(settings):
        raise ConfigError(
            personal_storage_reject_text(settings, used=used, incoming=int(incoming))
        )


def assert_personal_kb_storage(
    conn: Any,
    settings: Settings,
    *,
    kb_id: int | None = None,
    kb_code: str | None = None,
    incoming: int,
) -> None:
    """目标是个人库时才检查总容量。部门库不动。"""
    owner = personal_user_id_from_code(kb_code)
    if owner is None and kb_id is not None:
        row = conn.execute(
            "SELECT code FROM ekbs_kb WHERE kb_id = ?", (int(kb_id),)
        ).fetchone()
        if row is not None:
            owner = personal_user_id_from_code(row["code"])
    if owner is None:
        return
    assert_personal_storage(conn, settings, user_id=owner, incoming=incoming)


_STATUS_LABEL = {
    "pending": "等待解析",
    "parsing": "解析中",
    "review": "待复核",
    "indexing": "入库中",
    "published": "可提问",
    "failed": "解析失败",
    "offline": "已下线",
}


def scope_ref(user_id: int) -> str:
    return f"user:{int(user_id)}"


def sole_personal_owner(visible_dept_ids: list[str] | tuple[str, ...]) -> int | None:
    """全部是同一个 ``user:<id>`` 时返回该用户；掺了部门或为空则不是私有范围。"""
    owners: list[int] = []
    for raw in visible_dept_ids:
        match = _USER_REF.match(str(raw).strip())
        if not match:
            return None
        owners.append(int(match.group(1)))
    if not owners:
        return None
    if len(set(owners)) != 1:
        raise ConfigError("私有文档只能属于一个用户")
    return owners[0]


def is_personal_scope(raw: Any) -> bool:
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw or "[]")
        except ValueError:
            return False
    else:
        parsed = list(raw or [])
    try:
        return sole_personal_owner([str(item) for item in parsed]) is not None
    except ConfigError:
        return False


def personal_shard_id(user_id: int, level: int = PERSONAL_LEVEL) -> str:
    from ekbs.index.shard import make_shard_id

    return make_shard_id(f"p{int(user_id)}", level)


_PERSONAL_CODE = re.compile(r"^personal-(\d+)$")
_PERSONAL_DEPT = re.compile(r"^p(\d+)$")


def personal_kb_name(user_id: int, nick: str) -> str:
    """管理台上看个人库时，名称里同时有姓名和用户编号。"""
    shown = (nick or "").strip() or "未命名"
    return f"{shown}（用户 {int(user_id)}）的私有库"


def personal_user_id_from_code(code: str | None) -> int | None:
    match = _PERSONAL_CODE.match(str(code or "").strip())
    return int(match.group(1)) if match else None


def personal_user_id_from_dept(dept_code: str | None) -> int | None:
    match = _PERSONAL_DEPT.match(str(dept_code or "").strip())
    return int(match.group(1)) if match else None


def attach_personal_owners(conn: Any, records: list[dict[str, Any]]) -> None:
    """给个人库记录补上姓名、用户编号、钉钉 ID。

    认两种代号：知识库 ``personal-<用户 ID>``，分片部门码 ``p<用户 ID>``。
    对不上的记录不动。
    """
    wanted: dict[int, None] = {}
    located: list[tuple[dict[str, Any], int]] = []
    for record in records:
        user_id = personal_user_id_from_code(record.get("kb_code") or record.get("code"))
        if user_id is None:
            user_id = personal_user_id_from_dept(record.get("dept_code"))
        if user_id is None:
            continue
        wanted[user_id] = None
        located.append((record, user_id))
    owners: dict[int, Any] = {}
    if wanted:
        marks = ",".join("?" for _ in wanted)
        rows = conn.execute(
            "SELECT user_id, nick, dingtalk_userid FROM ekbs_user "
            f"WHERE user_id IN ({marks})",
            tuple(wanted),
        ).fetchall()
        owners = {int(row["user_id"]): row for row in rows}
    for record, user_id in located:
        row = owners.get(user_id)
        record["owner_user_id"] = user_id
        record["owner_nick"] = str(row["nick"]) if row and row["nick"] else None
        record["owner_dingtalk_userid"] = (
            str(row["dingtalk_userid"]) if row and row["dingtalk_userid"] else None
        )


def ensure_personal_kb(conn: Any, *, user_id: int, nick: str) -> int:
    code = f"personal-{int(user_id)}"
    name = personal_kb_name(int(user_id), nick)
    config = json.dumps(
        {"personal": True, "owner_user_id": int(user_id)}, ensure_ascii=False
    )
    conn.execute(
        "INSERT INTO ekbs_kb(code, name, description, default_level, retrieval_config, status) "
        "VALUES (?, ?, ?, ?, ?, 1) "
        "ON CONFLICT(code) DO UPDATE SET name = excluded.name, "
        "retrieval_config = excluded.retrieval_config, status = 1",
        (code, name, "仅本人可见，由钉钉单聊上传建立", PERSONAL_LEVEL, config),
    )
    row = conn.execute("SELECT kb_id FROM ekbs_kb WHERE code = ?", (code,)).fetchone()
    return int(row["kb_id"])


def grant_personal_shard(conn: Any, *, user_id: int, shard_id: str) -> None:
    """把私有分片的读权限只授给本人，并抬高策略版本，让旧权限缓存失效。"""
    from ekbs.permission.router import bump_policy_version

    conn.execute(
        "INSERT INTO ekbs_acl_entry(object_type, object_id, subject_type, subject_id, "
        "permission, effect) VALUES ('shard', ?, 'user', ?, 'read', 'allow') "
        "ON CONFLICT(object_type, object_id, subject_type, subject_id, permission) "
        "DO UPDATE SET effect = 'allow'",
        (shard_id, str(int(user_id))),
    )
    bump_policy_version(conn, reason=f"personal_shard:{user_id}")


def _safe_filename(filename: str) -> str:
    name = Path(str(filename or "")).name.strip()
    if not name or name in {".", ".."} or name.startswith("."):
        raise ConfigError("文件名不合法")
    if "/" in name or "\\" in name:
        raise ConfigError("文件名不合法")
    suffix = Path(name).suffix.lower()
    if suffix not in SUFFIX_ENGINES:
        allowed = "、".join(sorted(SUFFIX_ENGINES))
        raise ConfigError(f"不支持的格式 {suffix or '（无后缀）'}。可以发送：{allowed}")
    return name


def register_personal_upload(
    db: Database,
    settings: Settings,
    *,
    user_id: int,
    filename: str,
    content: bytes,
) -> dict[str, Any]:
    """把钉钉单聊发来的文件登记进本人的私有库，状态为待解析。"""
    if not content:
        raise ConfigError("文件是空的")
    if personal_upload_exceeds(len(content), settings):
        raise ConfigError(personal_upload_reject_text(settings))
    safe_name = _safe_filename(filename)
    checksum = hashlib.sha256(content).hexdigest()
    with db.sync_connect() as conn:
        user = conn.execute(
            "SELECT user_id, nick, status FROM ekbs_user "
            "WHERE user_id = ? AND deleted_at IS NULL",
            (int(user_id),),
        ).fetchone()
        if user is None or str(user["status"]) != "active":
            raise ConfigError("账号不可用，不能建立私有库")
        kb_id = ensure_personal_kb(conn, user_id=int(user_id), nick=str(user["nick"] or ""))
        existing = conn.execute(
            "SELECT doc_id, title, status FROM ekbs_document "
            "WHERE kb_id = ? AND checksum = ? AND status != 'deleted' AND deleted_at IS NULL",
            (kb_id, checksum),
        ).fetchone()
        if existing is not None:
            conn.commit()
            return {
                "doc_id": int(existing["doc_id"]),
                "title": str(existing["title"]),
                "status": str(existing["status"]),
                "created": False,
                "message": f"这份文件已在你的私有库里（{existing['title']}，{_STATUS_LABEL.get(str(existing['status']), existing['status'])}）。",
            }
        assert_personal_storage(
            conn, settings, user_id=int(user_id), incoming=len(content)
        )
        folder = raw_root(settings) / "personal" / str(int(user_id))
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / safe_name
        if target.exists():
            stem, suffix = target.stem, target.suffix
            n = 2
            while target.exists():
                target = folder / f"{stem}-{n}{suffix}"
                n += 1
        target.write_bytes(content)
        scope = scope_ref(int(user_id))
        cursor = conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, visible_dept_ids, "
            "status, checksum, size_bytes, doc_version, created_by) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, 'v1', ?)",
            (
                kb_id,
                target.stem,
                str(target),
                PERSONAL_LEVEL,
                json.dumps([scope], ensure_ascii=False),
                checksum,
                len(content),
                int(user_id),
            ),
        )
        conn.commit()
        doc_id = int(cursor.lastrowid)
    return {
        "doc_id": doc_id,
        "title": target.stem,
        "status": "pending",
        "created": True,
        "message": (
            f"已放入你的私有库：{target.stem}。"
            "解析完成后就可以直接提问。说「我的文件」查看，说「删除我的文件 标题」删除。"
            "这份文件只有你能看。"
        ),
    }


def list_personal_files(db: Database, *, user_id: int) -> list[dict[str, Any]]:
    scope = scope_ref(int(user_id))
    with db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT doc_id, title, status FROM ekbs_document "
            "WHERE created_by = ? AND status != 'deleted' AND deleted_at IS NULL "
            "AND visible_dept_ids LIKE ? ORDER BY doc_id DESC",
            (int(user_id), f'%"{scope}"%'),
        ).fetchall()
    return [
        {
            "doc_id": int(row["doc_id"]),
            "title": str(row["title"]),
            "status": str(row["status"]),
            "status_label": _STATUS_LABEL.get(str(row["status"]), str(row["status"])),
        }
        for row in rows
    ]


def format_personal_list(items: list[dict[str, Any]]) -> str:
    if not items:
        return "你的私有库还是空的。在和机器人的单聊里直接发送文件，就会建到这里，只有你能看。"
    lines = ["你的私有库（只有你能看）："]
    for index, item in enumerate(items[:40], 1):
        lines.append(f"{index}. {item['title']}（{item['status_label']}）")
    if len(items) > 40:
        lines.append(f"…共 {len(items)} 份，这里只列出 40 份")
    lines.append("删除请说「删除我的文件 标题」。")
    return "\n".join(lines)


def delete_personal_file(db: Database, *, user_id: int, title: str) -> dict[str, Any]:
    """按标题删除本人私有库里的文件。对不上或对上多份时不删。"""
    query = str(title or "").strip().lower()
    if not query:
        return {"deleted": False, "message": "请说「删除我的文件 标题」。标题用「我的文件」里看到的名字。"}
    items = list_personal_files(db, user_id=user_id)
    matches = [
        item for item in items
        if query == str(item["title"]).lower() or query in str(item["title"]).lower()
    ]
    if not matches:
        return {"deleted": False, "message": f"私有库里没有「{title.strip()}」。说「我的文件」可以看现有标题。"}
    titles = {str(item["title"]) for item in matches}
    if len(titles) > 1:
        shown = "、".join(sorted(titles)[:8])
        return {"deleted": False, "message": f"匹配到多份，请说完整标题：{shown}"}
    chosen = matches[0]
    with db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_document SET status = 'deleted', deleted_at = datetime('now'), "
            "updated_at = datetime('now') WHERE doc_id = ? AND created_by = ?",
            (int(chosen["doc_id"]), int(user_id)),
        )
        conn.commit()
    return {"deleted": True, "doc_id": int(chosen["doc_id"]), "message": f"已从你的私有库删除「{chosen['title']}」。"}


def purge_demo_accounts(db: Database) -> dict[str, Any]:
    """软删 ``demo_`` 开头的种子账号；演示部门若已没有真实人员，一并软删。

    钉钉同步不会再把这些账号拉回来。部门若仍在钉钉通讯录里，下次同步会按真实数据重建。
    """
    from ekbs.permission.router import bump_policy_version
    from ekbs.tools.sync_org import rebuild_dept_closure, refresh_user_dept_chain

    with db.sync_connect() as conn:
        users = conn.execute(
            "SELECT user_id, dingtalk_userid, nick FROM ekbs_user "
            "WHERE dingtalk_userid LIKE ? AND deleted_at IS NULL",
            (_DEMO_USER_PREFIX + "%",),
        ).fetchall()
        user_ids = [int(row["user_id"]) for row in users]
        if user_ids:
            marks = ",".join("?" for _ in user_ids)
            conn.execute(
                f"DELETE FROM ekbs_user_dept WHERE user_id IN ({marks})",
                tuple(user_ids),
            )
            conn.execute(
                f"UPDATE ekbs_user SET status = 'left', deleted_at = datetime('now') "
                f"WHERE user_id IN ({marks})",
                tuple(user_ids),
            )
            conn.execute(
                f"UPDATE ekbs_local_account SET status = 'disabled', "
                f"updated_at = datetime('now') WHERE user_id IN ({marks})",
                tuple(user_ids),
            )
        removed_depts: list[str] = []
        for dept in conn.execute(
            "SELECT dept_id, dingtalk_dept_id, name FROM ekbs_dept WHERE deleted_at IS NULL"
        ):
            if str(dept["dingtalk_dept_id"]) not in _DEMO_DEPT_IDS:
                continue
            real = conn.execute(
                "SELECT u.user_id FROM ekbs_user_dept ud "
                "JOIN ekbs_user u ON u.user_id = ud.user_id "
                "WHERE ud.dept_id = ? AND u.deleted_at IS NULL "
                "AND u.dingtalk_userid NOT LIKE ?",
                (int(dept["dept_id"]), _DEMO_USER_PREFIX + "%"),
            ).fetchone()
            if real is not None:
                continue
            conn.execute(
                "UPDATE ekbs_dept SET status = 'deleted', deleted_at = datetime('now') "
                "WHERE dept_id = ?",
                (int(dept["dept_id"]),),
            )
            removed_depts.append(str(dept["name"]))
        if user_ids or removed_depts:
            rebuild_dept_closure(conn)
            refresh_user_dept_chain(conn)
            bump_policy_version(conn, reason="purge_demo_accounts")
        conn.commit()
    return {
        "users": [str(row["dingtalk_userid"]) for row in users],
        "departments": removed_depts,
    }
