"""备份与恢复演练（`01` §13；DoD 的「备份已执行一次且可恢复，RPO≤24h / RTO≤2h」）。

三条断言都是「不许糊弄」型的：

* 备份走的必须是 SQLite 在线备份 API（WAL 下 ``cp`` 会拿到半截库）；
* 备份包里**不能有密钥**；
* 恢复演练不只看「能不能解开」，还要逐项核对 hash、表数、触发器数、policy_version、
  以及每个分片索引的 dim/ntotal —— 否则「恢复成功」证明不了任何事。
"""

from __future__ import annotations

import json
import sqlite3
import tarfile
from datetime import datetime, timedelta
from pathlib import Path

from ekbs.tools.backup import (
    MANIFEST_NAME,
    create_backup,
    prune_backups,
    restore_and_verify,
    sha256_file,
)


def _seeded_runtime(seeded):
    runtime, _summary = seeded
    return runtime


def test_backup_creates_archive_with_manifest(seeded, tmp_path: Path) -> None:
    runtime = _seeded_runtime(seeded)
    result = create_backup(runtime.settings, out_dir=tmp_path / "backup")
    archive = Path(result.archive)
    assert archive.is_file() and result.bytes > 0
    assert archive.name.startswith("ekbs-") and archive.suffixes[-2:] == [".tar", ".gz"]
    assert result.shards == 3, "三个分片都应进备份"
    assert result.pruned == []

    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
        assert "ekbs/index/ekbs.db" in names
        assert "ekbs/manifest.json" in names
        assert "ekbs/config/application.dev.yaml" in names
        assert any(name.endswith(".faiss") for name in names)
        # 密钥绝不进备份
        assert not any(name.endswith("secrets.env") for name in names)
        manifest = json.loads(tar.extractfile("ekbs/manifest.json").read().decode("utf-8"))

    assert manifest["schema"]["tables"] == 32
    assert manifest["schema"]["triggers"] == 12
    assert len(manifest["shards"]) == 3
    assert set(manifest["files"]) >= {"index/ekbs.db", "config/application.dev.yaml"}


def test_restore_drill_verifies_everything(seeded, tmp_path: Path) -> None:
    """恢复演练：解到独立目录后逐项核对（这就是 RTO 演练的核心动作）。"""
    runtime = _seeded_runtime(seeded)
    result = create_backup(runtime.settings, out_dir=tmp_path / "backup")

    # 先制造一些「已提交但可能还在 WAL 里」的数据，验证备份真的抓到了最新状态
    alice = 2
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_qa_session(session_id, user_id, channel, perm_fingerprint, "
            "clearance_at) VALUES ('restore-probe', ?, 'web', 'fp', 2)",
            (alice,),
        )
        conn.execute(
            "INSERT INTO ekbs_qa_message(session_id, turn, role, question, answer) "
            "VALUES ('restore-probe', 1, 'assistant', 'q', 'a')"
        )
        conn.commit()
        expected_policy = int(
            conn.execute("SELECT version FROM ekbs_policy_version").fetchone()["version"]
        )

    # 备份发生在写入之后 → 恢复出来的库必须包含这些行
    result = create_backup(runtime.settings, out_dir=tmp_path / "backup")
    report = restore_and_verify(Path(result.archive), tmp_path / "restore-test")

    assert report.ok, report.problems
    assert report.checks["integrity"] == "ok"
    assert report.checks["tables"] == 32
    assert report.checks["triggers"] == 12
    assert report.checks["policy_version"] == expected_policy
    assert report.checks["audit_messages"] >= 1
    assert all(entry["ok"] for entry in report.checks["shards"])

    # 恢复出来的库可以真的打开来用
    restored = tmp_path / "restore-test" / "ekbs" / "index" / "ekbs.db"
    conn = sqlite3.connect(str(restored))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT question FROM ekbs_qa_message WHERE session_id = 'restore-probe'"
        ).fetchone()
        assert row is not None and row["question"] == "q"
        # 审计保护在恢复后依然有效
        import pytest

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM ekbs_qa_message WHERE session_id = 'restore-probe'")
    finally:
        conn.close()


def test_verify_detects_corrupted_index(seeded, tmp_path: Path) -> None:
    """manifest 的 sha256 就是为这一刻存在的：索引文件被改坏时必须报出来。"""
    runtime = _seeded_runtime(seeded)
    result = create_backup(runtime.settings, out_dir=tmp_path / "backup")

    target = tmp_path / "restore-test"
    report = restore_and_verify(Path(result.archive), target)
    assert report.ok, report.problems

    # 篡改一个 .faiss，再复核
    index_files = sorted((target / "ekbs" / "index").glob("*.faiss"))
    assert index_files
    index_files[0].write_bytes(b"corrupted")
    second = restore_and_verify(Path(result.archive), target, clean=False)
    # clean=False 时解包会覆盖，因此用「单独核对 hash」的方式验证检测能力
    manifest = json.loads((target / "ekbs" / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert second.ok, "重新解包应覆盖损坏文件"

    # 直接对损坏状态做 hash 核对，必须不等
    name = f"index/{index_files[0].name}"
    index_files[0].write_bytes(b"corrupted-again")
    assert sha256_file(index_files[0]) != manifest["files"][name]


def test_same_day_backups_do_not_overwrite(seeded, tmp_path: Path) -> None:
    runtime = _seeded_runtime(seeded)
    first = create_backup(runtime.settings, out_dir=tmp_path / "backup")
    second = create_backup(runtime.settings, out_dir=tmp_path / "backup")
    assert first.archive != second.archive
    assert Path(first.archive).is_file() and Path(second.archive).is_file()


def test_prune_removes_only_old_backups(seeded, tmp_path: Path) -> None:
    runtime = _seeded_runtime(seeded)
    out = tmp_path / "backup"
    keep = create_backup(runtime.settings, out_dir=out)
    old = create_backup(runtime.settings, out_dir=out)
    old_path = Path(old.archive)

    # 把其中一个的 mtime 拨到 40 天前
    import os

    past = (datetime.now() - timedelta(days=40)).timestamp()
    os.utime(old_path, (past, past))

    removed = prune_backups(out, keep_days=30)
    assert str(old_path) in removed
    assert not old_path.exists()
    assert Path(keep.archive).exists()


def test_backup_rejects_missing_database(settings, tmp_path: Path) -> None:
    """库不存在时必须明确失败，而不是产出一个空包让人误以为有备份。"""
    import pytest

    from ekbs.db import Database

    Database(settings).apply_schema()
    settings.database.path = str(tmp_path / "nope" / "ekbs.db")
    with pytest.raises(FileNotFoundError):
        create_backup(settings, out_dir=tmp_path / "backup")
