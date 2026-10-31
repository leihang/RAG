"""删文档后把空掉的父目录收掉（`ekbs.api.docs._prune_empty_dirs`）。

回归：删文档只删文件、不删目录时，导入页的目录树里会一直留着空文件夹，
看起来"删了还都在"。
"""

from __future__ import annotations

from pathlib import Path

from ekbs.api.docs import _prune_empty_dirs


def test_prune_empty_dirs_stops_at_non_empty_and_root(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    deep = root / "a" / "b" / "c"
    deep.mkdir(parents=True)
    document = deep / "x.docx"
    document.write_bytes(b"x")
    (root / "a" / "keep.txt").write_text("k", encoding="utf-8")

    document.unlink()
    pruned = _prune_empty_dirs(document, stop=root)

    assert not deep.exists()
    assert not (root / "a" / "b").exists()
    assert (root / "a").exists()  # 非空目录保留
    assert len(pruned) == 2
