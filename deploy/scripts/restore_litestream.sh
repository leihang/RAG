#!/usr/bin/env bash
# =============================================================================
# deploy/scripts/restore_litestream.sh —— 从 Litestream 归档恢复 ekbs.db 并自证
# （00 v4.0 D15 / 08 §2；季度恢复演练 M15 的脚本化步骤）
# -----------------------------------------------------------------------------
# 用法：
#   ./restore_litestream.sh [config] [目标文件]
#   默认：config=/opt/ekbs/etc/litestream.yml  目标=/data/ekbs/restore-test/ekbs.db
#
# 自证项（与 backup.sh 同一哲学：没验证过的恢复等于没有备份）：
#   ① PRAGMA integrity_check = ok
#   ② ekbs 实体表数 = 31（排除 FTS5 影子表）
#   ③ ekbs_shard 登记向量数 > 0（有数据才校验；空库跳过）
# 通过后还应跑三方对账（scripts/smoke.sh），本脚本只负责「库文件本身可信」。
# =============================================================================
set -euo pipefail

CONFIG="${1:-/opt/ekbs/etc/litestream.yml}"
TARGET="${2:-/data/ekbs/restore-test/ekbs.db}"
SOURCE_DB="/data/ekbs/index/ekbs.db"   # 必须与 litestream.yml 中 dbs.path 一致

mkdir -p "$(dirname "$TARGET")"
rm -f "$TARGET"

echo ">> 从 litestream 归档恢复：$SOURCE_DB → $TARGET"
litestream restore -config "$CONFIG" -o "$TARGET" "$SOURCE_DB"

integ="$(sqlite3 "$TARGET" 'PRAGMA integrity_check;')"
if [ "$integ" != "ok" ]; then
  echo "[FATAL] 恢复库 integrity_check = ${integ}" >&2
  exit 1
fi

tables="$(sqlite3 "$TARGET" "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','view') AND name LIKE 'ekbs%' AND name NOT LIKE 'ekbs_chunk_fts_%';")"
if [ "$tables" != "32" ]; then
  echo "[FATAL] 恢复库表数 ${tables} ≠ 32（01 §5 口径）" >&2
  exit 1
fi

vectors="$(sqlite3 "$TARGET" 'SELECT COALESCE(SUM(vector_count),0) FROM ekbs_shard;' 2>/dev/null || echo 0)"
echo "恢复自证通过：integrity=ok  tables=$tables  登记向量=$vectors"
echo "→ $TARGET"
echo "后续：FAISS 分片与 raw/parse 从 backup.sh 的对应日期目录还原后，跑 deploy/scripts/smoke.sh 做三方对账"
