"""删除文档后的残留清理（`ekbs/tools/gc.py`）。

删除接口只能清**库里的三处**；raw 原文件、parse 产物、FAISS 孤儿向量它清不掉
（FAISS 是 `IndexFlatIP`，不支持删除）。这个工具负责把这几处找出来 ——
所以它最重要的性质是：**默认只报告，不删任何东西**。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ekbs.db import Database
from ekbs.tools.gc import purge, scan


@pytest.fixture
def gc_env(settings, db):
    """造出三类残留：无人引用的 raw 文件、文档已删的 parse 目录、孤儿向量。"""
    data_dir = Path(settings.data_dir)
    raw = settings.data_paths()["raw"]
    parse = settings.data_paths()["parse"]
    (raw / "sha-keep").mkdir(parents=True, exist_ok=True)
    (raw / "sha-orphan").mkdir(parents=True, exist_ok=True)
    (raw / "sha-keep" / "还在用.md").write_text("x", encoding="utf-8")
    (raw / "sha-orphan" / "已删除.md").write_text("y", encoding="utf-8")
    (parse / "7").mkdir(parents=True, exist_ok=True)      # 文档已不存在
    (parse / "7" / "images").mkdir(parents=True, exist_ok=True)
    (parse / "7" / "images" / "a.jpg").write_bytes(b"jpg")
    (parse / "sha-abc").mkdir(parents=True, exist_ok=True)  # 无法判定归属，不该被删
    (parse / "sha-abc" / "keep.txt").write_text("k", encoding="utf-8")
    # 上传中断留下的临时分片目录（成功上传时后端会自己清）
    tmp = settings.data_paths()["tmp"]
    (tmp / "abandoned-upload").mkdir(parents=True, exist_ok=True)
    (tmp / "abandoned-upload" / "0.part").write_bytes(b"part")

    with db.sync_connect() as conn:
        # 这个夹具不需要 seeded 的全套语料，但 ekbs_document.kb_id 有外键，得先有库
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, default_level, status) "
            "VALUES ('gc', '清理测试库', 2, 1) ON CONFLICT(code) DO NOTHING"
        )
        kb_id = int(conn.execute("SELECT kb_id FROM ekbs_kb WHERE code = 'gc'").fetchone()["kb_id"])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, status, checksum) "
            "VALUES (?, '还在用', ?, 2, 'published', 'c1')",
            (kb_id, str((raw / "sha-keep" / "还在用.md").resolve())),
        )
        conn.commit()
    assert data_dir.exists()
    return settings, db


def test_scan_reports_without_deleting(gc_env) -> None:
    """**默认只报告** —— 这是这个工具最要紧的性质（别一不小心把语料删了）。"""
    settings, db = gc_env
    report = scan(settings, db)
    assert any("已删除.md" in p for p in report.raw_orphans)
    assert not any("还在用.md" in p for p in report.raw_orphans), "有引用的文件不该算孤儿"
    assert report.parse_orphans == ["parse/7"], "文档已不存在的 parse 目录才算孤儿"
    assert any("abandoned-upload" in p for p in report.tmp_orphans), "上传中断的临时目录要报出来"
    # 只报告：文件必须还在
    assert (Path(settings.data_dir) / "raw/sha-orphan/已删除.md").exists()
    assert (Path(settings.data_dir) / "parse/7").exists()
    assert (Path(settings.data_dir) / "tmp/abandoned-upload").exists()


def test_purge_removes_only_orphans(gc_env) -> None:
    settings, db = gc_env
    data_dir = Path(settings.data_dir)
    report = purge(settings, scan(settings, db))

    assert report.deleted_files >= 2
    assert not (data_dir / "raw/sha-orphan/已删除.md").exists()
    assert not (data_dir / "parse/7").exists()
    assert not (data_dir / "tmp/abandoned-upload").exists()
    # 有引用的与无法判定归属的都要留着
    assert (data_dir / "raw/sha-keep/还在用.md").exists()
    assert (data_dir / "parse/sha-abc/keep.txt").exists()
    # 空掉的 sha 目录顺手收掉，不留空壳
    assert not (data_dir / "raw/sha-orphan").exists()


def test_scan_reports_shard_drift(gc_env) -> None:
    """分片登记向量多于实际可检索块 → 就是孤儿向量（删文档造成的）。"""
    settings, db = gc_env
    with db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_shard(shard_id, dept_code, level, faiss_path, dim, "
            "embedding_fingerprint, vector_count, index_type, status) "
            "VALUES ('kb_ghost_L1', 'all', 1, '/tmp/x.faiss', 1024, 'fp', 5, 'flat', 'active')"
        )
        conn.commit()
    report = scan(settings, db)
    drift = {row["shard_id"]: row for row in report.shard_drift}
    assert "kb_ghost_L1" in drift
    assert drift["kb_ghost_L1"]["registered_vectors"] == 5
    assert drift["kb_ghost_L1"]["live_chunks"] == 0
    assert drift["kb_ghost_L1"]["orphan_vectors"] == 5


def test_scan_is_clean_after_purge_and_rebuild(gc_env) -> None:
    """清理 + 重建之后必须回到「零残留」，否则这个工具就没法当验收用。"""
    settings, db = gc_env
    purge(settings, scan(settings, db))
    report = scan(settings, db)
    assert report.raw_orphans == []
    assert report.parse_orphans == []
    assert report.tmp_orphans == []
    assert report.tmp_orphans == []


def test_missing_directories_do_not_crash(settings, db) -> None:
    """raw/parse 不存在时（全新库）不该报错。"""
    import shutil

    for name in ("raw", "parse", "tmp"):
        path = settings.data_paths()[name]
        if path.exists():
            shutil.rmtree(path)
    report = scan(settings, db)
    assert report.raw_orphans == []
    assert report.parse_orphans == []
    assert report.tmp_orphans == []
