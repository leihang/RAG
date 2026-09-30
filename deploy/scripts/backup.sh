#!/usr/bin/env bash
# =============================================================================
# deploy/scripts/backup.sh —— 本地备份（01-实施步骤与本地部署.md §13）
# -----------------------------------------------------------------------------
# 安装位置建议：/opt/ekbs/etc/backup.sh（install.sh 会拷贝并 chmod +x）
# 调度：crontab 每日 02:00 —— `0 2 * * * /opt/ekbs/etc/backup.sh >> /opt/ekbs/log/backup.log 2>&1`
#
# 目标口径（与 00 §5 / 01 §13 / 02 对齐）：
#   RPO ≤ 24h（日备）；RTO ≤ 2h。不以 5 分钟 RPO 验收。
# 保留策略：目录按 YYYY-MM-DD 命名，清理 30 天前（01 §13 第 4 步）。
# 恢复演练：每季度在 /data/ekbs/restore-test/ 还原 ekbs.db + FAISS + 解析产物，
#           启动临时实例跑 deploy/scripts/smoke.sh。
#
# 单机最大风险是磁盘故障：**必须**另配外接盘/NAS 同步或 Time Machine（01 §13 第 5 步），
# 本脚本第 5 步默认注释掉，请按贵司实际挂载点启用。
#
# 密钥：本脚本**不备份** ~/.ekbs/secrets.env 的明文。若确需异地备份，
#       必须先用公司密钥管理方式加密（01 §13 第 3 步的「密钥加密后再备份」）。
# =============================================================================
set -euo pipefail

D=$(date +%F); B=/data/ekbs/backup/$D; mkdir -p "$B"
# 1) SQLite 一致性备份（必须用 .backup，不能直接 cp WAL 模式的库）
sqlite3 /data/ekbs/index/ekbs.db ".backup '$B/ekbs.db'"
# 2) FAISS 索引与解析产物
rsync -a --delete /data/ekbs/index/ "$B/index/" --exclude '*.db'
rsync -a --delete /data/ekbs/raw/   "$B/raw/"
rsync -a --delete /data/ekbs/parse/ "$B/parse/"
# 3) 配置与密钥（密钥加密后再备份）
cp /opt/ekbs/etc/application.yaml "$B/"
# 4) 清理 30 天前
find /data/ekbs/backup -maxdepth 1 -type d -mtime +30 -exec rm -rf {} +
# 5) 同步到外接盘 / NAS（二选一，必做其一）
# rsync -a "$B" /Volumes/Backup/ekbs/    || true
echo "backup done: $B"

# ---- 以下为交付版新增的「备份自证」步骤（对应 04 §7.6 阶段 2 的验证点）----
# 备份若不可恢复，等于没有备份；这里就地验证，避免到恢复那天才发现文件是坏的。
integ="$(sqlite3 "$B/ekbs.db" 'PRAGMA integrity_check;')"
if [ "$integ" != "ok" ]; then
  echo "[FATAL] 备份库 integrity_check = ${integ}（$B/ekbs.db 不可用）" >&2
  exit 1
fi
rows="$(sqlite3 "$B/ekbs.db" 'SELECT COUNT(*) FROM ekbs_document;' 2>/dev/null || echo 0)"
shards="$(sqlite3 "$B/ekbs.db" 'SELECT COUNT(*) FROM ekbs_shard;' 2>/dev/null || echo 0)"
size="$(du -sh "$B" | awk '{print $1}')"
echo "备份校验通过：integrity_check=ok  document=$rows  shard=$shards  体积=$size"
