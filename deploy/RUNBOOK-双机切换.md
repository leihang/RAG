# RUNBOOK：双机热备切换（EKBS v4.0 D14 / `08` §1）

> 适用版本：`00` v4.0 起（2× Mac mini 32GB/1TB active/standby）
> 目标（`00` §4）：**RTO ≤15min、RPO ≤15min**（SQLite 经 Litestream ≤1min）
> **铁律：全流程不自动切换；任何时刻两台机器不得同时对外提供服务。**

---

## 0. 日常形态

| 机器 | 角色 | 运行什么 |
|---|---|---|
| 主机（active） | 对外服务 | 4 个 launchd 服务（api / dingtalk / worker / scheduler）+ litestream 归档 + Caddy |
| 备机（standby） | 待命 | **不运行** ekbs 服务；litestream restore + rsync 同步；`com.ekbs.watchdog` 每 60s 探测主机 |

**备机初始化（一次性）**：
```bash
# 1) 安装代码与依赖
deploy/scripts/install.sh
# 2) 写入节点配置
cp deploy/config/node.env.example /opt/ekbs/etc/node.env && chmod 600 /opt/ekbs/etc/node.env
#    编辑：NODE_ROLE=standby、PEER_HOST=<主机内网 IP>、DNS_SWITCH_CMD（可选）
# 3) **卸载** 4 个业务服务（备机不得对外服务）
for s in api dingtalk worker scheduler; do
  launchctl bootout "gui/$(id -u)/com.ekbs.$s" 2>/dev/null || true
done
# 4) 启动看门狗
cp deploy/launchd/com.ekbs.watchdog.plist ~/Library/LaunchAgents/
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.ekbs.watchdog.plist
# 5) 启动 litestream 恢复侧（见 deploy/litestream/）
```

---

## 1. 故障发现（自动）

- 备机看门狗连续 3 次（间隔 5s）探测主机 `http://<PEER_HOST>:8000/readyz` 失败 →
  写 `ekbs_security_event.peer_unreachable` + 钉钉告警（若配置）+ 日志
  `/opt/ekbs/log/watchdog.out.log`；
- 监控侧另有主机的 `/healthz`、`/readyz`、指标告警（`07` §7.5）；
- **看门狗不会切换**——它只喊人。

---

## 2. 人工确认（3 分钟内）

依次排除「不是主机死了」的情况：

1. 看门狗日志：是否只是备机到主机的网络问题（从第三台机器 ping/curl 主机）；
2. 现场/远程看主机：`launchctl list | grep ekbs`、`/opt/ekbs/log/api.err.log`；
3. 若主机进程在但 `/readyz` 不 ok：**优先修主机**（`launchctl kickstart -k`），不要切换；
4. 确认为真宕机（断电/硬件/系统无法启动）→ 进入切换。

---

## 3. 切换（备机上执行）

```bash
# ① 预检（不动服务）：角色、对端不可达、数据新鲜度
/opt/ekbs/etc/failover.sh --dry-run

# ② 正式切换：启动 4 服务 → 打印/执行 DNS 切换 → 等待 Stream 重连 → smoke.sh
/opt/ekbs/etc/failover.sh
```

- 脚本拒绝在对端仍可达时切换（防双主）；确认为僵尸进程时才用 `--force`；
- DNS：内网记录 TTL 须 ≤60s；有 DNS API 时用 `DNS_SWITCH_CMD` 自动化，否则人工改；
- `smoke.sh` 未通过 → 脚本自动停服务并提示回退，**不得对外宣布恢复**；
- 计时：预检 1~2min + 服务启动 1min + DNS 生效 ≤1min + Stream 重连 ~15s + 冒烟 2min ≈ **6~8min**（预算 15min）。

---

## 4. 切换后

1. 通知业务方恢复；在钉钉群同步事件；
2. 检查 `ekbs_msg_inbox`：切换窗口内的消息由 D16 落库 + 恢复扫描补处理（不应有丢失）；
3. 主机修复后：**先全量 rsync 校验同步，再把它转为备机**（`launchctl bootout` 4 服务 +
   更新 `node.env` 的 `NODE_ROLE=standby`/`PEER_HOST`）；
4. **禁止双写回切**：不得让两台机器同时跑服务后再「切回去」；
5. 记录：切换时间线、RTO 实测值、消息丢失数（应为 0）→ 归档到事件记录。

---

## 5. 演练要求（`00` §8.2 M14）

- 每季度一次计划内切换（含 DNS、Stream 重连、消息补处理全链路）；
- 演练必须实测 RTO/RPO 并写进记录；连续两次不达标的，按 `00` §8.3 触发回迁评估；
- 演练前在钉钉群公告；演练完成后核对三方对账（`smoke.sh`）。
