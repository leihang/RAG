"""分段索引 compaction：合并小段（方案 B / P2）。

用法：
    .venv/bin/python -m ekbs.tools.compact_shards --config config/application.cloud.yaml
    .venv/bin/python -m ekbs.tools.compact_shards --config ... --shard-id kb_all_L1

只重写被合并的小段；最大段保留。默认参数来自 ``index.segment.compaction_max_segments``。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ekbs.config import load_settings
from ekbs.logging import configure_logging
from ekbs.runtime import Runtime

REPO = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser(description="分段索引 compaction")
    parser.add_argument("--config", default=os.environ.get("EKBS_CONFIG", "config/application.dev.yaml"))
    parser.add_argument("--shard-id", default="", help="只处理指定分片；空=全部 active 分片")
    args = parser.parse_args()
    settings = load_settings(REPO / args.config)
    configure_logging(settings, "tools")
    runtime = Runtime.create(settings, service="tools", init_schema=False)
    try:
        if settings.index.layout != "segmented":
            print(json.dumps({"compacted": False, "reason": "index.layout != segmented"}))
            return 1
        results = (
            runtime.shards.compact_all()
            if not args.shard_id
            else {args.shard_id: runtime.shards.compact(args.shard_id)}
        )
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    finally:
        import asyncio

        asyncio.run(runtime.aclose())


if __name__ == "__main__":
    sys.exit(main())
