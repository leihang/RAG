"""扫描并清理磁盘上的孤儿残留（`00` §3.1 的存储一致性）。

**2026-09-29 起，正常删除已经不需要它收尾**：`DELETE /docs/documents/{id}` 与批量
``action=delete`` 会一次清掉库里的行、真删向量（``IndexIDMap2.remove_ids``）、
并删掉磁盘原件与 ``parse/<doc_id>/``（见 ``docs/删除文档-涉及位置与清理.md``）。

那这个工具还管什么：

* **失败/中断的上传**留下的 ``tmp/<upload_id>/``；
* **旧版本删过的文档**留下的原件（升级前删除只清库、不动磁盘）；
* **手工塞进 raw 但从未导入**的文件；
* **对账**：``ekbs_shard.vector_count`` 与库里实际可检索块数是否一致
  —— 这是"向量是不是真删了"的唯一可信证据（文件还在 ≠ 向量还在）。

⚠️ 历史提醒：本工具过去的主业是回收**孤儿向量**，因为老索引是 ``IndexFlatIP``、
不支持删除。换成 ``IndexIDMap2`` 之后这条已经不成立，别再照着旧结论去跑
``rebuild_shard`` 回收容量（那是白跑，还会真花钱重嵌一遍）。

**默认只报告不删**，``--delete`` 才动手。
"""


from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ekbs.config import load_settings
from ekbs.db import Database
from ekbs.ingest.retrievable import RETRIEVABLE_PREDICATE
from ekbs.logging import get_logger

log = get_logger(component="tools.gc")


@dataclass
class GcReport:
    raw_orphans: list[str] = field(default_factory=list)
    parse_orphans: list[str] = field(default_factory=list)
    tmp_orphans: list[str] = field(default_factory=list)
    shard_drift: list[dict[str, Any]] = field(default_factory=list)
    deleted_files: int = 0
    freed_bytes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw_orphans": self.raw_orphans,
            "parse_orphans": self.parse_orphans,
            "tmp_orphans": self.tmp_orphans,
            "shard_drift": self.shard_drift,
            "deleted_files": self.deleted_files,
            "freed_bytes": self.freed_bytes,
        }


def _referenced_paths(db: Database) -> set[str]:
    with db.sync_connect() as conn:
        rows = conn.execute("SELECT source_path FROM ekbs_document").fetchall()
    return {str(r["source_path"]) for r in rows}


def _live_doc_ids(db: Database) -> set[int]:
    with db.sync_connect() as conn:
        rows = conn.execute("SELECT doc_id FROM ekbs_document").fetchall()
    return {int(r["doc_id"]) for r in rows}


def scan(settings, db: Database) -> GcReport:
    """只盘点，不动任何文件。"""
    report = GcReport()
    data_dir = Path(settings.data_dir)

    # 1) raw 下无人引用的文件
    referenced = {str(Path(p).resolve()) for p in _referenced_paths(db)}
    raw_root = settings.data_paths()["raw"]
    if raw_root.is_dir():
        for path in sorted(raw_root.rglob("*")):
            if not path.is_file() or path.name == ".DS_Store":
                continue
            if str(path.resolve()) not in referenced:
                report.raw_orphans.append(str(path.relative_to(data_dir)))

    # 2) parse 下文档已不存在的产物目录
    live = _live_doc_ids(db)
    parse_root = settings.data_paths()["parse"]
    if parse_root.is_dir():
        for child in sorted(parse_root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            # 目录名有两种约定：老路径用 doc_id，重解析用 sha-<hash>（无法判定归属，跳过）
            if child.name.isdigit() and int(child.name) not in live:
                report.parse_orphans.append(str(child.relative_to(data_dir)))

    # 3) tmp 下**上传中断留下的临时分片目录**：上传成功时后端会 rmtree，
    #    失败/放弃（用户关掉页面、422 之后没重试）就会残留。实测攒到过 25 个。
    tmp_root = settings.data_paths()["tmp"]
    if tmp_root.is_dir():
        for child in sorted(tmp_root.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                report.tmp_orphans.append(str(child.relative_to(data_dir)))

    # 4) 分片登记向量 vs 实际可检索块（差值 = 孤儿向量）
    with db.sync_connect() as conn:
        rows = conn.execute(
            "SELECT c.shard_id AS shard_id, COUNT(*) AS n FROM ekbs_chunk c "
            f"WHERE {RETRIEVABLE_PREDICATE} GROUP BY c.shard_id"
        ).fetchall()
        actual = {str(r["shard_id"]): int(r["n"]) for r in rows}
        registered = {
            str(r["shard_id"]): int(r["vector_count"])
            for r in conn.execute("SELECT shard_id, vector_count FROM ekbs_shard")
        }
    for shard_id, count in sorted(registered.items()):
        live_count = actual.get(shard_id, 0)
        if count != live_count:
            report.shard_drift.append(
                {
                    "shard_id": shard_id,
                    "registered_vectors": count,
                    "live_chunks": live_count,
                    "orphan_vectors": count - live_count,
                }
            )
    return report


def purge(settings, report: GcReport) -> GcReport:
    """真删报告里的孤儿文件/目录（**不碰数据库、不碰 FAISS**）。"""
    data_dir = Path(settings.data_dir)
    for rel in report.raw_orphans:
        path = data_dir / rel
        if path.is_file():
            report.freed_bytes += path.stat().st_size
            path.unlink()
            report.deleted_files += 1
            # 顺手清掉空掉的 sha 目录，别留下满地空壳
            parent = path.parent
            try:
                if parent != settings.data_paths()["raw"] and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:  # pragma: no cover - 目录非空/权限
                pass
    for rel in report.parse_orphans + report.tmp_orphans:
        path = data_dir / rel
        if path.is_dir():
            report.freed_bytes += sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            shutil.rmtree(path, ignore_errors=True)
            report.deleted_files += 1
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理删除文档后的残留（raw 原文件 / parse 产物 / tmp 上传残留 / 孤儿向量盘点）")
    parser.add_argument("--delete", action="store_true", help="真的删除（默认只报告）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(argv)

    settings = load_settings()
    db = Database(settings)
    db.apply_schema()

    report = scan(settings, db)
    if args.delete:
        purge(settings, report)

    if args.json:
        import json

        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"raw 孤儿文件：{len(report.raw_orphans)} 个")
        for rel in report.raw_orphans[:20]:
            print(f"  - {rel}")
        print(f"parse 孤儿目录：{len(report.parse_orphans)} 个")
        for rel in report.parse_orphans[:20]:
            print(f"  - {rel}")
        print(f"tmp 上传残留目录：{len(report.tmp_orphans)} 个（上传中断留下的临时分片）")
        for rel in report.tmp_orphans[:5]:
            print(f"  - {rel}")
        print("分片向量对账：")
        if not report.shard_drift:
            print("  ✅ 登记向量与实际可检索块一致，没有孤儿向量")
        for row in report.shard_drift:
            print(
                f"  ⚠️ {row['shard_id']}：登记 {row['registered_vectors']} / 实际 {row['live_chunks']}"
                f" → 孤儿向量 {row['orphan_vectors']}，跑 rebuild_shard 回收"
            )
        if args.delete:
            print(f"已删除 {report.deleted_files} 项，释放约 {report.freed_bytes / 1024:.1f} KB")
        elif report.raw_orphans or report.parse_orphans or report.tmp_orphans:
            print("（当前只是报告；加 --delete 才会真删）")

    log.info(
        "gc_done",
        raw_orphans=len(report.raw_orphans),
        parse_orphans=len(report.parse_orphans),
        tmp_orphans=len(report.tmp_orphans),
        shard_drift=len(report.shard_drift),
        deleted=report.deleted_files,
        delete=args.delete,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
