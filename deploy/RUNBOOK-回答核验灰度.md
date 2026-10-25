# RUNBOOK：回答后置核验（answer_verify）灰度

> 适用：把 `answer_verify.enabled` 从 `false` 切到 `true`，只影响**生成后置**的"资料充分性"核验；
> 不重建索引、不改数据库、不改检索阈值。默认关闭时行为与现在完全一致。
>
> 决策依据：`docs/公开基准评测报告-2026-10-25.md` §6.5 / §6.6。

## 0. 前置检查

```bash
# 生产机
sudo -u ekbs test -f /opt/ekbs/etc/application.yaml && echo cfg-ok
grep -E '^DEEPSEEK_API_KEY=' ~/.ekbs/secrets.env >/dev/null && echo key-ok || echo "缺 DEEPSEEK_API_KEY"
grep -A14 '^answer_verify:' /opt/ekbs/etc/application.yaml   # 确认存在且 enabled: false
```

要求：
- `~/.ekbs/secrets.env` 有 `DEEPSEEK_API_KEY`（launchd 会注入）；
- `/opt/ekbs/etc/application.yaml` 已有 `answer_verify` 段（本仓库 `deploy/config/application.yaml` 是模板）；
- 当前 `/opt/ekbs/log` 可写。

## 1. 打开开关（只改一行）

```bash
sudo cp /opt/ekbs/etc/application.yaml /opt/ekbs/etc/application.yaml.bak.$(date +%Y%m%d%H%M)
sudo sed -i '' 's/^  enabled: false$/  enabled: true/' /opt/ekbs/etc/application.yaml
# 注意：answer_verify 是文件里最后一个段，且 enabled 只出现一次时才能这样改；
# 改完务必人工确认：
grep -A2 '^answer_verify:' /opt/ekbs/etc/application.yaml
```

> 若同文件里还有别的 `enabled: false`，请用编辑器只改 `answer_verify` 段的 enabled，
> 不要用上面的全局 sed。

## 2. 重启 API（灰度只开问答 API；钉钉链路后跟）

```bash
# macOS launchd（按 deploy/scripts/release.sh 的口径）
sudo launchctl kickstart -k system/com.ekbs.api
sudo launchctl print system/com.ekbs.api | grep -E 'state|pid'
```

如果服务是 gui 域安装的，用 `gui/$(id -u)/com.ekbs.api`；以实际 `launchctl print` 结果为准。

## 3. 观测（至少 1 个工作日 / 2000 次问答）

```bash
# 1) 核验事件（只记审计，不含用户内容）
sqlite3 /data/ekbs/index/ekbs.db "
SELECT event_type, COUNT(*) FROM ekbs_security_event
WHERE created_at > datetime('now','-1 day')
  AND event_type IN ('answer_verify_refused','faithfulness_refused','below_threshold')
GROUP BY event_type;"

# 2) 判官失败/超时
grep -h 'answer_verify_failed\|answer_verify_error' /opt/ekbs/log/*.log | tail -50

# 3) 接口整体拒答率（按需从业务日志/指标里取 refused=true 比例）
```

### 通过标准（建议）

| 指标 | 阈值 | 处理 |
|---|---|---|
| `answer_verify_failed` 占问答量 | < 1% | 超过 → 检查 key/网络/模型名 |
| 判官 p90 延迟 | < 15s | 超过 → 缩小 band（max_score 0.80）或先回滚 |
| 抽样 100 条 `answer_verify_refused` 人工复核 | 误拒 ≤ 5% | 超过 → 回滚并收紧 band |
| 可答问题拒答率相对基线 | +1.5pp 以内 | 超过 → 回滚 |

## 4. 回滚（1 分钟）

```bash
sudo cp /opt/ekbs/etc/application.yaml.bak.<时间戳> /opt/ekbs/etc/application.yaml
sudo launchctl kickstart -k system/com.ekbs.api
```

或直接把 `answer_verify.enabled` 改回 `false` 再 kickstart；**无需重启数据库/重建索引**。

## 5. 灰度期间可选调参

| 现象 | 调整 |
|---|---|
| 误拒偏高 | 收窄 band：`max_score` 0.85→0.80，或把 `enabled` 关掉 |
| 拒答收益不足 | 放宽 band：`max_score` 0.85→0.90（负例分数集中在中段，先看分布再改） |
| 延迟高 | `reasoning_effort` high→low；或只对 0.65–0.80 调用 |
| 判官偶发 429 | 已内置重试；持续则降低调用量（收窄 band） |

## 6. 灰度记录表（填完再决定是否保留）

| 项 | 值 |
|---|---|
| 开启时间 / 执行人 |  |
| 环境与服务 | 生产 `/opt/ekbs`，只开 `com.ekbs.api` |
| 观测窗口 | 例：1 个工作日 / 问答量 N |
| `answer_verify_refused` 计数 / 占问答量 |  |
| 接口整体拒答率（灰度前 → 灰度后） |  /  |
| 抽样复核条数 / 误拒条数 |  /  |
| `answer_verify_failed` 计数 / 失败率 |  /  |
| 判官 p90 延迟 |  |
| 结论 | ☐ 保留 ☐ 调 band 后重试 ☐ 回滚 |
| 回滚时间 / 执行人（如有） |  |

> 判定标准见 §3；回滚步骤见 §4。灰度数据建议同时留一份到本表，便于下次调参对比。
