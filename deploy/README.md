# `deploy/` —— macOS 原生部署产物

> **部署步骤的唯一权威在 [`../01-实施步骤与本地部署.md`](../01-实施步骤与本地部署.md)（§1~§16）。**
> `deploy/` 是它的**可执行落地**：把文档里的命令、plist、配置、SQL 变成能直接跑的文件。
> 两者冲突时，以 `01` 为准，并请把 `deploy/` 一起改掉。

---

## 一、文件清单

| 路径 | 行数 | 用途 | 权威来源 |
|---|---|---|---|
| `sql/001_init.sql` | 565 | **唯一 Schema**：29 张表 + 8 个索引 + 1 张 FTS5 虚拟表 + 12 个审计触发器 | `01` §5 |
| `requirements.txt` | 77 | 固定版本的 pip 依赖（分组注释：Web / 数据 / 检索 / 解析 / 测试） | `01` §3 |
| `config/application.yaml` | 163 | **非敏感**配置模板（server / database / retrieval / models / parse / permission / dingtalk / adapter / session / limits / security） | `01` §7 + `04` §5.2 |
| `config/secrets.env.example` | 81 | **敏感项**模板（8 项），复制为 `~/.ekbs/secrets.env` 后须 `chmod 600` | `04` §2.6 |
| `launchd/com.ekbs.api.plist` | 47 | API 服务（`uvicorn ekbs.main:app --workers 1`，127.0.0.1:8000） | `01` §8.1 |
| `launchd/com.ekbs.dingtalk.plist` | 51 | Stream 适配器（127.0.0.1:8100，`KeepAlive=true`） | `01` §8.1 + `04` §7.1.1 |
| `launchd/com.ekbs.worker.plist` | 42 | MinerU 解析 worker（常驻轮询 `ekbs_document.status='pending'`，串行） | `00` D13 + `01` §8.2 |
| `launchd/com.ekbs.scheduler.plist` | 42 | APScheduler 单实例（8 个定时作业） | `00` D13 + `04` §7.1.4 |
| `scripts/install.sh` | 269 | 一体化安装（**幂等**）：目录树 → 配置 → venv → SQLite → launchd → 备份作业 | `01` §1/§3/§5.7/§7/§8.3/§13 |
| `scripts/preflight.sh` | 196 | 上线前静态检查（**只读**）：机型/磁盘、目录、Python、plist、配置硬断言、secrets 权限 | `04` §7.2 + `01` §0.1/§1/§3/§8 |
| `scripts/check-secrets.sh` | 30 | 凭据纪律三条断言（权限 600 / 未被 git 跟踪 / plist 无明文） | `04` §2.6 |
| `scripts/smoke.sh` | 249 | 冒烟集：SQLite + 表数 + 完整性 + FTS5/JSON1 + FAISS 一致性 + 4 个服务 + 健康端点 + Caddy | `01` §14 + `04` §7.3 |
| `scripts/backup.sh` | 45 | 本地备份（`.backup` + rsync + 保留 30 天） | `01` §13 |
| `scripts/release.sh` | 76 | 版本切换与逐个重启（release 目录 + `ln -sfn` + 按序 `kickstart`） | `04` §7.6 |
| `RUNBOOK-回答核验灰度.md` | 96 | 回答后置核验 `answer_verify` 的灰度开启/观测/回滚步骤 | 公开基准报告 §6.5/§6.6 |
| `scripts/answer_verify_gray_check.sh` | 55 | 灰度巡检（**只读**）：配置开关、密钥、近 24h 审计事件、判官失败数、服务状态 | 同上 |
| `RUNBOOK-分段索引迁移.md` | — | 分片从单文件迁到「base 段 + manifest」的迁移/切换/回滚步骤 | 方案 B / P3 |
| `scripts/migrate_segments_all.sh` | — | 遍历 active 分片执行迁移（默认 dry-run，`--apply` 真复制） | 同上 |

---

## 二、执行顺序

```bash
# 0) 先把 secrets 准备好（install.sh 不会替你填密钥）
mkdir -p ~/.ekbs
cp deploy/config/secrets.env.example ~/.ekbs/secrets.env
chmod 600 ~/.ekbs/secrets.env
vim ~/.ekbs/secrets.env                 # 填入 8 项真实凭证

# 1) 安装（幂等，可重复执行）
bash deploy/scripts/install.sh

# 2) 上线前检查（只读；必须 0 个 FAIL）
bash deploy/scripts/preflight.sh

# 3) 冒烟（默认不出网；加 --with-deps / --with-qa 才会真实调用云端）
bash deploy/scripts/smoke.sh
bash deploy/scripts/smoke.sh --with-deps        # 额外验证云端三模型与权限红线
bash deploy/scripts/smoke.sh --with-qa          # 额外做一次真实问答（消耗 token）

# 4) 凭据纪律（CI 与日常巡检都跑）
bash deploy/scripts/check-secrets.sh

# 5) 备份（建议加进 crontab： 0 2 * * * /opt/ekbs/etc/backup.sh >> /opt/ekbs/log/backup.log 2>&1）
bash deploy/scripts/backup.sh

# 6) 发版 / 回滚
bash deploy/scripts/release.sh /opt/ekbs/releases/20260926-1430
# 回滚：ln -sfn /opt/ekbs/releases/<上一版> /opt/ekbs/app && bash deploy/scripts/release.sh /opt/ekbs/releases/<上一版>
```

**退出码约定**：`preflight.sh` 与 `smoke.sh` 以 `0` 表示通过、`1` 表示至少一项 FAIL；
`smoke.sh` 的 `SKIP`（如未配 Caddy 域名）不计入失败。

---

## 三、已在本机验证过的内容

| 验证项 | 命令 | 结果 |
|---|---|---|
| SQL 可执行且幂等 | `sqlite3 :memory: < deploy/sql/001_init.sql` | ✅ 通过；**29 张 `ekbs_*` 对象**（+ 5 张 FTS5 影子表，共 34 行 `sqlite_master`） |
| 审计触发器生效 | 建库后 `UPDATE` / `DELETE` 审计表 | ✅ 12 个触发器全部生效；`ekbs_qa_session` 只允许改 `ended_at` / `message_count`，且 NULL→值 的篡改也会被拦（用 `IS NOT` 而非 `<>`） |
| plist 合法 | `plutil -lint deploy/launchd/*.plist` | ✅ 4/4 OK |
| shell 语法 | `bash -n deploy/scripts/*.sh` | ✅ 6/6 OK |
| 关键配置值 | 抽查 `application.yaml` | ✅ `host: 127.0.0.1`、`workers: 1`、`rerank.top_n: 15` / `keep: 5`、`l4_enabled: false` |

> **尚未验证**（需要真机与凭据，属 `00` §11 的 M1~M6）：`install.sh` 全流程、MinerU 在 macOS 26 的安装与解析、
> 云端三模型连通性、`smoke.sh --with-qa` 的真实问答。**这些是 POC 首周要做的事。**

---

## 四、与文档不一致时怎么办

本目录是 `01`/`04` 的落地，**不新增设计**。若发现两者不一致：

| 情况 | 处置 |
|---|---|
| `deploy/` 里是旧的（文档已改） | 改 `deploy/`，并在 `01`/`04` 的修订记录里记一下 |
| `deploy/` 里是新想法 | **先改文档**（`00` 决策台账或对应文档），再改 `deploy/` —— 不要让脚本成为隐性设计源 |
| 文档本身自相矛盾 | 列出来找架构负责人定，**不要自己选一个然后悄悄改** |

### 已知的文档口径差异（已在 `smoke.sh` 头部标注）

`01` §14 的旧写法用过 `/health/ready`，而 `04` §7.3 定义的是 `/livez`、`/healthz`、`/readyz`、`/health/deps`。
**现以 `04` §7.3 为准**（`01` §8.3 与 §14 已改为 `/readyz`）。
`/health/deps` **会真实出网**（探测云端三模型），因此**绝不能作为 readiness 探针**；探针只用 `/readyz`。

---

## 十二、回答核验灰度（answer_verify）

默认关闭；灰度步骤见 `RUNBOOK-回答核验灰度.md`，巡检用：

```bash
bash deploy/scripts/answer_verify_gray_check.sh
```

只看不改：配置开关、`DEEPSEEK_API_KEY`、近 24h `answer_verify_refused` 审计事件、
`answer_verify_failed` 日志数、`com.ekbs.api` 状态。回滚只需把 `answer_verify.enabled`
改回 `false` 并 `launchctl kickstart -k`。

---

## 十三、LibreOffice 原件渲染（复核页 Excel 左栏）

复核页「原件」对 `.xlsx/.xls` 走 `GET /api/v1/docs/documents/{id}/source-render`：
后端用 **LibreOffice headless** 读原始文件导出 PDF，保留表格样式、合并单元格和
图表（饼图等），再嵌左栏。**不是** openpyxl/SheetJS 重画。

### 安装（二选一，按目标机系统）

```bash
# Debian / Ubuntu
sudo apt-get update
sudo apt-get install -y --no-install-recommends libreoffice fonts-noto-cjk fonts-wqy-zenhei

# macOS（下载 dmg 后把 LibreOffice.app 放到 /Applications）
# https://download.documentfoundation.org/libreoffice/stable/
```

`soffice` 不在 PATH 时，用环境变量指定绝对路径：

```bash
export EKBS_SOFFICE_BIN="/Applications/LibreOffice.app/Contents/MacOS/soffice"
# 或 Linux: /usr/bin/soffice
```

服务启动环境（launchd / systemd）也要带上这个变量；改完重启 `api`。

### 行为

- 首次请求把原件渲染为 PDF，缓存在 `<data_dir>/render/<doc_id>.pdf`；
- 使用 Calc `SinglePageSheets` PDF filter：宽表/图表不会被打印分页切到后续页（旧版 LO 自动回退普通 pdf）；
- 源文件 mtime/size 变化自动重建；
- LibreOffice 未安装/转换失败：接口返回 **503**，前端降级为“下载原件”，不会退回伪 HTML；
- 转换在子进程执行，独立 UserInstallation，超时 240s，避免并发 profile 锁；
- 中文缺字体时安装 `fonts-noto-cjk`/`fonts-wqy-zenhei`，否则 PDF 可能字体回退。

### 验收

```bash
# 用一个有 xlsx 的 doc_id
curl -s -o /tmp/source.pdf \
  -H "X-Internal-Token: $EKBS_INTERNAL_TOKEN" \
  -H "X-Internal-User-Id: 365" \
  "http://127.0.0.1:8000/api/v1/docs/documents/<doc_id>/source-render"
head -c 5 /tmp/source.pdf   # 应为 %PDF-
```

左栏应显示带样式和饼图的 Excel 原件；分块/检索仍用键=值，互不影响。
