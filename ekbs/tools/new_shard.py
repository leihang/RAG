"""``python -m ekbs.tools.new_shard`` —— 新建分片（`01` §6.3）。

用法（与 ``deploy/scripts/install.sh`` 的提示一致）：

.. code-block:: bash

    python -m ekbs.tools.new_shard --shard-id kb_hr_L2 --dim 1024
    python -m ekbs.tools.new_shard --dept-code hr --level 2 --dim 1024
"""

from __future__ import annotations

import argparse
import sys

from ekbs.config import load_settings
from ekbs.index.faiss_store import ShardIndexManager
from ekbs.index.shard import make_shard_id
from ekbs.logging import configure_logging, get_logger
from ekbs.provider.registry import build_embedding
from ekbs.config import Secrets
from ekbs.runtime import Runtime

log = get_logger(component="tools.new_shard")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="新建 FAISS 分片索引")
    parser.add_argument("--shard-id", help="分片 ID，如 kb_hr_L2")
    parser.add_argument("--dept-code", help="部门短码，如 hr（与 --level 一起用）")
    parser.add_argument("--level", type=int, help="密级 1..4（与 --dept-code 一起用）")
    parser.add_argument("--dim", type=int, help="覆盖配置里的嵌入维度")
    parser.add_argument(
        "--index-type", default="flat", choices=["flat", "ivf"], help="索引类型（06 §2.3）"
    )
    args = parser.parse_args(argv)

    if args.shard_id:
        shard_id = args.shard_id
    elif args.dept_code and args.level:
        shard_id = make_shard_id(args.dept_code, args.level)
    else:
        parser.error("需要 --shard-id，或同时给出 --dept-code 与 --level")

    settings = load_settings()
    configure_logging(settings, "tools")
    settings.ensure_dirs()
    db_report = Runtime.create(settings, service="tools", init_schema=True)
    embed = build_embedding(settings, Secrets())
    dim = int(args.dim or settings.models.embed.dim)

    manager = ShardIndexManager(
        db_report.db, settings, dim=dim, fingerprint=embed.ref.fingerprint
    )
    index = manager.create_shard(shard_id, index_type=args.index_type)
    print(
        f"已建分片 {shard_id}：dim={index.dim} ntotal={index.vector_count} "
        f"path={index.path}"
    )
    print(f"嵌入指纹 {embed.ref.fingerprint}（{embed.ref.ident}）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
