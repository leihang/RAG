# RUNBOOK：钉钉接入操作流程（EKBS）

> 适用：企业内部应用 + 机器人（Stream 模式）+ H5 微应用免登 + 组织架构同步  
> 目标：机器人能问答、管理台能免登、组织与权限能自动同步  
> **不需要公网 IP / 不需回调地址** —— Stream 是服务器主动外连（`04` §1）

---

## 一、钉钉开放平台侧（管理员在浏览器里做，约 30~60 分钟）

### 1. 创建企业内部应用

钉钉开放平台 <https://open-dev.dingtalk.com> → 应用开发 → 企业内部应用 → 创建应用。  
拿到三样东西并记好（**只显示一次 AppSecret**）：

| 钉钉里的名字        | 配置键                      | 从哪拿          |
| ------------- | ------------------------ | ------------ |
| AppKey        | `DINGTALK_CLIENT_ID`     | 应用凭证页        |
| AppSecret     | `DINGTALK_CLIENT_SECRET` | 应用凭证页        |
| CorpId（企业 ID） | `DINGTALK_CORP_ID`       | 开发者后台 → 企业信息 |

### 2. 开机器人（消息接收选 **Stream 模式**）

应用能力 → 机器人 → 配置：

- 机器人名称/头像；
- **消息接收模式：Stream 模式** ← 关键。选 HTTP 回调就必须有公网地址，本方案不需要；
- 记下 **RobotCode** → 配置键 `DINGTALK_ROBOT_CODE`。

### 3. 申请接口权限（权限管理页，逐项开）

现场已开通的全部权限点、以及够不够拉通讯录，见下文「已开通权限点备忘（2026-10-20）」。  
权限点只决定接口能不能调；**应用可见范围**决定能看到哪些部门的人。范围不含根部门时，部门列表会返回 `50004`。

> 部分权限要企业管理员审批，**提前 2 周提交**（风险 R4）。  
> 改完权限或可见范围后要**发布新版本**，否则线上应用仍用旧范围。

### 4. 配 H5 微应用（管理台免登入口）

应用能力 → 网页应用：

- **首页地址** = 管理台地址（内网域名或 IP，例如 `https://kb.内网域名/admin`）；
- **可信域名 / redirectUri 白名单** = 同一个域名（免登回调校验，不符会报 `INVALID_REDIRECT_URI`）。

### 5. 设可见范围（灰度就用它）

应用 → 可见范围 → 先选「项目组」，再逐步放大到部门、全员。  
（DingTalk 侧控制"谁能看到这个应用"，是本方案唯一的灰度手段 —— 没有独立灰度环境。）

### 6. 发布版本（**必须做，否则认不出用户**）

应用发布 → 版本管理与发布 → 填版本说明 → 提交 → 管理员审批通过。

> ⚠️ **最常见的坑**：机器人**未发布线上版本**时，回调里**不带 `senderStaffId`**，  
> 系统无法识别用户，会回「未找到你的企业账号」。所以"能问答"的前提是**已发版 + 可见范围包含该用户**。

---

## 二、服务器侧（在 Mac mini 上做）

### 7. 写凭据

`~/.ekbs/secrets.env`（权限 600，绝不进 git）：

```bash
DINGTALK_CLIENT_ID=CHANGE_ME_AppKey
DINGTALK_CLIENT_SECRET=CHANGE_ME_AppSecret
DINGTALK_CORP_ID=CHANGE_ME_CorpId
DINGTALK_ROBOT_CODE=CHANGE_ME_RobotCode
EKBS_INTERNAL_TOKEN=$(openssl rand -hex 32)
EKBS_JWT_SECRET=$(openssl rand -base64 48)
```

### 8. 出网放行

| 域名                                            | 用途                           |
| --------------------------------------------- | ---------------------------- |
| `*.dingtalk.com` / `*.aliyuncs.com`           | OpenAPI、Stream 长连接           |
| `g.alicdn.com`                                | H5 端钉钉 JSAPI（免登取 authCode 用） |
| `dashscope.aliyuncs.com` / `api.deepseek.com` | 云端模型                         |

走企业代理时：**代理必须支持 WebSocket 长连接**（心跳 60s，`timeout ≥ 1h`），  
否则 Stream 会反复掉线（风险 R5）。

### 9. 起服务并验证

```bash
launchctl list | grep ekbs            # 4 个服务都在
curl -s localhost:8000/readyz         # 应 ready
python -m ekbs.tools.sync_org         # 拉一次组织架构（应看到 depts/users 数量）
```

验证清单（缺一不可）：

- [ ] 钉钉里 **@机器人** 提问 → 有回答、带引用；
- [ ] 钉钉里打开管理台 → **自动免登**进入（若提示缺配置，看 `/auth/dingtalk-config` 的 `missing` 字段）；
- [ ] 服务器本机 `python -m ekbs.tools.local_account create --username admin --generate` → 本机账号登录可用；
- [ ] 组织同步后，`GET /perm/me` 能看到正确的 `visible_shards`。

---

## 已开通权限点备忘（2026-10-20）

企业管理员在该企业内部应用上开通的全部权限点如下。对照 `docs/spec/50-dingtalk.md` §5.1：拉通讯录用的是旧版 `topapi/v2/*`，不走新版 `Contact.User.Read`。

| 权限点                           | 页面上的用途         | 和通讯录的关系                                                |
| ----------------------------- | -------------- | ------------------------------------------------------ |
| `qyapi_base`                  | 调用企业 API 的基础权限 | 调任何旧版接口的前提，需要                                          |
| `qyapi_get_department_list`   | 通讯录部门信息读权限     | **需要**。部门列表 `department/listsub`、部门详情 `department/get` |
| `qyapi_get_department_member` | 通讯录部门成员读权限     | **需要**。部门用户详情 `user/list`（按部门把人列出来）                    |
| `qyapi_get_member`            | 成员信息读权限        | **需要**。单个用户详情 `user/get`（职位、部门列表）。只开这一项拉不了全员           |
| `qyapi_robot_sendmsg`         | 机器人发消息         | 问答回复用，与通讯录无关                                           |
| `Card.Instance.Write`         | 创建卡片实例         | 卡片回复用，与通讯录无关                                           |
| `Card.Streaming.Write`        | AI 卡片流式更新      | 卡片流式回复用，与通讯录无关                                         |
| `snsapi_base`                 | 免登时取用户身份       | 管理台免登用，与通讯录名单无关                                        |
| `qyapi_blackboard_manage`     | 公告管理           | 本系统未调用                                                 |
| `qyapi_ai_base`               | AI 基础          | 本系统未调用                                                 |
| `qyapi_ocr_structured`        | 结构化 OCR        | 本系统未调用                                                 |

**结论：获取钉钉通讯录够用。** 部门树、部门成员、单个用户详情这三步所需的权限点都在上面。2026-10-20 实测：授权范围已包含根部门，全量登记 **31 个部门、117 人**。

还差、但当前同步不依赖的两项：

- **离职记录接口** `GET /v1.0/contact/empLeaveRecords` 要 `Contact.Common.Read`。现在离职靠事件「通讯录用户离职」，不靠这个接口。
- **手机号、邮箱** 不在这批权限里。用户详情里这两个字段可能是空的。入库只用脱敏手机号，缺了不影响认人和提问。

事件订阅不是权限点，权限管理里没有单独的 `qyapi_*` 代码。事件自己的英文代码如下，推送方式用 **Stream**：

| 事件与回调里的名称 | 事件 code           |
| --------- | ----------------- |
| 企业增加员工事件  | `user_add_org`    |
| 加入企业后用户激活 | `user_active_org` |
| 通讯录用户更改   | `user_modify_org` |
| 通讯录用户离职   | `user_leave_org`  |

事件到达后补拉详情，用的仍是已经开通的 `qyapi_get_member` 和 `qyapi_get_department_list`，不用再申请新权限点。

### 待办：等开发者权限（2026-10-20）

当前账号在开发者后台还不能把下面两件事做完。开通开发者权限后再做，不另申请权限点。

1. **人员变动自动更新。** 事件订阅页推送方式已选 Stream，但「保存」是灰的，事件列表不出现。下一步：点「已完成接入，验证连接通道」→ 保存 → 勾选上面四项通讯录事件 → 再到「版本管理与发布」发版。发版前，入职、离职、职位和部门变化只能在 EKBS 用户管理点「从钉钉更新」。
2. **AI 卡片回复。** `Card.Instance.Write`、`Card.Streaming.Write` 已开通。`config/application.cloud.yaml` 里 `dingtalk.card.ai_template_id` 仍是空的，机器人因此只发纯文本。开发者权限到位后：在开放平台建好 AI 卡片模板，把模板 ID 填进该配置并重启钉钉服务。

---

## 三、日常运维

| 事项      | 怎么做                                                                                |
| ------- | ---------------------------------------------------------------------------------- |
| 组织/人员变更 | 现在只能在用户管理点「从钉钉更新」。自动更新待开发者权限办完事件订阅（见上面待办）                                          |
| 新部门加入   | 先在 `config/application.yaml` 的 `permission.dept_code_map` 加映射（O6-1 未定稿，暂由配置承载），再重启 |
| 密钥轮换    | AppSecret 轮换后改 `secrets.env` → `launchctl kickstart -k` 重启相关服务                     |
| 告警通道    | 另建一个**自定义群机器人**（webhook）用于系统告警，与业务机器人分开                                            |
| 消息可靠性   | D16：消息先落库再 ACK；崩溃/发布窗口由恢复扫描补处理（不丢消息）                                               |
| 常见故障    | 见下表                                                                                |

### 排错速查

| 现象             | 先查                                                                               |
| -------------- | -------------------------------------------------------------------------------- |
| 机器人不回复         | `stream_enabled` 是否为 true；Stream 连接状态 `/readyz`；出网代理是否切断长连接                      |
| 回「未找到你的企业账号」   | 应用**是否已发线上版本**；可见范围是否含该用户；`senderStaffId` 是否为空                                   |
| 免登进不去          | `DINGTALK_CORP_ID/CLIENT_ID/SECRET` 是否齐（看 `/auth/dingtalk-config`）；可信域名是否配了管理台地址 |
| 管理台白屏/JSAPI 报错 | 内网能否访问 `g.alicdn.com`（H5 免登必须放行）                                                 |
| 权限不对（看不到该看的）   | 该部门是否在 `dept_code_map`；用户 `clearance` 是否够；分片是否 active                            |

---

## 四、与本方案其他部分的边界

- **不上公网**：Stream 是主动外连，管理台只给内网/VPN；本机账号登录**默认仅服务器本机**；
- **不做 HTTP 回调**：避免公网暴露面与证书/回调地址维护；
- **凭据只有一个来源**：`~/.ekbs/secrets.env`（plist 不写明文，启动时注入）；
- **免登 ≠ 权限**：钉钉只证明"你是谁"，能看到什么由服务端按部门/密级/ACL 现算。
