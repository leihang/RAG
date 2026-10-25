"""把现有单文件分片迁移为「base 段 + manifest」（方案 B / P0，默认 dry-run）。

用法：
    # 预演（只打印计划，不写盘）
    .venv/bin/python -m ekbs.tools.migrate_shard_segments --config config/application.cloud.yaml \
        --shard-id kb_all_L1

    # 真正复制（原 .faiss 保留，切换由 index.layout=segmented 控制）
    .venv/bin/python -m ekbs.tools.migrate_shard_segments --config config/application.cloud.yaml \
        --shard-id kb_all_L1 --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ekbs.config import load_settings
from ekbs.index.segmented import migrate_single_to_segments

REPO = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser(description="单文件分片 → 分段布局（P0 迁移）")
    parser.add_argument("--config", default=os.environ.get("EKBS_CONFIG", "config/application.dev.yaml"))
    parser.add_argument("--shard-id", required=True, help="例如 kb_all_L1 / kb_hr_L2")
    parser.add_argument("--target-dir", default="", help="默认 <data_dir>/index/shards/<shard_id>")
    parser.add_argument("--apply", action="store_true", help="真正复制并写 manifest（默认 dry-run）")
    args = parser.parse_args()

    settings = load_settings(REPO / args.config)
    db_path = Path(settings.db_path)
    if not db_path.is_absolute():
        db_path = REPO / db_path
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT shard_id, faiss_path, dim, embedding_fingerprint, vector_count "
            "FROM ekbs_shard WHERE shard_id = ?",
            (args.shard_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        print(f"分片未登记：{args.shard_id}", file=sys.stderr)
        return 2

    single = Path(str(row["faiss_path"]))
    target = Path(args.target_dir) if args.target_dir else (
        Path(settings.data_dir) / "index" / "shards" / args.shard_id
    )
    plan = migrate_single_to_segments(
        shard_id=args.shard_id,
        single_faiss=single,
        target_dir=target,
        dim=int(row["dim"]),
        embedding_fingerprint=str(row["embedding_fingerprint"]),
        vector_count=int(row["vector_count"]),
        dry_run=not args.apply,
    )
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


def _connect(path: Path):
    import sqlite3

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


if __name__ == "__main__":
    raise SystemExit(main())
