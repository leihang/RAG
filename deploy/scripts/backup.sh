#!/usr/bin/env bash
# =============================================================================
# deploy/scripts/backup.sh —— 本地备份（01-实施步骤与本地部署.md §13）
# -----------------------------------------------------------------------------
# 安装位置建议：/opt/ekbs/etc/backup.sh（install.sh 会拷贝并 chmod +x）
# 调度：crontab 每日 02:00 —— `0 2 * * * /opt/ekbs/etc/backup.sh >> /opt/ekbs/log/backup.log 2>&1`
#
# 目标口径（00 v4.0 §4 / 08 §2 的 3-2-1 分层）：
#   · DB 层 RPO ≤1min —— 由 Litestream 连续归档承担（deploy/litestream/），**不是**本脚本；
#   · 文件层 RPO ≤15min —— 由镜像 rsync 周期任务承担（本脚本第 5 步的日备镜像是兜底）；
#   · 本脚本 = 日备层（第 3 跳保留 30 天）：可回滚窗口与季度恢复演练的材料。
# 保留策略：目录按 YYYY-MM-DD 命名，清理 30 天前（01 §13 第 4 步）。
# 恢复演练：每季度在 /data/ekbs/restore-test/ 还原 ekbs.db + FAISS + 解析产物，
#           启动临时实例跑 deploy/scripts/smoke.sh；litestream 层用 restore_litestream.sh。
#
# 单机最大风险是磁盘故障：第 5 步镜像同步为**必做项**（08 §2），
# 经 EKBS_BACKUP_MIRROR 配置（写在 /opt/ekbs/etc/backup.env 或 crontab 环境里）；
# 同步失败 = 备份失败，脚本非零退出并打 FATAL（07 §7.5：备份失败 1 次即人工介入）。
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
echo "backup done: $B"

# ---- 备份自证（04 §7.6 阶段 2）：没验证过的备份等于没有备份 ----
integ="$(sqlite3 "$B/ekbs.db" 'PRAGMA integrity_check;')"
if [ "$integ" != "ok" ]; then
  echo "[FATAL] 备份库 integrity_check = ${integ}（$B/ekbs.db 不可用）" >&2
  exit 1
fi
rows="$(sqlite3 "$B/ekbs.db" 'SELECT COUNT(*) FROM ekbs_document;' 2>/dev/null || echo 0)"
shards="$(sqlite3 "$B/ekbs.db" 'SELECT COUNT(*) FROM ekbs_shard;' 2>/dev/null || echo 0)"
size="$(du -sh "$B" | awk '{print $1}')"
echo "备份校验通过：integrity_check=ok  document=$rows  shard=$shards  体积=$size"

# 5) 镜像同步（3-2-1 第二跳；**校验通过后才外发**，不同步坏备份）——
#    EKBS_BACKUP_MIRROR 例：/Volumes/Backup/ekbs 或 nas:/volume1/ekbs
#    同步失败 = 备份失败（非零退出 + FATAL），杜绝「本地成功、异地断流 30 天」的静默全损。
if [ -f /opt/ekbs/etc/backup.env ]; then . /opt/ekbs/etc/backup.env; fi
if [ -n "${EKBS_BACKUP_MIRROR:-}" ]; then
  if ! rsync -a --delete "$B" "${EKBS_BACKUP_MIRROR%/}/"; then
    echo "[FATAL] 镜像同步失败：$B → ${EKBS_BACKUP_MIRROR}（3-2-1 第二跳缺失，立即人工介入）" >&2
    exit 1
  fi
  echo "镜像同步完成：$EKBS_BACKUP_MIRROR"
else
  echo "[WARN] 未配置 EKBS_BACKUP_MIRROR，备份只在本机 —— 磁盘故障即全损（08 §2 要求必配）" >&2
fi
