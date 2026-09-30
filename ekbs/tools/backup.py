"""``python -m ekbs.tools.backup`` / ``restore`` —— 备份与恢复演练（`01` §13）。

DoD 里有两条：「备份任务已执行一次且可恢复（RPO≤24h / RTO≤2h）」。
**没人恢复过的备份只是假设**，所以本模块把两件事放在一起：

* :func:`create_backup`：产 ``backup/ekbs-<YYYYMMDD>.tar.gz``（命名口径 `00` §3.1），
  内含 SQLite 一致性副本 + FAISS 分片 + 非敏感配置 + ``manifest.json``（含各文件 sha256）；
* :func:`restore_and_verify`：解到一个**独立目录**后逐项核对
  （integrity_check / 29 表 / 12 触发器 / policy_version / 每个分片的 dim 与 ntotal / sha256），
  即 `01` §13 的「恢复演练」。

三个容易做错的地方，都在这里按正确做法实现：

1. **SQLite 必须用 ``Connection.backup()``，不能 ``cp``** —— 库是 WAL 模式，
   直接复制会拿到「主库 + 未合并 WAL」的半截状态（`01` §13 特意点了这一条）。
2. **不含密钥**：``~/.ekbs/secrets.env`` 绝不进备份包（`01` §13：密钥加密后再备份）。
3. **manifest 里记 sha256**：否则「恢复成功」只是文件解压成功，证明不了内容没坏。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ekbs.config import Settings, load_settings
from ekbs.db import EXPECTED_TABLES, EXPECTED_TRIGGERS, Database
from ekbs.logging import configure_logging, get_logger

log = get_logger(component="backup")

MANIFEST_NAME = "manifest.json"
#: 明确排除的敏感文件名（双保险：check-secrets.sh 之外的第二道）
FORBIDDEN_NAMES = {"secrets.env", ".env", "id_rsa"}


def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class BackupResult:
    archive: str
    bytes: int
    created_at: str
    files: list[str] = field(default_factory=list)
    shards: int = 0
    pruned: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "archive": self.archive,
            "bytes": self.bytes,
            "created_at": self.created_at,
            "files": len(self.files),
            "shards": self.shards,
            "pruned": self.pruned,
        }


@dataclass
class VerifyReport:
    archive: str
    ok: bool
    checks: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"archive": self.archive, "ok": self.ok, "checks": self.checks,
                "problems": self.problems}


# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------


def _sqlite_consistent_copy(source: Path, target: Path) -> None:
    """用 SQLite 的在线备份 API 取一致性副本（WAL 模式下不能 cp）。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def create_backup(
    settings: Settings,
    *,
    out_dir: Path | None = None,
    keep_days: int = 30,
    now: datetime | None = None,
) -> BackupResult:
    stamp_dt = now or datetime.now()
    stamp = stamp_dt.strftime("%Y%m%d")
    out = out_dir or settings.data_paths()["backup"]
    out.mkdir(parents=True, exist_ok=True)

    archive = out / f"ekbs-{stamp}.tar.gz"
    if archive.exists():  # 同一天多次执行不覆盖已有备份
        archive = out / f"ekbs-{stamp}-{stamp_dt.strftime('%H%M%S')}.tar.gz"

    database = Database(settings)
    report = database.verify_schema()
    if not report.exists:
        raise FileNotFoundError(f"数据库不存在，无法备份：{settings.db_path}")

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "ekbs"
        staging.mkdir(parents=True)
        files: list[str] = []

        # ① SQLite 一致性副本
        db_copy = staging / "index" / "ekbs.db"
        _sqlite_consistent_copy(settings.db_path, db_copy)
        files.append("index/ekbs.db")

        # ② FAISS 分片（只备份登记在册的 active 分片）
        shards: list[dict[str, Any]] = []
        with database.sync_connect() as conn:
            rows = conn.execute(
                "SELECT shard_id, dim, embedding_fingerprint, vector_count, faiss_path "
                "FROM ekbs_shard WHERE status = 'active' ORDER BY shard_id"
            ).fetchall()
            policy_version = conn.execute(
                "SELECT version FROM ekbs_policy_version WHERE id = 1"
            ).fetchone()
        for row in rows:
            source = Path(str(row["faiss_path"]))
            if not source.is_file():
                log.warning("shard_file_missing", shard_id=str(row["shard_id"]))
                continue
            target = staging / "index" / source.name
            shutil.copy2(source, target)
            files.append(f"index/{source.name}")
            shards.append(
                {
                    "shard_id": str(row["shard_id"]),
                    "dim": int(row["dim"]),
                    "embedding_fingerprint": str(row["embedding_fingerprint"]),
                    "vector_count": int(row["vector_count"]),
                    "file": f"index/{source.name}",
                    "sha256": sha256_file(target),
                }
            )

        # ③ 非敏感配置（**不含密钥**）
        config_name = f"config/{settings.config_path.name}"
        (staging / "config").mkdir(parents=True, exist_ok=True)
        shutil.copy2(settings.config_path, staging / config_name)
        files.append(config_name)

        # ④ manifest（含各文件 sha256：恢复时用来证明内容没坏）
        manifest = {
            "created_at": stamp_dt.astimezone(timezone.utc).isoformat(),
            "app": {"name": settings.app.name, "env": settings.app.env},
            "schema": {
                "tables": report.tables,
                "triggers": report.triggers,
                "policy_version": int(policy_version["version"]) if policy_version else None,
                "journal_mode": report.journal_mode,
            },
            "embedding": {
                "model": settings.models.embed.model,
                "dim": settings.models.embed.dim,
            },
            "shards": shards,
            "files": {
                name: sha256_file(staging / name)
                for name in files
                if name != "manifest.json"
            },
        }
        (staging / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # ⑤ 打包（顺带断言没有敏感文件被带进来）
        for path in staging.rglob("*"):
            if path.is_file() and path.name in FORBIDDEN_NAMES:
                raise RuntimeError(f"备份包里出现了敏感文件：{path.name}（密钥绝不进备份）")
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(staging, arcname="ekbs")

    result = BackupResult(
        archive=str(archive),
        bytes=archive.stat().st_size,
        created_at=stamp_dt.isoformat(),
        files=files,
        shards=len(shards),
    )
    result.pruned = prune_backups(out, keep_days=keep_days, now=stamp_dt)
    log.info("backup_created", **result.as_dict())
    return result


def prune_backups(
    out_dir: Path, *, keep_days: int = 30, now: datetime | None = None
) -> list[str]:
    """清理超过保留期的备份文件（`01` §13：保留 30 天）。"""
    if keep_days <= 0:
        return []
    cutoff = (now or datetime.now()).timestamp() - keep_days * 86400
    removed: list[str] = []
    for path in sorted(out_dir.glob("ekbs-*.tar.gz")):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            removed.append(str(path))
    return removed


# ---------------------------------------------------------------------------
# 恢复与演练
# ---------------------------------------------------------------------------


def restore_and_verify(
    archive_path: Path, target_dir: Path, *, clean: bool = True
) -> VerifyReport:
    """把备份解到独立目录并逐项核对（`01` §13 的恢复演练）。"""
    archive_path = Path(archive_path)
    report = VerifyReport(archive=str(archive_path), ok=False)
    if not archive_path.is_file():
        report.problems.append(f"备份文件不存在：{archive_path}")
        return report

    if clean and target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as tar:
        tar.extractall(target_dir)  # noqa: S202 - 备份包由本工具生成

    root = target_dir / "ekbs"
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        report.problems.append("备份包缺少 manifest.json")
        return report
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # ① 文件完整性（sha256）
    bad_files: list[str] = []
    for name, expected in (manifest.get("files") or {}).items():
        path = root / name
        if not path.is_file():
            bad_files.append(f"{name} 缺失")
        elif sha256_file(path) != expected:
            bad_files.append(f"{name} sha256 不符")
    report.checks["files_checked"] = len(manifest.get("files") or {})
    if bad_files:
        report.problems.extend(bad_files)

    # ② 数据库
    db_path = root / "index" / "ekbs.db"
    if not db_path.is_file():
        report.problems.append("备份包缺少 index/ekbs.db")
        return report
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        tables = int(
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','view') "
                "AND name LIKE 'ekbs%' AND name NOT LIKE 'ekbs_chunk_fts_%'"
            ).fetchone()[0]
        )
        triggers = int(
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_%'"
            ).fetchone()[0]
        )
        row = conn.execute("SELECT version FROM ekbs_policy_version WHERE id = 1").fetchone()
        policy_version = int(row["version"]) if row else None
        live_shards = {
            str(r["shard_id"]): (int(r["dim"]), int(r["vector_count"]))
            for r in conn.execute(
                "SELECT shard_id, dim, vector_count FROM ekbs_shard WHERE status='active'"
            ).fetchall()
        }
        # RPO 的核心：备份里的数据必须是「已提交」的最新状态（WAL 也要在）
        messages = int(conn.execute("SELECT COUNT(*) FROM ekbs_qa_message").fetchone()[0])
    finally:
        conn.close()

    report.checks.update(
        integrity=integrity,
        tables=tables,
        triggers=triggers,
        policy_version=policy_version,
        shards_in_db=len(live_shards),
        audit_messages=messages,
    )
    if integrity != "ok":
        report.problems.append(f"integrity_check = {integrity}")
    if tables != EXPECTED_TABLES:
        report.problems.append(f"表数 {tables} ≠ {EXPECTED_TABLES}")
    if triggers != EXPECTED_TRIGGERS:
        report.problems.append(f"触发器数 {triggers} ≠ {EXPECTED_TRIGGERS}")
    expected_policy = (manifest.get("schema") or {}).get("policy_version")
    if expected_policy is not None and policy_version != expected_policy:
        report.problems.append(f"policy_version {policy_version} ≠ manifest {expected_policy}")

    # ③ FAISS 分片：能读、dim/ntotal 与 manifest 一致（这就是 RTO 演练的核心动作）
    shard_checks: list[dict[str, Any]] = []
    try:
        import faiss

        for shard in manifest.get("shards") or []:
            path = root / str(shard["file"])
            entry: dict[str, Any] = {"shard_id": shard["shard_id"], "ok": True}
            try:
                index = faiss.read_index(str(path))
            except Exception as exc:
                entry.update(ok=False, error=f"{type(exc).__name__}: {exc}")
                report.problems.append(f"{shard['shard_id']} 索引读取失败")
                shard_checks.append(entry)
                continue
            entry.update(dim=int(index.d), ntotal=int(index.ntotal))
            if int(index.d) != int(shard["dim"]):
                entry["ok"] = False
                report.problems.append(
                    f"{shard['shard_id']} dim {index.d} ≠ manifest {shard['dim']}"
                )
            if int(index.ntotal) != int(shard["vector_count"]):
                # 陈旧向量会让 ntotal 大于登记值（见 changes.py 的迁移说明），
                # 只有「少于」才是真问题：说明索引丢了向量。
                if int(index.ntotal) < int(shard["vector_count"]):
                    entry["ok"] = False
                    report.problems.append(
                        f"{shard['shard_id']} ntotal {index.ntotal} < manifest "
                        f"{shard['vector_count']}（索引丢向量）"
                    )
                else:
                    entry["stale_vectors"] = int(index.ntotal) - int(shard["vector_count"])
            shard_checks.append(entry)
    except ImportError:  # pragma: no cover - 环境缺少 faiss
        report.problems.append("faiss 不可用，无法校验分片索引")
    report.checks["shards"] = shard_checks

    report.ok = not report.problems
    log.info("restore_verified", **report.as_dict())
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EKBS 备份与恢复演练（01 §13）")
    sub = parser.add_subparsers(dest="command", required=True)

    backup_cmd = sub.add_parser("backup", help="生成备份包")
    backup_cmd.add_argument("--out-dir", help="备份输出目录（默认 data_dir/backup）")
    backup_cmd.add_argument("--keep-days", type=int, default=30)

    restore_cmd = sub.add_parser("restore", help="恢复演练：解包并逐项核对")
    restore_cmd.add_argument("archive")
    restore_cmd.add_argument(
        "--target", default=None, help="演练目录（默认 data_dir/restore-test）"
    )

    args = parser.parse_args(argv)
    settings = load_settings()
    configure_logging(settings, "tools")

    if args.command == "backup":
        result = create_backup(
            settings,
            out_dir=Path(args.out_dir) if args.out_dir else None,
            keep_days=args.keep_days,
        )
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        return 0

    target = Path(args.target) if args.target else (settings.data_dir / "restore-test")
    report = restore_and_verify(Path(args.archive), target)
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    if not report.ok:
        print("恢复演练未通过：", "；".join(report.problems), file=sys.stderr)
        return 1
    print(f"恢复演练通过（解到 {target}）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
