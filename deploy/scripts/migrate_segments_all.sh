#!/usr/bin/env bash
# 把当前 DB 里所有 active 分片迁移到分段布局（dry-run 默认；--apply 真复制）。
# 用法：bash deploy/scripts/migrate_segments_all.sh --config /opt/ekbs/etc/application.yaml [--apply]
set -euo pipefail
CONFIG="/opt/ekbs/etc/application.yaml"
APPLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done
cd "$(dirname "$0")/../.."
DB=$(.venv/bin/python -c "from ekbs.config import load_settings; from pathlib import Path; s=load_settings(Path('$CONFIG')); print(s.db_path)")
for shard in $(sqlite3 "$DB" "SELECT shard_id FROM ekbs_shard WHERE status='active' ORDER BY shard_id;"); do
  echo "== $shard"
  if [ "$APPLY" = "1" ]; then
    .venv/bin/python -m ekbs.tools.migrate_shard_segments --config "$CONFIG" --shard-id "$shard" --apply
  else
    .venv/bin/python -m ekbs.tools.migrate_shard_segments --config "$CONFIG" --shard-id "$shard"
  fi
done
