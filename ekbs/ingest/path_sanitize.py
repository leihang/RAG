"""上传/导入路径消毒与 root 内相对路径判定。"""

from __future__ import annotations

from pathlib import Path


def safe_upload_relative(filename: str) -> str:
    """把客户端文件名规整成 raw 目录内的相对路径，挡掉越界写法。

    绝对路径、``..`` 段、空段、以点开头的段一律剔除；全空时回落 ``未命名``。
    """
    parts: list[str] = []
    for raw in str(filename or "").replace("\\", "/").split("/"):
        seg = raw.strip()
        if not seg or seg in {".", ".."} or seg.startswith("."):
            continue
        parts.append(seg)
    return "/".join(parts) or "未命名"


def relative_under(path: Path, root: Path) -> Path | None:
    """若 ``path`` 落在 ``root`` 内返回相对路径，否则 ``None``。"""
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
