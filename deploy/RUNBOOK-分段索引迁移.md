# RUNBOOK：分片索引迁移到「分段布局」（方案 B / P3）

> 适用：把生产分片从单文件 `kb_*.faiss` 迁到 `shards/<shard_id>/{manifest.json, seg_*.faiss}`，
> 并把 `index.layout` 从 `single` 切到 `segmented`。
> 迁移**只复制**原 `.faiss`，不删除；回滚只需把 layout 改回 `single`。

## 0. 前置检查

```bash
# 配置 & 磁盘
grep -A8 '^index:' /opt/ekbs/etc/application.yaml
df -h /data/ekbs              # 至少留出「索引文件总大小」的空闲
ls -lh /data/ekbs/index/*.faiss | tail

# 干跑迁移（不写盘）
.venv/bin/python -m ekbs.tools.migrate_shard_segments \
  --config /opt/ekbs/etc/application.yaml --shard-id kb_all_L1
```

要求：
- 当前 `index.layout` 是 `single`（迁移期间保持单文件读）；
- 已备份 `/data/ekbs/index/ekbs.db` 与 `*.faiss`（至少 `cp -a` 一份）；
- **写向量者只有 worker**：迁移和切换期间不要通过管理台手动入库（或等队列清空）。

## 1. 停止写入（worker）

```bash
sudo launchctl bootout system/com.ekbs.worker || true
# 确认没有 worker 进程：
pgrep -fl ekbs.worker || echo "worker stopped"
```

API 可以继续读；此时不会产生新向量。

## 2. 迁移分片（逐分片，幂等）

```bash
bash deploy/scripts/migrate_segments_all.sh --config /opt/ekbs/etc/application.yaml
# 或单个：
.venv/bin/python -m ekbs.tools.migrate_shard_segments \
  --config /opt/ekbs/etc/application.yaml --shard-id kb_all_L1 --apply
```

检查：

```bash
for d in /data/ekbs/index/shards/*/; do echo "== $d"; cat "$d/manifest.json" | head -20; done
```

## 3. 切换配置并重启

```bash
sudo cp /opt/ekbs/etc/application.yaml /opt/ekbs/etc/application.yaml.bak.$(date +%Y%m%d%H%M)
# 只改一处：index.layout: single → segmented
sudo sed -i '' 's/^  layout: single$/  layout: segmented/' /opt/ekbs/etc/application.yaml
grep -A2 '^index:' /opt/ekbs/etc/application.yaml

sudo launchctl kickstart -k system/com.ekbs.worker
sudo launchctl kickstart -k system/com.ekbs.api
```

## 4. 验证

```bash
# 4.1 启动日志无 fingerprint/shard 加载错误
grep -E 'runtime_ready|shard_load_rejected|segmented' /opt/ekbs/log/*.log | tail -20

# 4.2 分片向量数对账
sqlite3 /data/ekbs/index/ekbs.db \
  "SELECT shard_id, vector_count, status FROM ekbs_shard ORDER BY shard_id;"

# 4.3 冒烟（检索/问答）
bash deploy/scripts/smoke.sh --with-qa

# 4.4 手动上传一篇小文档（管理台，先复核再“手动向量入库”）
#     预期：状态 review → pending_index → indexing → published；
#     日志出现 segmented 段冻结/active 写入，检索能搜到。
```

## 5. 灰度观测（建议 1 个工作日）

| 指标 | 检查 |
|---|---|
| 段数量 | `ls /data/ekbs/index/shards/*/seg_*.faiss | wc -l`，应保持个位数 |
| `pending_index` 积压 | `SELECT COUNT(*) FROM ekbs_document WHERE status='pending_index';` 应能归零 |
| worker 空闲 compaction | 日志 `segmented_compacted` |
| 检索质量 | 与灰度前固定问题集对比 top-k 结果 |
| API 延迟 | 上传后单篇可搜延迟（个人库应秒级） |

## 6. 回滚（分钟级）

```bash
sudo launchctl bootout system/com.ekbs.worker || true
sudo sed -i '' 's/^  layout: segmented$/  layout: single/' /opt/ekbs/etc/application.yaml
sudo launchctl kickstart -k system/com.ekbs.worker
sudo launchctl kickstart -k system/com.ekbs.api
```

原 `.faiss` 文件仍在，`layout=single` 直接读旧文件；不丢数据。
若需彻底清理：确认 segmented 稳定后再删除 `/data/ekbs/index/shards/`。

## 7. 常见问题

| 现象 | 处理 |
|---|---|
| 启动报 manifest fingerprint 不一致 | 说明换过嵌入模型：先切回 single / 重建分片，再迁移 |
| `pending_index` 一直不消化 | worker 未启动或 `index.writer` 不是 worker；检查 launchd + 配置 |
| 段数持续增长 | compaction 未触发：检查 `compaction_max_segments` 与 worker 空闲日志 |
| 单篇上传后搜不到（个人库） | 确认 `personal_max_vectors=1` 且 worker 在处理 `pending`/`pending_index` |
