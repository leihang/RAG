"""文件夹上的入库权限。

密级向下生效：子文件夹里的文件不能比上级文件夹更公开，没另写密级时用上级的密级。
可见部门不向下套用：工程部、客服部即使都在物业中心下面，也各自只看自己的部门。
物业中心这一层直接存放的文件，才使用物业中心自己的可见部门。

设置写在该目录里的 ``.ekbs-folder.json``。目录从磁盘删除后，这份设置一起消失，
界面再列目录时不会留下已经不存在的文件夹。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ekbs.config import Settings
from ekbs.db import Database
from ekbs.errors import ConfigError
from ekbs.ingest.dir_import import ImportPathError, raw_root, resolve_within_raw, to_rel
from ekbs.personal import personal_user_id_from_code

POLICY_FILENAME = ".ekbs-folder.json"
#: 删空文件夹时可以一并清掉的系统杂文件，不算「里面还有内容」。
_IGNORED_NAMES = {POLICY_FILENAME, ".DS_Store"}


class FolderNotEmpty(ImportPathError):
    """文件夹里还有文件或子目录，拒绝删除。"""


def _read_policy_file(directory: Path) -> dict[str, Any] | None:
    path = directory / POLICY_FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        level = int(data.get("level"))
    except (TypeError, ValueError):
        return None
    if level < 1 or level > 4:
        return None
    depts_raw = data.get("visible_dept_ids") or []
    viewers_raw = data.get("viewer_user_ids") or []
    if not isinstance(depts_raw, list) or not isinstance(viewers_raw, list):
        return None
    depts = [str(item).strip() for item in depts_raw if str(item).strip()]
    viewers: list[int] = []
    for item in viewers_raw:
        try:
            user_id = int(item)
        except (TypeError, ValueError):
            continue
        if user_id > 0 and user_id not in viewers:
            viewers.append(user_id)
    return {
        "level": level,
        "visible_dept_ids": depts,
        "viewer_user_ids": viewers,
    }


def _containing_dir(settings: Settings, rel: str) -> tuple[Path, Path] | None:
    """返回 ``(raw 根, 这一层目录)``。``rel`` 是文件时取它所在的目录，不往上跳。"""
    try:
        target = resolve_within_raw(settings, rel)
    except ImportPathError:
        return None
    root = raw_root(settings).resolve()
    current = target.resolve() if target.is_dir() else target.resolve().parent
    try:
        current.relative_to(root)
    except ValueError:
        return None
    return root, current


def resolve_folder_policy(settings: Settings, rel: str) -> tuple[dict[str, Any], str] | None:
    """只读这一层目录自己的权限，不读上级文件夹。

    ``rel`` 是目录时读它自己；是文件时读它所在的那一层。没有则返回 None。
    """
    located = _containing_dir(settings, rel)
    if located is None:
        return None
    root, current = located
    if current == root:
        return None
    found = _read_policy_file(current)
    if found is None:
        return None
    return found, to_rel(settings, current)


def _policy_chain(settings: Settings, rel: str) -> list[tuple[str, dict[str, Any] | None]]:
    """从这一层往上走到知识库目录为止。最近的一层在前。raw 根不算。"""
    located = _containing_dir(settings, rel)
    if located is None:
        return []
    root, current = located
    chain: list[tuple[str, dict[str, Any] | None]] = []
    while current != root:
        chain.append((to_rel(settings, current), _read_policy_file(current)))
        parent = current.parent
        if parent == current:
            break
        current = parent
    return chain


def _ancestor_level(chain: list[tuple[str, dict[str, Any] | None]]) -> tuple[int | None, str | None]:
    """链上除本层以外，最严的密级（数字更大更严）以及写下它的文件夹。"""
    best: int | None = None
    owner: str | None = None
    for rel, policy in chain[1:]:
        if policy is None:
            continue
        level = int(policy["level"])
        if best is None or level > best:
            best = level
            owner = rel
    return best, owner


def effective_folder_policy(settings: Settings, rel: str) -> tuple[dict[str, Any] | None, str | None]:
    """文件的生效权限。

    密级取本层和全部上级里更严的那一档。可见部门和额外可查看的人只取本层。
    本层还没写部门时，返回拒绝原因，不用上级的部门。
    """
    chain = _policy_chain(settings, rel)
    if not chain:
        return None, None
    here, own = chain[0]
    ancestor_level, ancestor_rel = _ancestor_level(chain)
    if own is None:
        if ancestor_level is None:
            return None, None
        return None, (
            f"密级沿用上级「{ancestor_rel}」的 L{ancestor_level}。"
            f"请给「{here}」单独选择可见部门，不要用上级文件夹的部门，"
            f"否则不同子部门会互相看见。"
        )
    level = int(own["level"])
    if ancestor_level is not None and ancestor_level > level:
        level = ancestor_level
    return {
        "level": level,
        "visible_dept_ids": list(own["visible_dept_ids"]),
        "viewer_user_ids": list(own["viewer_user_ids"]),
    }, None


def policy_view(settings: Settings, rel: str) -> dict[str, Any]:
    """给目录列表用的扁平字段。密级含上级下限；部门只算本层。"""
    chain = _policy_chain(settings, rel)
    empty = {
        "level": None,
        "visible_dept_ids": [],
        "viewer_user_ids": [],
        "policy_from": None,
    }
    if not chain:
        return empty
    here, own = chain[0]
    ancestor_level, ancestor_rel = _ancestor_level(chain)
    if own is None and ancestor_level is None:
        return empty
    if own is None:
        return {
            "level": ancestor_level,
            "visible_dept_ids": [],
            "viewer_user_ids": [],
            "policy_from": ancestor_rel,
        }
    level = int(own["level"])
    source = here
    if ancestor_level is not None and ancestor_level > level:
        level = ancestor_level
        source = ancestor_rel
    return {
        "level": level,
        "visible_dept_ids": list(own["visible_dept_ids"]),
        "viewer_user_ids": list(own["viewer_user_ids"]),
        "policy_from": source,
    }


def _safe_upload_rel(filename: str) -> str:
    parts: list[str] = []
    for raw in str(filename or "").replace("\\", "/").split("/"):
        seg = raw.strip()
        if not seg or seg in {".", ".."} or seg.startswith("."):
            continue
        parts.append(seg)
    return "/".join(parts) or "未命名"


def policy_for_upload(settings: Settings, kb_code: str, filename: str) -> dict[str, Any] | None:
    """上传文件的生效权限。密级受上级文件夹限制，部门只用本层。

    个人库不读这份文件。
    """
    code = str(kb_code or "").strip().strip("/")
    if not code or personal_user_id_from_code(code) is not None:
        return None
    policy, refuse = effective_folder_policy(settings, f"{code}/{_safe_upload_rel(filename)}")
    if refuse:
        raise ConfigError(refuse)
    return policy


def permission_for_import_file(
    settings: Settings, rel: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """导入时的生效权限。密级受上级限制；本层没有部门时返回拒绝原因。"""
    return effective_folder_policy(settings, rel)


def save_folder_policy(
    settings: Settings,
    db: Database,
    rel: str,
    *,
    level: int,
    visible_dept_ids: list[str],
    viewer_user_ids: list[int],
) -> dict[str, Any]:
    """把权限写进已经存在的文件夹。知识库必须是路径的第一段。"""
    target = resolve_within_raw(settings, rel)
    if not target.is_dir():
        raise ImportPathError("请先建立文件夹，再写入密级和部门")
    folder_rel = to_rel(settings, target)
    kb_code = folder_rel.split("/")[0] if folder_rel else ""
    if not kb_code:
        raise ConfigError("请在知识库下面建文件夹，不要把权限写在 raw 根上")
    if personal_user_id_from_code(kb_code) is not None:
        raise ConfigError("个人库只对主人开放，不能在文件夹上再指定密级、部门或其他人")
    settings.assert_l3_ingest_allowed(int(level))
    if int(level) == 4 and not settings.permission.l4_enabled:
        raise ConfigError("L4 未开放入库，文件夹不能设成 L4")
    ancestor_level, ancestor_rel = _ancestor_level(_policy_chain(settings, folder_rel))
    if ancestor_level is not None and int(level) < ancestor_level:
        raise ConfigError(
            f"上级文件夹「{ancestor_rel}」是 L{ancestor_level}，"
            f"这里不能设成更公开的 L{int(level)}"
        )
    from ekbs.index.shard import resolve_shard_for_document
    from ekbs.permission.viewers import assert_viewers_assignable

    depts = [str(item).strip() for item in visible_dept_ids if str(item).strip()]
    resolve_shard_for_document(
        level=int(level), visible_dept_ids=depts, settings=settings,
    )
    with db.sync_connect() as conn:
        row = conn.execute(
            "SELECT kb_id FROM ekbs_kb WHERE code = ?", (kb_code,)
        ).fetchone()
        if row is None:
            raise ConfigError(f"知识库不存在：{kb_code}")
        viewers = assert_viewers_assignable(
            conn,
            kb_id=int(row["kb_id"]),
            kb_code=kb_code,
            viewer_user_ids=viewer_user_ids,
        )
    policy = {
        "level": int(level),
        "visible_dept_ids": depts,
        "viewer_user_ids": viewers,
    }
    (target / POLICY_FILENAME).write_text(
        json.dumps(policy, ensure_ascii=False), encoding="utf-8",
    )
    return policy


def remove_import_dir(settings: Settings, rel: str) -> str:
    """删除 raw 下的空文件夹。已经不存在也算删掉，方便界面清掉残留名字。

    只含权限文件或 ``.DS_Store`` 视为空。里面还有文件或子文件夹则拒绝。
    """
    cleaned = str(rel or "").strip().replace("\\", "/").strip("/")
    if not cleaned:
        raise ImportPathError("不能删除 raw 根目录")
    try:
        target = resolve_within_raw(settings, cleaned)
    except ImportPathError:
        raise
    root = raw_root(settings).resolve()
    if target.resolve() == root:
        raise ImportPathError("不能删除 raw 根目录")
    if not target.exists():
        return cleaned
    if not target.is_dir():
        raise ImportPathError(f"不是文件夹：{cleaned}")
    leftovers: list[str] = []
    for child in target.iterdir():
        if child.name in _IGNORED_NAMES or (child.is_file() and child.name.startswith(".")):
            continue
        leftovers.append(child.name)
    if leftovers:
        shown = "、".join(leftovers[:5])
        raise FolderNotEmpty(f"文件夹里还有内容，不能删：{shown}")
    for child in list(target.iterdir()):
        if child.is_file() or child.is_symlink():
            child.unlink()
        else:
            raise FolderNotEmpty(f"文件夹里还有子目录：{child.name}")
    target.rmdir()
    return cleaned
