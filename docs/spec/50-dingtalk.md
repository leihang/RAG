# 50 · 钉钉开放平台接入规格（企业内部应用 + 机器人 Stream 长连接 + 网页应用免登）

> 用途：把「企业内部应用 + 机器人 Stream 长连接 + 网页应用（原 H5 微应用）免登 + 通讯录同步」
> 这一条链路的**真实调用契约**逐字段固定下来，供直接照着写 Python 客户端。
> 本文**只写核实到的事实**；核实不到的集中列在 §7，实施前不得自行拍板。
>
> **核实时点**：2026-09（本会话）。钉钉文档改版频繁，落地前请复核 URL 是否仍可达。
>
> **阅读约定**
> - ✅ = 已在钉钉官方文档 / 官方 SDK 源码中核实到原文；结论后附 URL。
> - ⚠️ = 官方文档**自相矛盾**或与项目假设不同，已注明取哪一份。
> - ❓ = **未核实到**，不得当作事实使用（汇总见 §7）。

---

## 0. 核实方法与来源可信度分级

本次核实**全部基于联网抓取的原文**，不依赖记忆。用到的来源分三级：

| 级别 | 来源 | 说明 |
|---|---|---|
| **A（权威原始文档）** | `https://open.dingtalk.com/document/**/<slug>.md` | 钉钉开放平台中文站。反直觉但关键：该站对爬虫友好的接口是在路径后加 `.md`，返回**纯 Markdown 全文**（不带 `.md` 时是 SPA，抓不到正文）。本文绝大部分结论出自这一级。 |
| **B（官方 SDK 源码）** | `dingtalk-stream==0.24.3` PyPI wheel 解包；仓库 <https://github.com/open-dingtalk/dingtalk-stream-sdk-python> | Stream 的**实际字节级契约**（topic、ACK 报文、心跳、重连、卡片请求体）以 SDK 源码为准，因为文档只讲概念不讲报文。 |
| **C（官方文档镜像）** | `https://help.dingtalk.io/zh/open/development/<slug>.md`；`https://dingtalk.apifox.cn/` | 国际站帮助中心与第三方 Apifox 镜像。**注意 C 级把域名写成 `api.dingtalk.io`**，中国大陆必须用 `api.dingtalk.com`；仅作交叉印证，冲突时以 A 级为准。 |

> ⚠️ **`open.dingtalk.com` 的 `.md` 路由不是官方承诺的公开 API**，只是当前站点实现。脚本化抓取时请留好失败降级。
> ✅ 官方还提供了面向 LLM 的文档索引：<https://open.dingtalk.com/llms.txt>（含 `llms-docs/zh-CN/llms-api-catalog.txt` 全量 API 目录）——这是**查"某能力当前有哪些接口"的最快入口**，建议写进团队书签。

---

## 1. 应用凭证与 access_token

### 1.1 新版 `/v1.0/oauth2/accessToken`（当前推荐，企业内部应用）

**基本信息**

| 项 | 值 |
|---|---|
| HTTP URL | `https://api.dingtalk.com/v1.0/oauth2/accessToken` |
| Method | `POST` |
| Content-Type | `application/json` |
| 支持应用类型 | 企业内部应用 |
| 权限要求 | `permission-qyapi_base`（调用企业 API 时需要具备的基本权限，**无需单独申请**） |

（来源：<https://open.dingtalk.com/document/development/obtain-the-access-token-of-an-internal-app>）

**请求体**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `appKey` | String | 是 | 企业内部应用的 **Client ID**（= 旧版 AppKey） |
| `appSecret` | String | 是 | 企业内部应用的 **Client Secret**（= 旧版 AppSecret） |

**响应体**

| 字段 | 类型 | 示例 | 说明 |
|---|---|---|---|
| `accessToken` | String | `fw8ef8we8f76e6f7s8dxxxx` | 访问凭证。**每个企业内部应用的 accessToken 相互独立**，须按应用维度缓存 |
| `expireIn` | Long | `7200` | 过期时间，单位**秒**。有效期内重复获取返回**相同结果并自动续期**，过期后返回新的 accessToken |

```json
HTTP/1.1 200 OK
Content-Type:application/json
{ "accessToken": "fw8ef8we8f76e6f7s8dxxxx", "expireIn": 7200 }
```

**错误码**：`400 invalidClientIdOrSecret`（无效的 clientId 或 clientSecret）。

**调用侧用法（官方明确写的）**：把 accessToken 缓存到本地（内存/Redis/DB），**缓存过期时间略小于 7200 秒（例如 7000 秒）**；
不可频繁调用本接口取凭证，否则触发限流。
（来源：同页「接口调用说明」；<https://open.dingtalk.com/document/development/server-api-calling-guide> 建议「过期前 10 分钟」刷新）

### 1.2 一个新的、容易被忽略的接口：`/v1.0/oauth2/{corpId}/token`

文档标题也叫「获取应用的 Access Token」，但**不是**旧的 `oapi/gettoken`：

| 项 | 值 |
|---|---|
| HTTP URL | `https://api.dingtalk.com/v1.0/oauth2/{corpId}/token` |
| Method | `POST` |
| 路径参数 | `corpId`（组织 ID，企业内部应用填本企业 corpId） |
| 请求体 | `client_id` / `client_secret` / `grant_type=client_credentials`（**下划线命名**） |
| 响应体 | `access_token` / `expires_in`（**下划线命名**，7200） |

（来源：<https://open.dingtalk.com/document/development/api-gettoken>；国际站同名页 <https://help.dingtalk.io/zh/open/development/api-gettoken>）

⚠️ **本文档与其它页自相矛盾**：该页声称「企业内部应用/第三方企业应用两种场景统一为一个接口」，
但**网页应用免登流程页**恰恰把它写成第 3 步的取凭证接口
（<https://open.dingtalk.com/document/development/enterprise-internal-application-logon-free>），
而**通过免登码获取用户信息页**又让你用**旧版** `obtain-orgapp-token` 拿 `access_token`
（<https://open.dingtalk.com/document/development/obtain-the-userid-of-a-user-by-using-the-log-free>）。
**建议**：机器人/服务端调用统一走 §1.1 的 `/v1.0/oauth2/accessToken`（官方主推、SDK 也用它）；
`/v1.0/oauth2/{corpId}/token` 目前**不要押注**，除非实测通过。

### 1.3 旧版 `oapi.dingtalk.com/gettoken`（历史文档，不推荐）

| 项 | 值 |
|---|---|
| Method / URL | `GET https://oapi.dingtalk.com/gettoken?appkey=xxx&appsecret=xxx` |
| Query | `appkey`（应用唯一标识 key）、`appsecret`（应用密钥） |
| 响应 | `errcode`(`0`) / `errmsg`(`ok`) / `access_token` / `expires_in`(`7200`) |

```
GET https://oapi.dingtalk.com/gettoken?appkey=appkey&appsecret=appsecret
{"errcode":0,"access_token":"96fc7a7axxx","errmsg":"ok","expires_in":7200}
```

**官方原文（关键结论）**：

> 为提升接口使用体验，针对**获取访问凭证**相关接口规范进行升级，从旧版升级到新版。
> 本文旧版规范接口文档**已于 2023 年 8 月 17 日迁移至历史文档（不推荐）目录**，
> 且本接口仅保持现有功能，不再新增支持其他能力。

（来源：<https://open.dingtalk.com/document/development/obtain-orgapp-token>）

**新版 vs 旧版字段差异（逐字段）**

| 维度 | 旧版 `oapi/gettoken` | 新版 `/v1.0/oauth2/accessToken` |
|---|---|---|
| 传输位置 | URL Query（GET） | JSON Body（POST） |
| 请求字段名 | `appkey` / `appsecret`（全小写） | `appKey` / `appSecret`（驼峰） |
| 鉴权方式 | 无 Header | 无 Header（凭证即 Body，无需额外 token） |
| 响应包裹 | `errcode`/`errmsg` 包裹，**token 在顶层** | 无包裹，**直接** `accessToken`/`expireIn` |
| Token 字段 | `access_token`（下划线） | `accessToken`（驼峰） |
| 有效期字段 | `expires_in` | `expireIn` |
| 使用方式 | `?access_token=xxx` 拼 URL | Header `x-acs-dingtalk-access-token: xxx` |
| 错误结构 | `{"errcode":40013,"errmsg":"..."}` | HTTP 400 + `invalidClientIdOrSecret` |
| 状态 | 历史文档，不推荐，不新增能力 | **当前推荐** |

> 适用差异的根本原因：`errcode/errmsg` + URL 带 `access_token` 是**旧版规范**，
> `code/message` + `x-acs-dingtalk-access-token` 头是**新版规范**，两套规范的接口长期并存，
> **新接口要用新 token 头，老接口（如 `topapi/v2/*`）要用 URL 上的 `access_token`**。
> （来源：<https://open.dingtalk.com/document/development/differences-between-server-apis-and-new-server-apis>）

### 1.4 有效期 / 刷新策略 / 限流

| 项 | 结论 | 依据 |
|---|---|---|
| accessToken 有效期 | **7200 秒**（2 小时）；有效期内重复获取**返回相同 token 并自动续期** | <https://open.dingtalk.com/document/development/obtain-the-access-token-of-an-internal-app> |
| 推荐刷新 | 缓存 TTL 设 7000s（官方）；或过期前 10 分钟刷新（调用指南） | 同上 + <https://open.dingtalk.com/document/development/server-api-calling-guide> |
| **每日调用次数上限** | ❓ **未核实到任何"每日"次数上限的官方表述** | — |
| 实际存在的配额口径 | ① **每月调用量**：钉钉标准版**累计 1 万次/月**；专业版 50 万次/月；专属版 500 万次/月。写在各接口文档的「接口调用量说明」里 | <https://open.dingtalk.com/document/development/interface-for-creating-a-card-instance>（卡片接口「接口调用量说明」原文） |
| ② 全局限流 | 以**接口**为单位跨全部企业/应用统计，超限返回 **`errcode 90002`**，所有调用方都失败 | <https://open.dingtalk.com/document/development/call-frequency-limit> |
| ③ IP 维度限流 | 单个公网出口 IP 对**所有接口**合计 **20 秒内最多 10000 次**；触发后该 IP 被禁 **5 分钟**，且**不返回标准 JSON**（返回 HTML 或 `{"status":1111,"wait":5,"source":"x5","punish":"deny","uuid":"xxx"}`） | 同上 |
| 基础接口是否占月配额 | 「不纳入每月调用量限制的接口清单」包含**获取访问凭证、身份验证（免登）、通讯录**等基础接口 | ⚠️ 仅从官方文档索引摘要读到（<https://help.dingtalk.io/zh/open/development/basic-interfaces>），**未逐条核实清单内容** |

> 结论：**"每天多少次"这个提问口径在官方文档里不存在**，请改用「每月调用量（版本相关）+ 90002 全局限流 + IP 20s/10000 次」三个维度做容量评估。

### 1.5 `robotCode`：发消息时**必填**，且对内部应用机器人就等于 AppKey

| 接口 | `robotCode` 是否必填 | 原文 |
|---|---|---|
| `POST /v1.0/robot/oToMessages/batchSend` | **是** | 「机器人的编码，该参数**必须使用企业内部应用机器人的 robotCode**」 |
| `POST /v1.0/robot/groupMessages/send` | **是** | 「机器人的编码」 |
| `POST /v1.0/robot/messageFiles/download` | **是** | 「机器人的编码」 |
| `POST /v1.0/card/instances/deliver`（群场域） | 是（用 `imGroupOpenDeliverModel` 时） | 「**非场景群的企业内部开发的机器人发送群聊，使用机器人的 AppKey**」 |

（来源：<https://open.dingtalk.com/document/development/chatbots-send-one-on-one-chat-messages-in-batches>、
<https://open.dingtalk.com/document/development/the-robot-sends-a-group-message>、
<https://open.dingtalk.com/document/development/download-the-file-content-of-the-robot-receiving-message>、
<https://open.dingtalk.com/document/development/delivery-card-interface>）

**与 AppKey 的关系**：机器人 ID 表中，**企业内部应用/第三方应用机器人的 ID 就是 `robotCode`**
（<https://open.dingtalk.com/document/development/development-robot-overview>）；
卡片投放文档进一步明确**非场景群的企业内部机器人用 AppKey**。
官方 Python SDK 也正是这么用的——`reply_card` / `create_and_send_card` 里一律取
`self.dingtalk_client.credential.client_id` 当 `robotCode`
（来源：`dingtalk_stream/chatbot.py`、`dingtalk_stream/card_replier.py`，SDK 0.24.3）。

⚠️ 但要注意两处文档不一致：`robot/oToMessages/batchSend` 页的 `userIds` 写「每次最多传 **20** 个」，
同页错误码表却写「超过最大限制 **100**，请拆成不超过 100 的数组」；`groupMessages/send` 的
`openConversationId` / `robotCode` 在 `orgapp` 版 slug 下标「否」，在 `development` 版 slug 下标「是」。
**取严**：按 20 个/次分批，`robotCode` 与 `openConversationId` 一律传。

---

## 2. 机器人 Stream 模式（本项目唯一使用的接入方式）

### 2.1 Python SDK：当前版本与最小可用代码

| 项 | 结论 |
|---|---|
| 包名 | `dingtalk-stream`（import 名 `dingtalk_stream`） |
| **当前版本** | **0.24.3**（本机 `pip download` 实测拿到的就是它；本项目 `requirements.txt:52` 也钉在 `dingtalk-stream==0.24.3`，✅ 一致） |
| 依赖 | `aiohttp>=3.10.0`、`requests>=2.27.1`、`websockets>=11.0.2` |
| 发布页 | <https://pypi.org/project/dingtalk-stream/> |
| 仓库 | <https://github.com/open-dingtalk/dingtalk-stream-sdk-python>（官方文档「其他语言支持」里点名此仓库：<https://open.dingtalk.com/document/development/develop-stream-mode-push-server>） |
| 官方示例（AI 卡片流式） | <https://open.dingtalk.com/document/development/typewriter-effect-streaming-ai-card> 正文给出可直接运行的 `ai_card.py`（`pip install dingtalk_stream loguru`） |

**凭据类名（逐字）**：`dingtalk_stream.Credential(client_id, client_secret)`
（`dingtalk_stream/credential.py`：`class Credential: def __init__(self, client_id, client_secret)`）。

**最小可用代码（按 SDK 0.24.3 源码逐句对齐）**

```python
import asyncio, logging
import dingtalk_stream
from dingtalk_stream import AckMessage, ChatbotMessage

async def main():
    credential = dingtalk_stream.Credential(CLIENT_ID, CLIENT_SECRET)   # client_id=AppKey, client_secret=AppSecret
    client = dingtalk_stream.DingTalkStreamClient(credential)

    # 机器人消息回调：topic 固定 '/v1.0/im/bot/messages/get'
    client.register_callback_handler(ChatbotMessage.TOPIC, MyChatbotHandler())
    # 卡片按钮/交互回调：topic 固定 '/v1.0/card/instances/callback'
    client.register_callback_handler(dingtalk_stream.Card_Callback_Router_Topic, MyCardHandler())
    # 事件订阅（通讯录变更等）：订阅 EVENT topic '*'
    client.register_all_event_handler(MyEventHandler())

    client.start_forever()      # 阻塞；内部自带断线重连（见 2.4）

class MyChatbotHandler(dingtalk_stream.ChatbotHandler):
    async def process(self, callback: dingtalk_stream.CallbackMessage):
        incoming = ChatbotMessage.from_dict(callback.data)
        ...
        return AckMessage.STATUS_OK, 'OK'      # ← ACK 方式：返回 (code, message) 二元组
```

**topic 常量对照（✅ 官方文档与 SDK 双向印证）**

| 用途 | topic 字符串 | SDK 常量 | 依据 |
|---|---|---|---|
| 机器人消息回调 | `/v1.0/im/bot/messages/get` | `ChatbotMessage.TOPIC` | <https://open.dingtalk.com/document/development/develop-stream-mode-push-server>（原文「机器人回调名称，固定值 `/v1.0/im/bot/messages/get`」）+ SDK `chatbot.py:159` |
| 卡片回调 | `/v1.0/card/instances/callback` | `Card_Callback_Router_Topic`（`card_callback.py`） | <https://open.dingtalk.com/document/development/event-callback-card>（原文「卡片回调的 topic，固定值 `/v1.0/card/instances/callback`」）+ SDK |
| 机器人消息**委托** | `/v1.0/im/bot/messages/delegate` | `ChatbotMessage.DELEGATE_TOPIC` | 仅 SDK 源码（❓ 未见文档说明用途） |
| 事件订阅 | 订阅时上报 `{'type':'EVENT','topic':'*'}` | `register_all_event_handler()` | SDK `stream.py:158-161` |

### 2.2 ACK：返回什么对象、报文长什么样

**结论（SDK 源码级）**：处理器 `process()` 返回 **`(code, message)` 二元组**；
SDK 在 `CallbackHandler.raw_process()` 里把它包成 `AckMessage` 并通过 **WebSocket 原路发回**。

```python
# dingtalk_stream/handlers.py
async def raw_process(self, callback_message: CallbackMessage):
    code, message = await self.process(callback_message)
    ack_message = AckMessage()
    ack_message.code = code
    ack_message.headers.message_id = callback_message.headers.message_id   # 必须回填 messageId
    ack_message.headers.content_type = Headers.CONTENT_TYPE_APPLICATION_JSON
    ack_message.data = {"response": message}      # ← CallbackMessage 用 data={"response": message}
    return ack_message
```

**实际发回的 JSON（`frames.py: AckMessage.to_dict()`）**

```json
{
  "code": 200,
  "headers": { "messageId": "<回调的 messageId>", "contentType": "application/json" },
  "message": "",
  "data": "{\"response\": \"OK\"}"      // 注意：data 是 json.dumps 后的字符串
}
```

**可用状态码**：`AckMessage.STATUS_OK = 200`、`STATUS_BAD_REQUEST = 400`、
`STATUS_NOT_IMPLEMENT = 404`、`STATUS_SYSTEM_EXCEPTION = 500`。

> **事件（EVENT）与回调（CALLBACK）的 ACK 报文不同**：事件路径把业务数据回填到 `message`，
> 且 `data = event_message.data`（原样回传事件体）。这是 `EventHandler.raw_process()` 与
> `CallbackHandler.raw_process()` 的差异，写通用处理器时别混用。
> 官方 Java SDK 的等价语义是 `EventAckStatus.SUCCESS / LATER`（成功 / 稍后重试）：
> <https://open.dingtalk.com/document/development/develop-stream-mode-push-server>

**长连接建立过程（写客户端/排障必读）**

1. `POST https://api.dingtalk.com/v1.0/gateway/connections/open`
   body：`{"clientId","clientSecret","subscriptions":[{"type":"CALLBACK","topic":"…"},{"type":"EVENT","topic":"*"}],"ua":"…","localIp":"…"}`
   → 响应 `{"endpoint": "...", "ticket": "..."}`
2. `websockets.connect(f'{endpoint}?ticket={quote_plus(ticket)}')`
3. 之后钉钉经该 WebSocket 下发 `SYSTEM` / `EVENT` / `CALLBACK` 三类报文；`SYSTEM` 里 topic 为 `disconnect` 时 SDK 会**主动关闭连接并重连**。

（来源：SDK `stream.py:28, 148-181, 110-137`；域名可被环境变量 `DINGTALK_OPENAPI_ENDPOINT` 覆盖，默认 `https://api.dingtalk.com`，见 `utils.py`）

### 2.3 回调里怎么取字段

`ChatbotMessage.from_dict(callback.data)` 之后可用字段（camelCase → snake_case，✅ SDK `chatbot.py:190-252`）：

| 钉钉报文字段 | SDK 属性 | 说明（文档口径） |
|---|---|---|
| `msgId` | `incoming.message_id` | **加密的消息 ID**，幂等去重就用它 |
| `senderStaffId` | `incoming.sender_staff_id` | **发送者的 userid**。原文：「企业内部群中@该机器人的成员 userid」**且「该字段在机器人发布线上版本后，才会返回」** |
| `senderId` | `incoming.sender_id` | 加密的发送者 ID。原文注明「使用 `senderStaffId` 作为发送者 userid 值」→ **不要用 `senderId` 当 userid** |
| `conversationId` | `incoming.conversation_id` | 会话 ID（单聊/群聊都有） |
| `conversationType` | `incoming.conversation_type` | **字符串** `"1"`=单聊，`"2"`=群聊 |
| `conversationTitle` | `incoming.conversation_title` | 群聊才有 |
| `msgtype` | `incoming.message_type` | `text` / `picture` / `richText` / `audio` / `video` / `file` |
| `text.content` | `incoming.text.content` | 文本内容（`TextContent`，仅 `msgtype == 'text'` 时被解析） |
| `isInAtList` | `incoming.is_in_at_list` | 是否在 @ 列表中 |
| `atUsers` | `incoming.at_users` | `[{dingtalkId, staffId, unionId}]`；外部群中的外部用户 `staffId` 为空 |
| `sessionWebhook` | `incoming.session_webhook` | 当前会话的 Webhook 地址（回复即 POST 它） |
| `sessionWebhookExpiredTime` | `incoming.session_webhook_expired_time` | Webhook 过期时间（毫秒时间戳） |
| `senderNick` / `isAdmin` / `createAt` / `robotCode` / `chatbotUserId` / `chatbotCorpId` / `senderCorpId` | 同名 snake | 见文档参数说明 |
| `content.downloadCode`（picture） | `incoming.image_content.download_code` | 图片下载码 |
| `content.richText`（richText） | `incoming.rich_text_content.rich_text_list` | 富文本列表 |
| **`senderUnionId`** | ❌ **SDK 未解析** | 文档正文列了 `senderUnionId`（发送人 unionid），但 **SDK 0.24.3 的 `from_dict` 没有这个分支** → 会落进 `incoming.extensions['senderUnionId']`。要用 unionid 得自己从 `extensions` 取 |

（字段语义依据：<https://open.dingtalk.com/document/development/receive-message>；
SDK 解析依据：`dingtalk_stream/chatbot.py`）

### 2.4 ACK 超时、心跳、断线重连

| 问题 | 结论 |
|---|---|
| **ACK 超时时间** | ❓ **未核实到**。官方文档（Stream 模式、开发事件推送服务、接收消息、事件回调）**全文没有给出回调 ACK 的秒级超时值，也没写超时后果**。实测口径只有 SDK 行为（下条） |
| SDK 的 ACK 触发时机 | 每条报文由 `asyncio.create_task(self.background_task(json_message))` **异步**处理（`stream.py:79`），处理多久都不会阻塞下一条；**没有 ACK 定时器，也没有超时重发**。业务侧若处理慢，SDK 不会报错，钉钉侧是否重投❓未核实到 |
| **心跳 / 保活** | ✅ SDK 自建：连接建立后启动 `keepalive(ws, ping_interval=60)`，**每 60 秒发一次 WebSocket ping**（`stream.py:94-100`） |
| **断线重连** | ✅ SDK 自建（`stream.py:60-92`、`139-146`）：<br>① `open_connection()` 失败 → `sleep(10)` 后重试；<br>② `ConnectionClosedError` / `CancelledError` → `sleep(10)` 后重连；<br>③ 其它异常 → `sleep(3)` 后重连；<br>④ 收到 `SYSTEM topic=disconnect` → 主动 `websocket.close()` 触发重连；<br>⑤ `start_forever()` 外层 `while True: asyncio.run(self.start())` + `finally: time.sleep(3)` |
| 官方对重连的建议 | 「一个企业内部应用**仅需配置一个 Stream 客户端**」；可以启动多个监听（但没必要）。（<https://open.dingtalk.com/document/development/develop-stream-mode-push-server>） |
| 官方错误码 | `20001`「受调用量超量影响，当前我的消息服务已经暂停…接收消息中新增 `errMsg` 字段」，此时回调体**没有 text/content**（同上） |

> **工程结论**：ACK 超时不能靠文档，只能靠「快速 ACK + 业务异步化」这条经验规则兜底；
> 本项目 `方案/04` §5.4 的「<10s 内 ACK」是**项目自定阈值，不是官方数字**，文档里没有依据。

### 2.5 群聊里怎么区分「@机器人」与普通消息

| 项 | 结论 | 依据 |
|---|---|---|
| 投递规则 | 原文：「当用户 **@群机器人** 或**与机器人发送单聊消息**时，钉钉会通过机器人开发者的服务地址把消息内容发送出去」→ **群聊只有被 @ 才会投递** | <https://open.dingtalk.com/document/development/receive-message> |
| `conversationType` | `"1"` = 单聊；`"2"` = 群聊（**字符串，不是数字**） | 同上 |
| 判定「被 @」 | `isInAtList == true`（是否在 @ 列表中）；`atUsers[]` 给出被 @ 的人（含 `staffId`/`unionId`；外部用户 `staffId` 为空） | 同上 |
| @所有人 | 卡片投放的 `atUserIds` 里 **key 和 value 都为 `@ALL`** 表示 @所有人 | <https://open.dingtalk.com/document/development/delivery-card-interface> |
| **"即使 `isInAtList == false` 也能收到群消息"** | ❓ **未核实到**。文档字面是「@群机器人」才投递；`isInAtList` 的存在暗示可能有非 @ 投递路径（如 @所有人、会话内连续对话），但**没有任何文档支持这一说法** | — |
| 单聊/群聊支持的消息类型差异 | 群聊中@机器人时**不支持**语音/文件/视频；人与人会话中机器人**不支持**语音/文件/视频；**人与机器人单聊支持**语音/文件/视频 | <https://open.dingtalk.com/document/development/receive-message> |

---

## 3. 机器人发消息

### 3.1 单聊批量发送 `POST /v1.0/robot/oToMessages/batchSend`

| 项 | 值 |
|---|---|
| URL / Method | `https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend`，`POST` |
| 鉴权 Header | `x-acs-dingtalk-access-token: <企业应用 accessToken>`（**必填**） |
| 权限 | API Explorer `robot_1.0#BatchSendOTO`；权限名「企业内机器人发送消息权限」 |
| 限制 | 向**最多 20 个**用户批量发送 |

**请求体**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `robotCode` | String | 是 | 企业内部应用机器人的 robotCode |
| `userIds` | Array\<String\> | 是 | 接收者 userId 列表，**每次最多 20 个** |
| `msgKey` | String | 是 | 消息模板 key（取值见 §3.3） |
| `msgParam` | String | 是 | 消息模板参数，**JSON 序列化后的字符串** |

**响应体**

| 字段 | 类型 | 说明 |
|---|---|---|
| `processQueryKey` | String | 消息 ID，用于查已读 / 撤回 |
| `invalidStaffIdList` | Array\<String\> | 无效的 userId 列表 |
| `flowControlledStaffIdList` | Array\<String\> | **被限流的 userId 列表**（部分成功是可能的，必须检查） |

（来源：<https://open.dingtalk.com/document/development/chatbots-send-one-on-one-chat-messages-in-batches>）

### 3.2 群聊发送 `POST /v1.0/robot/groupMessages/send`

| 项 | 值 |
|---|---|
| URL / Method | `https://api.dingtalk.com/v1.0/robot/groupMessages/send`，`POST` |
| 鉴权 Header | `x-acs-dingtalk-access-token` |
| 权限 | `robot_1.0#OrgGroupSend` |
| **注意** | 文档原文：**「接口暂不支持 @ 功能」**；需要 @ 就用卡片（`cardAtUserIds` / `atUserIds`） |

**请求体**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `msgParam` | String | 是 | **长度限制 15000 字节以内** |
| `msgKey` | String | 是 | 消息模板 key |
| `openConversationId` | String | 是 | 会话 ID（`orgapp` 版 slug 标"否"，`development` 版标"是"，**取严传**） |
| `robotCode` | String | 是 | 机器人编码 |
| `coolAppCode` | String | 否 | 酷应用方式安装机器人时**必须**传 |

**响应体**：`processQueryKey`（加密消息 ID，用于查已读 / 撤回）。

（来源：<https://open.dingtalk.com/document/development/the-robot-sends-a-group-message>）

### 3.3 消息模板 key（`msgKey`）与内容限制

| 消息类型 | msgKey |
|---|---|
| 文本 | `sampleText` |
| Markdown | `sampleMarkdown` |
| 图片 | `sampleImageMsg` |
| 链接 | `sampleLink` |
| ActionCard | `sampleActionCard`、`sampleActionCard2`…`sampleActionCard6` |
| 语音 | `sampleAudio` |
| 文件 | `sampleFile` |
| 视频 | `sampleVideo` |

（来源：<https://open.dingtalk.com/document/development/robot-message-type>；`sampleMarkdown` 的 Markdown 语法子集另有补充说明在同页）

**长度/频率限制**

| 项 | 值 | 依据 |
|---|---|---|
| `msgParam`（群聊） | **≤ 15000 字节** | groupMessages/send 页 |
| `msgParam`（单聊） | ❓ 未在该页给数值，**按 15000 字节从严控制** | — |
| `userIds` | ≤ 20 个/次（错误码表写 100，⚠️ 文档不一致，取严） | batchSend 页 |
| 机器人发消息频率 | ❓ **未核实到"每分钟/每机器人"的官方限值**；只有 §1.4 的三种限流维度 + 响应里的 `flowControlledStaffIdList` | — |
| 卡片 `cardParamMap` | key ≤ 100 字节，value ≤ **1KB**；只支持 String（非 String 见「API 卡片数据的填写说明」） | <https://open.dingtalk.com/document/development/interface-for-creating-a-card-instance> |

### 3.4 互动卡片：创建 → 投放 → 更新（普通卡片）

**步骤 1｜创建卡片实例** `POST https://api.dingtalk.com/v1.0/card/instances`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `cardTemplateId` | String | 是 | 卡片模板 ID（开发者后台 > 卡片平台） |
| `outTrackId` | String | 是 | **开发者自己生成**的外部卡片实例 ID；换模板/数据要换新 outTrackId 否则不生效 |
| `cardData` | Object | 是 | `{"cardParamMap": {"key": "value"}}`，value 只支持 String |
| `userId` | String | 否 | 卡片创建者 userId |
| `callbackType` | String | 否 | `STREAM` / `HTTP`（**必须大写**） |
| `callbackRouteKey` | String | 否 | HTTP 回调模式的路由 key |
| `imGroupOpenSpaceModel` / `imRobotOpenSpaceModel` | Object | 否 | 群聊/机器人单聊场域；用哪个场域对象，其 `supportForward` **必填** |
| `imSingleOpenSpaceModel` | Object | 否 | IM 单聊酷应用场域 |
| `topOpenSpaceModel.spaceType` | String | 否 | 吊顶场域固定 `ONE_BOX` |
| `privateData` | Map\<String,Object\> | 否 | 按 userId 给私有数据 |
| `cardAtUserIds` | Array\<String\> | 否 | 被 @ 的 userId；**同时**要在 markdown 变量里写 `<a atId=user_id> 昵称</a>` |
| `userIdType` | Integer | 否 | `1`=userId 模式（默认），`2`=unionId 模式 |

响应：`{"success": true, "result": "<outTrackId>"}`

（来源：<https://open.dingtalk.com/document/development/interface-for-creating-a-card-instance>）

**步骤 2｜投放卡片** `POST https://api.dingtalk.com/v1.0/card/instances/deliver`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `outTrackId` | String | 是 | 与创建时一致 |
| `openSpaceId` | String | 是 | 格式 `dtv1.card//spaceType.spaceId`；群聊 `IM_GROUP.{conversationId}`，机器人单聊 `IM_ROBOT.{senderStaffId}` |
| `imGroupOpenDeliverModel.robotCode` | String | 是（用该对象时） | 场景群用群机器人 robotCode；**非场景群企业内部机器人用 AppKey** |
| `imGroupOpenDeliverModel.atUserIds` | Map | 否 | `{"userId":"昵称"}`；key/value 都是 `@ALL` 即 @所有人 |
| `imGroupOpenDeliverModel.recipients` | Array\<String\> | 否 | 指定接收者 userId |
| `imRobotOpenDeliverModel.spaceType` | String | 否 | 单聊需设 `IM_ROBOT` |
| `imSingleOpenDeliverModel.atUserIds` | Map | 否 | 单聊场域 @人（key=userId, value=昵称） |
| `userIdType` | Integer | 否 | 同上 |

响应：`{"success": true, "result": [{"spaceType","spaceId","success","carrierId","errorMsg"}]}`
（IM 场域的 `carrierId` 即 `processQueryKey`）

（来源：<https://open.dingtalk.com/document/development/delivery-card-interface>）

**一步版（推荐）**：`POST https://api.dingtalk.com/v1.0/card/instances/createAndDeliver`
（<https://open.dingtalk.com/document/development/create-and-deliver-cards>）——
官方「打字机效果」教程的 Python 示例用的就是这一步
（`card_instance.async_create_and_deliver_card(...)`，<https://open.dingtalk.com/document/development/typewriter-effect-streaming-ai-card>）。

**步骤 3｜更新卡片** `PUT https://api.dingtalk.com/v1.0/card/instances`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `outTrackId` | String | 是 | |
| `cardData.cardParamMap` | Map | 否 | |
| `privateData` | Map | 否 | |
| **`cardUpdateOptions.updateCardDataByKey`** | Boolean | 否 | `true` 按 key 更新（**局部更新**）；不填=**覆盖更新整个 cardData** |
| **`cardUpdateOptions.updatePrivateDataByKey`** | Boolean | 否 | 同上，针对 privateData |
| `userIdType` | Integer | 否 | |

响应：`{"success": true, "result": true}`

（来源：<https://open.dingtalk.com/document/development/interactive-card-update-interface>）

> ⚠️ **`updateCardDataByKey` 是 `cardUpdateOptions` 的**子字段**，不是顶层参数**（见上表）。
> SDK 的 `put_card_data(card_instance_id, card_data, **kwargs)` 把 `kwargs` 平铺到**请求体顶层**
> （`card_replier.py:547-551`），因此正确写法是
> `replier.put_card_data(id, {...}, cardUpdateOptions={"updateCardDataByKey": True})`。

### 3.5 AI 卡片流式更新（本项目的主回答形态）

**端点与权限**

| 项 | 值 |
|---|---|
| HTTP URL | **`https://api.dingtalk.com/v1.0/card/streaming`** |
| HTTP Method | **`PUT`**（不是 POST） |
| 权限点 | **`Card.Streaming.Write`**（「AI卡片流式更新权限」） |
| 鉴权 Header | `x-acs-dingtalk-access-token` |

（来源：<https://open.dingtalk.com/document/development/api-streamingupdate>；
权限前置说明见 <https://open.dingtalk.com/document/development/typewriter-effect-streaming-ai-card>「申请权限：权限点 Code：`Card.Streaming.Write`」）

**请求体（逐字段）**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `outTrackId` | String | 是 | 外部卡片实例 ID，与创建/创建并投放时的 `outTrackId` 一致 |
| `guid` | String | 是 | 请求唯一标志，**服务端用于幂等判断**（SDK 用 `uuid.uuid1()`） |
| `key` | String | 是 | **需要流式更新的变量名**（= 卡片模板里那个变量的 key，如 `content` / `msgContent`） |
| `content` | String | 是 | 本次流式内容。**单次 ≤ 1KB，总量建议 ≤ 3KB**；markdown 必须保证是**全量**且语法完整 |
| `isFull` | Boolean | 否 | `true`=全量（覆盖），`false`=非全量（**默认**，增量）。**「如果流式变量绑定的是 markdown，该参数必须设置为 true，否则会报错」** |
| `isFinalize` | Boolean | 否 | `true`=最后一帧，AI 卡片从「输入中」→「完成」 |
| `isError` | Boolean | 否 | `true`=出错，AI 卡片从「输入中」→「失败」 |

**响应体**：`{"success": true, "result": true}`

**专属错误码（页面级）**：`param.stream.keyEmpty`、`param.stream.contentEmpty`、`param.stream.guidEmpty`、
`param.stream.outTrackId`（card is not exist）、`param.stream.isFull`（isFull 字段为空）、
`param.stream.content`（content is too large）。

**完整调用顺序（AI 卡片）**

```
1. 开发者后台申请权限点 Card.Streaming.Write
2. 卡片平台新建模板：卡片类型=消息卡片，卡片模板场景=AI 卡片；
   在「输入中」状态的 Markdown 组件上打开【流式组件开关】并绑定 markdown 变量（记下变量名，即 key）
3. POST /v1.0/card/instances            （或直接 createAndDeliver）→ outTrackId
4. POST /v1.0/card/instances/deliver    （群聊 IM_GROUP.{conversationId} / 单聊 IM_ROBOT.{staffId}）
   ※ AI 卡片投放后自动进入「处理中」状态，无需手动设置
5. PUT  /v1.0/card/streaming            isFull=true（markdown 必须），isFinalize=false  ← N 次，每次发全量内容
6. PUT  /v1.0/card/streaming            isFull=true，isFinalize=true                  ← 收尾 → 「完成」
   失败：isError=true → 「失败」
```

（来源：<https://open.dingtalk.com/document/development/api-streamingupdate>、
<https://open.dingtalk.com/document/development/ai-card-template>、
<https://open.dingtalk.com/document/development/typewriter-effect-streaming-ai-card>）

**状态值（`flowStatus`）**

| 状态 | `flowStatus` | 谁设置 | 依据 |
|---|---|---|---|
| 处理中 | `1` | **无需手动设置**（卡片投放后、流式开始前） | ✅ 文档（ai-card-template）；数值 `1` 见 SDK `AICardStatus.PROCESSING` |
| 输入中 | `2` | **无需手动设置**（流式更新开始后） | ✅ 文档给"无需手动"；数值 `2` 见 Apifox 镜像的 AI 卡片模板文档与 SDK `AICardStatus.INPUTING` |
| 完成 | `3` | 手动：`isFinalize=true` | ✅ 文档给"需手动 setFinalize"；数值 `3` 见 SDK / 镜像 |
| 执行中 | `4` | 文档未描述 | 仅 SDK `AICardStatus.EXECUTING=4` ❓ |
| 失败 | `5` | 手动：`isError=true` | ✅ 文档；数值 `5` 见 SDK `AICardStatus.FAILED` |

> 官方 AI 卡片模板页**只给了状态名与触发参数，没有给数字**；数字来自 SDK 与镜像文档。
> 流式更新的正确姿势是**用 `isFinalize`/`isError` 驱动状态，不要自己去写 `flowStatus` 数字**。

### 3.6 卡片回调（Stream 模式）

| 项 | 值 |
|---|---|
| topic | `/v1.0/card/instances/callback` |
| 回调体字段 | `outTrackId`（卡片 ID）、`corpId`、`userId`（发起人 userId，**与创建卡片时配置的 `userIdType` 一致**）、`content`（用户输入/按钮值） |
| 前置 | 创建卡片时 `callbackType` 必须为 `STREAM`（大写） |
| SDK 解析 | `CardCallbackMessage.from_dict()`，支持 `extension`/`corpId`/`userId`/`outTrackId`/`content`（`card_callback.py`） |

（来源：<https://open.dingtalk.com/document/development/event-callback-card>）

---

## 4. 网页应用（原 H5 微应用）免登

### 4.1 当前官方流程（**四步**，逐字）

> 1. 调用 `requestAuthCode` 获取免登授权码。
> 2. 调用「获取应用的 Access Token」接口，获取应用访问凭证。
> 3. 调用「通过免登码获取用户信息」接口，获取用户 userid。
> 4. 调用「查询用户详情」接口，获取用户信息。

（来源：<https://open.dingtalk.com/document/development/jsapi-request-auth-code>；
完整流程页 <https://open.dingtalk.com/document/development/enterprise-internal-application-logon-free>）

### 4.2 前端取 authCode

**当前 API 是 `dd.requestAuthCode`（不是 `dd.runtime.permission.requestAuthCode`）**

```javascript
import * as dd from 'dingtalk-jsapi';          // npm install dingtalk-jsapi --save
dd.requestAuthCode({
  corpId: 'corpid',
  clientId: 'clientid',
  onSuccess: function (result) {
    /* result = { code: 'hYLK98jkf0m' }   ← 注意字段名是 code */
  },
  onFail: function (err) {},
});
```

| 项 | 结论 | 依据 |
|---|---|---|
| 适用能力 | **仅「网页应用（原 H5 微应用）」**；小程序**不支持**该 JSAPI | <https://open.dingtalk.com/document/development/jsapi-request-auth-code>（支持说明表） |
| 客户端版本 | Android 7.0.45 / iOS 7.0.45 / Harmony 7.0.0 / Mac 7.0.50 / Windows 7.0.50 | 同上 |
| 返回字段 | `{ code: string }` | 同上 |
| **鉴权规则** | **「无需鉴权即可直接调用」**——免登 JSAPI **不需要 jsapiTicket / dd.config** | 同上 |
| authCode 有效期 | **5 分钟**，且**只能使用一次** | 同上 + getuserinfo 页 |
| 小程序对应 API | `dd.getAuthCode({corpId})` → `res.authCode`（字段名不同！） | <https://open.dingtalk.com/document/development/jsapi-get-auth-code> |
| 必须端内 | 「免登操作需要在钉钉端内实现，端外无法正常调用免登组件」 | <https://open.dingtalk.com/document/development/enterprise-internal-application-logon-free> |

### 4.3 后端换身份：**当前推荐是旧版路径的 `topapi/v2/user/getuserinfo`，用「企业应用 access_token」**

| 项 | 值 |
|---|---|
| HTTP URL | `https://oapi.dingtalk.com/topapi/v2/user/getuserinfo` |
| Method | `POST`（`application/x-www-form-urlencoded;charset=utf-8`） |
| 鉴权 | **Query 参数 `access_token`** = **企业应用 accessToken**（获取方式官方指向 `obtain-orgapp-token`） |
| 请求体 | `code`（免登授权码，5 分钟有效、一次性） |
| 权限 | `permission-qyapi_base`（无需单独申请） |

（来源：<https://open.dingtalk.com/document/development/obtain-the-userid-of-a-user-by-using-the-log-free>）

**响应体（逐字段）**

| 字段 | 类型 | 说明 |
|---|---|---|
| `request_id` | String | 请求 ID |
| `errcode` | Number | 返回码，`0` 成功 |
| `errmsg` | String | 描述（`ok`） |
| `result` | Object | 返回结果 |
| `result.userid` | String | **用户 userId**（企业内部应用要的就是它） |
| `result.unionid` | String | 用户 unionId |
| `result.associated_unionid` | String | 用户关联的 unionId |
| `result.name` | String | 用户名字 |
| `result.device_id` | String | 设备 ID |
| `result.sys` | Boolean | 是否管理员 |
| `result.sys_level` | Number | `1` 主管理员 / `2` 子管理员 / `100` 老板 / `0` 其他 |

```json
{"errcode":0,"errmsg":"ok","result":{"associated_unionid":"N2o5U3axxxx","unionid":"gliiW0piiii02zBUjUxxxx",
 "device_id":"12drtfxxxxx","sys_level":1,"name":"张xx","sys":true,"userid":"userid123"}}
```

**`/v1.0/contact/users/{unionId}`（"新版"路径）的真实契约——与本项目假设不同**

| 项 | 值 |
|---|---|
| URL / Method | `https://api.dingtalk.com/v1.0/contact/users/{unionId}`，`GET`（**路径参数**，当前用户可传 `me`） |
| 鉴权 | Header `x-acs-dingtalk-access-token` = **「个人用户的 accessToken」**（**不是企业应用 accessToken**） |
| 权限 | `permission-Contact.User.Read` |
| 响应字段 | `nick`、`avatarUrl`、`mobile`、`openId`、`unionId`、`email`、`stateCode` |

（来源：<https://open.dingtalk.com/document/development/dingtalk-retrieve-user-information>；
响应示例见 <https://open.dingtalk.com/document/development/server-api-calling-guide> 末节「敏感权限API」）

⚠️ **两个必须记住的坑**：
1. 该接口**返回里没有 `userid`**，只有 `unionId` / `openId`。要 userid 必须再调
   `POST https://oapi.dingtalk.com/topapi/user/getbyunionid` 做映射
   （<https://open.dingtalk.com/document/development/query-a-user-by-the-union-id>）。
2. 它需要**用户级 accessToken**，而用户级 token 来自 **OAuth2 登录流程**，不是来自 `dd.requestAuthCode`：

| 用户级 token 接口 | 值 |
|---|---|
| URL / Method | `https://api.dingtalk.com/v1.0/oauth2/userAccessToken`，`POST` |
| 请求体 | `clientId`（企业内部应用传 AppKey）、`clientSecret`（AppSecret）、`code`（OAuth2 授权码）、`refreshToken`、`grantType`（`authorization_code` 或 `refresh_token`） |
| 响应 | `accessToken`（7200s）、`refreshToken`（**有效期 30 天**）、`expireIn` |

（来源：<https://open.dingtalk.com/document/development/obtain-user-token>；
前置 OAuth2 授权码获取见 <https://open.dingtalk.com/document/development/obtain-identity-credentials>）

> **给本项目的选型结论**：钉钉客户端内 H5 免登（`requestAuthCode` + `authCode`）→ **用 §4.3 的
> `topapi/v2/user/getuserinfo` + 企业应用 access_token**，这是文档当前给出的官方链路；
> `/v1.0/contact/users/me` **不是同一场景**（它服务的是 OAuth2 登录后代表用户访问资源），
> 用它反而要额外引入一套 OAuth2 与 30 天 refreshToken 生命周期。

### 4.4 `unionid` 与 `userid` 的关系（官方口径）

> 1. 用户的 `userid` 在**同一企业下全局唯一**，不会重复。
> 2. 用户的 `unionId` 在**同一个应用服务内全局唯一**（同一自然人在多个企业用同一应用时 unionId 相同）。
> 3. 通过 userId 可查 unionId（查询用户详情）；通过 unionId 也可查 userId（根据 unionid 获取用户 userid）。

（来源：<https://help.dingtalk.io/zh/open/development/common-errors>）

企业内部应用的用户详情里，`unionid` 的定义是「员工在当前**开发者企业账号**范围内的唯一标识」
（<https://open.dingtalk.com/document/development/query-user-details>）。

**工程口径（推荐）**：内部应用一律**以 `userid` 为主键**（企业内唯一、稳定、与 `senderStaffId` 同域），
`unionid` 仅作跨应用/跨企业关联的辅助键；两者都用接口做双向映射，**不要自己拼规则推导**。

### 4.5 前置配置：是不是「应用可信域名」？

| 项 | 结论 | 依据 |
|---|---|---|
| 应用能力 | 必须已添加**网页应用**能力 | <https://open.dingtalk.com/document/development/enterprise-internal-application-logon-free> |
| 域名一致性要求 | 「在使用该 JSAPI 之前，请确保网页应用的**首页地址 / 重定向 URL（回调地址）/ 端内免登地址**与**运行该 JSAPI 的地址域名保持一致**」 | 同上（+ <https://open.dingtalk.com/document/development/jsapi-request-auth-code>） |
| 配置位置 | 开发者后台 → 开发配置 → **安全设置**（端内免登地址/重定向 URL）；网页应用首页地址在「配置网页应用」 | 同上 |
| 「可信域名」这个提法 | ❓ **当前文档中未出现"应用可信域名"字样**；它属于旧版 JSAPI 鉴权（`dd.config` + jsapiTicket）时代的说法。当前对免登的要求就是上面的「四个地址域名一致」 | — |

---

## 5. 通讯录同步（部门与用户）

### 5.1 一个必须纠正的前提：**当前文档给出的通讯录接口是 OAPI（旧版规范）`topapi/v2/*`**

我按 `open.dingtalk.com` 的全量 API 目录逐个打开核实，企业内部应用「获取部门列表 / 获取部门详情 /
获取部门用户详情 / 查询用户详情 / 根据 unionid 获取用户 userid」**全部落在 `oapi.dingtalk.com`**：

| 能力 | 当前文档 URL | 规范 | 权限 |
|---|---|---|---|
| 获取部门列表 | `POST https://oapi.dingtalk.com/topapi/v2/department/listsub` | 旧版 | `qyapi_get_department_list` |
| 获取部门详情 | `POST https://oapi.dingtalk.com/topapi/v2/department/get` | 旧版 | `qyapi_get_department_list` |
| 获取部门用户详情 | `POST https://oapi.dingtalk.com/topapi/v2/user/list` | 旧版 | `qyapi_get_department_member` |
| 获取部门用户 userid 列表 | `POST https://oapi.dingtalk.com/topapi/user/listid` | 旧版 | 不受通讯录权限范围限制 |
| 获取部门用户基础信息 | `POST https://oapi.dingtalk.com/user/simplelist` | 旧版 | — |
| 查询用户详情 | `POST https://oapi.dingtalk.com/topapi/v2/user/get` | 旧版 | `qyapi_get_member` |
| 根据 unionid 获取用户 userid | `POST https://oapi.dingtalk.com/topapi/user/getbyunionid` | 旧版 | `qyapi_get_member` |
| 查询离职记录列表 | `GET https://api.dingtalk.com/v1.0/contact/empLeaveRecords` | **新版** | `Contact.Common.Read` |
| 获取用户通讯录个人信息 | `GET https://api.dingtalk.com/v1.0/contact/users/{unionId}` | **新版** | `Contact.User.Read` |

（来源依次：<https://open.dingtalk.com/document/development/user-management-acquires-the-list-departments>、
<https://open.dingtalk.com/document/development/query-department-details0-v2>、
<https://open.dingtalk.com/document/development/queries-the-complete-information-of-a-department-user>、
<https://open.dingtalk.com/document/development/query-the-list-of-department-userids>、
<https://open.dingtalk.com/document/development/query-user-details>、
<https://open.dingtalk.com/document/development/query-a-user-by-the-union-id>、
<https://open.dingtalk.com/document/development/query-the-details-of-employees-who-have-left-office>、
<https://open.dingtalk.com/document/development/dingtalk-retrieve-user-information>）

> ❓ **`/v1.0/contact/v3/departments/lists`、`/v1.0/contact/v3/users/{userId}` 这两个路径
> 在钉钉当前的公开文档里未核实到**（既不在全量 API 目录，也不在通讯录产品线索引）。
> 本项目 `方案/04` §10.1 把它们标为「新版路径」——**至少当前文档不支持这个说法**（见 §8）。

### 5.2 部门列表 / 详情：字段与分页

**获取部门列表（`topapi/v2/department/listsub`）**

| 请求 | 说明 |
|---|---|
| Query `access_token` | 企业应用 access_token（**URL 上**） |
| Body `dept_id` | Number，**否**（缺省即根部门）；「父部门 ID」 |
| Body `language` | `zh_CN`（默认）/ `en_US` |

响应：`request_id` / `errcode` / `errmsg` / `result[]`，每个元素：
`dept_id`(Number)、`name`(String)、`parent_id`(Number)、`create_dept_group`(Boolean)、`auto_add_user`(Boolean)。

⚠️ **没有分页参数**，且原文明确：**「只支持获取当前部门的下一级部门基础信息，不支持获取当前部门下所有层级子部门」**
→ 全量部门树必须**自己递归**。

**获取部门详情（`topapi/v2/department/get`）**

| 请求 | 说明 |
|---|---|
| Body `dept_id` | Number，**是**。原文：**「根部门 ID 为 1」** |
| Body `language` | 否 |

响应 `result` 关键字段：`dept_id`、`name`、**`parent_id`（原文：「父部门 id，根部门为 1」）**、
`source_identifier`、`create_dept_group`、`auto_add_user`、`order`、`dept_group_chat_id`、
`group_contain_sub_dept`、`org_dept_owner`、`dept_manager_userid_list`(String[])、
`outer_dept`/`outer_permit_depts`/`outer_permit_users`/`user_permits`/`hide_dept`/`dept_permits`（可见性控制）、
`auto_approve_apply`、`code`（部门编码）、**`member_count`（含子部门）**、**`owning_member_count`（不含子部门）**。

✅ **根部门约定 = `1`**（部门详情页两处写明「根部门 ID 为 1 / 根部门为 1」；
部门用户详情页也写「如果是根部门，该参数传 1」）。

### 5.3 部门成员（`topapi/v2/user/list`）：分页参数与字段

| 请求字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `dept_id` | Number | 是 | 「如果是根部门，该参数传 1」；**只取本部门，不含子部门** |
| `cursor` | Number | 是 | **游标分页**：首次传 `0`，后续传上次返回的 `next_cursor` |
| `size` | Number | 是 | 分页大小（官方示例 10，**上限未在页内给出** ❓） |
| `order_field` | String | 否 | `custom`(默认) / `entry_asc` / `entry_desc` / `modify_asc` / `modify_desc` |
| `contain_access_limit` | Boolean | 否 | 是否返回访问受限的员工 |
| `language` | String | 否 | `zh_CN`(默认) / `en_US` |

响应：`errcode`/`errmsg`/`result{ has_more, next_cursor, list[] }`；
`list[]` 的主要字段：
`userid`、`unionid`、`name`、`avatar`、`state_code`、`mobile`、`hide_mobile`、`telephone`、`job_number`、
`title`、`email`、`org_email`、`work_place`、`remark`、**`dept_id_list`(Number[])**、`dept_order`、
`leader`、`active`、`admin`、`boss`、`exclusive_account`、`extension`、`hired_date`。

⚠️ **`mobile` / `email` 等敏感字段需要额外权限点**（原文：「如果没有返回该字段，需要检查当前应用通讯录权限中
『企业员工手机号信息权限』/『邮箱等个人信息』权限是否开启」）。

（来源：<https://open.dingtalk.com/document/development/queries-the-complete-information-of-a-department-user>）

### 5.4 用户详情（`topapi/v2/user/get`）

请求：`userid`（是）、`language`（否）。
响应 `result`：`userid`、`unionid`、`name`、`avatar`、`mobile`、`hide_mobile`、`telephone`、`job_number`、
`title`、`email`、`org_email`、`work_place`、`remark`、**`dept_id_list`**、`dept_order_list`、
`leader_in_dept`（所在部门 + 是否主管）、`role_list`（角色）、`hired_date`、`active`、`admin`、`boss`、
`exclusive_account`、`manager_userid`、`extension`、`union_emp_ext` 等。

（来源：<https://open.dingtalk.com/document/development/query-user-details>）

### 5.5 离职与删除怎么表示

**结论：当前用户/部门接口的响应里没有 `status` 或 `del` 字段**（我逐字段核对了
`topapi/v2/user/list` 与 `topapi/v2/user/get` 的响应表，**均无 `status`/`del`**）。
离职在钉钉侧的表达是**侧向的**：

| 手段 | 说明 | 依据 |
|---|---|---|
| **事件回调** | `user_leave_org`（通讯录用户离职）、`user_add_org`、`user_modify_org`、`user_active_org`、`org_admin_add`、`org_admin_remove`、`org_dept_create`、`org_dept_modify`、`org_dept_remove`、`org_remove` | <https://open.dingtalk.com/document/org/event-list-1>（事件列表原文） |
| **离职记录接口** | `GET /v1.0/contact/empLeaveRecords`，Query `startTime`（必填，ISO8601/RFC3339）、`endTime`（跨度 ≤365 天）。响应含 `userId`、`name`、`mobile`、`leaveTime`、**`leaveReason`** | <https://open.dingtalk.com/document/development/query-the-details-of-employees-who-have-left-office> |
| `leaveReason` 取值 | `oapi`=调用接口删除、`cancel`=注销、`leave`=主动离职、`unknown`=未知原因、`delete`=管理员删除 | 同上 |
| **现象特征** | 已离职/待入职用户用 userId 查详情会报 **`errcode 50002`「请求的员工 userid 不在授权范围内」**（原文把「用户为已离职状态或待入职状态」列为该报错成因之一） | <https://help.dingtalk.io/zh/open/development/common-errors> |
| 成员列表 | 离职后该用户从部门成员列表中消失（无 tombstone 字段可判断） | 推论，⚠️ 未见明文 |

**增量同步是否可行**：✅ **可行，且有事件回调**——Stream 接 `register_all_event_handler` + 订阅 `EVENT *`
即可收到上述事件；`empLeaveRecords` 按时间窗拉取可做**对账/补偿**。
（来源：<https://open.dingtalk.com/document/development/develop-stream-mode-push-server>、
<https://open.dingtalk.com/document/org/event-list-1>）

> **给本项目的落地建议**：本地 `ekbs_user.status` 不能指望钉钉给，只能由**本系统**根据
> 事件（`user_leave_org` / `org_remove`）或「连续 N 次同步都查不到该 userid（50002）」推导；
> 这条推导规则必须显式写进设计，不要假设钉钉返回里有一个 `status`。

---

## 6. 错误码与可重试性

### 6.1 两套错误结构（按接口规范分）

| 规范 | 错误结构 | 涉及接口 |
|---|---|---|
| **旧版规范**（`oapi.dingtalk.com`） | `{"errcode": <Number>, "errmsg": "<String>"[, "request_id": "..."]}` | `topapi/v2/*`、`gettoken`、`user/getuserinfo`、`user/list` … |
| **新版规范**（`api.dingtalk.com`） | 用 **HTTP 状态码**表达失败（如 `400`），错误体给出**错误码字符串 + 错误信息**（各接口文档的「错误码」表：`HttpCode` / 错误码 / 错误信息 / 说明） | `v1.0/oauth2/accessToken`、`v1.0/robot/*`、`v1.0/card/*`、`v1.0/contact/*` |

⚠️ **`code` / `message` / `requestid` 三字段的错误体结构**：❓ **未核实到官方示例**——
我检索了全局错误码页、API 调用步骤详解页、以及多个新版接口页，**没有任何一页给出带 `requestid` 的错误响应 JSON 示例**。
文档层面只承诺「HTTP 状态码 + 错误码字符串 + 错误信息」。
工程上**不要**依赖 `requestid` 字段做日志关联，除非实测确认。

（来源：<https://open.dingtalk.com/document/development/server-api-error-codes-1>、
<https://open.dingtalk.com/document/development/server-api-calling-guide>）

### 6.2 常见错误码（逐条核实）

| errcode | 含义 | 官方排查建议 | 可重试性 |
|---|---|---|---|
| `40001` | 获取 access_token 时 Secret 错误，或 access_token 无效 | 检查 access_token | 改配置后重试 |
| `40014` | 不合法的 access_token | **「access_token 这个参数应该是带在 url 后面的」** | ✅ 刷新 token 后重试（**注意是新旧规范混用导致的高频坑**） |
| `41001` | 缺少 access_token 参数 | 「该参数必须跟在请求 url 中」 | ❌ 修代码 |
| `42001` | access_token 超时 | 检查网络状态 | ✅ 刷新后重试 |
| `40035` | 不合法的参数 | 常见于 POST 未带参数 | ❌ 修代码 |
| `40048` | url 中包含不合法 domain | 发消息接口里消息 URL 链接不安全 | ❌ 改内容 |
| `33001` | 无效的企业 ID | 确认 access_token 是否正确 | ❌ |
| `50002` | 企业员工不在授权范围 | 检查「版本管理与发布 > 可使用范围设置」与授权范围；**也可能是该用户已离职/待入职** | ❌ 配置/数据问题 |
| `60011` | 没有调用该接口的权限 | 到「应用详情 > 权限管理 > 添加接口权限」勾选权限点 | ❌ 需管理员操作 |
| `60121` | 找不到该用户 | 确认该企业下员工是否存在 | ❌ |
| `88` | 鉴权异常 | **「关注返回结果里的 `subCode` 和 `subMsg`」** | ⚠️ 看 subCode 决定 |
| `90002` | 全局限流（该接口所有调用方都被暂时禁用） | 立即暂停密集调用，**指数退避重试**（1s/2s/4s…） | ✅ **官方明确建议指数退避** |
| `20001` | 「受调用量超量影响，当前我的消息服务已经暂停」——Stream 接收消息里会**新增 `errMsg` 字段**，且**回调体没有 text/content** | 去开发者后台看调用量 | ❌ 需处理配额 |
| IP 维度限流 | **不是错误码**：返回 HTML 或 `{"status":1111,"wait":5,"source":"x5","punish":"deny","uuid":"xxx"}` | 该 IP 被禁 **5 分钟** | ✅ 等 `wait` 后重试 / 换出口 IP |

（来源：错误码见 <https://open.dingtalk.com/document/development/server-api-error-codes-1>；
限流见 <https://open.dingtalk.com/document/development/call-frequency-limit>；
`20001` 见 <https://open.dingtalk.com/document/development/develop-stream-mode-push-server>；
`50002` 与离职状态的关系见 <https://help.dingtalk.io/zh/open/development/common-errors>）

### 6.3 `90018` —— ❓ **未核实到**

`90018` 在**全局错误码页（约 2MB 全文）中检索不到**，也不在限流页、Stream 页、通讯录常见错误页中。
请勿在代码里为它写特定分支；按 `errcode + errmsg` 通用处理。

---

## 7. 「未核实到」清单（实施前不得当作事实）

| # | 问题 | 状态 | 说明 |
|---|---|---|---|
| 1 | accessToken 的**每日**调用次数上限 | ❓ | 官方只讲「每月调用量（版本相关）」「90002 全局限流」「IP 20s/10000 次」，无"每日"口径 |
| 2 | 机器人发消息的**每分钟/每机器人**频率硬限值 | ❓ | 未找到官方数值；只有 `flowControlledStaffIdList` 这种事后反馈 |
| 3 | Stream 回调 **ACK 超时秒数与超时后果** | ❓ | 官方文档全篇未给；SDK 亦无 ACK 定时器（异步 task + 无超时） |
| 4 | 群聊**非 @** 消息是否也投递（是否可用 `isInAtList=false` 判定为普通消息） | ❓ | 文档字面是「@群机器人」才投递；`isInAtList` 的用途仅"是否在@列表中" |
| 5 | `flowStatus = 4（执行中）` 的文档依据 | ❓ | 仅 SDK `AICardStatus.EXECUTING=4`；AI 卡片模板页只描述 4 个状态 |
| 6 | 新版规范错误体是否含 `code`/`message`/`requestid` | ❓ | 文档无示例；只有 HTTP 状态码 + 错误码字符串 + 错误信息 |
| 7 | `errcode 90018` | ❓ | 全局错误码页检索不到 |
| 8 | `/v1.0/contact/v3/departments/lists`、`/v1.0/contact/v3/users/{userId}` 是否存在/为当前推荐 | ❓ | 当前全量 API 目录与通讯录索引中未出现 |
| 9 | `topapi/v2/user/list` 的 `size` 上限 | ❓ | 页内只给示例值 10 |
| 10 | 机器人文件 `downloadUrl` 的实际域名（是否落在 `*.alicdn.com`） | ❓ | 接口只说明「临时下载链接」，未给域名；出网白名单需按实测放宽 |
| 11 | Stream 网关 WebSocket 的实际域名（是否 `*.dingtalk.com`） | ❓ | 域名由 `open_connection` 动态返回，文档未列举 |
| 12 | 「应用可信域名」这一配置项在当前后台是否仍存在 | ❓ | 当前文档改为「首页地址/重定向URL/端内免登地址 与运行 JSAPI 的域名一致」 |
| 13 | `robotCode` 对内部应用机器人是否**恒等于** AppKey | ⚠️ 强证据但非明文 | 卡片投放页「非场景群的企业内部机器人发送群聊，使用机器人的 AppKey」+ SDK 全程用 `credential.client_id` 当 robotCode；机器人 ID 页只给图片 |
| 14 | 「不纳入每月调用量限制」的完整接口清单 | ⚠️ | 只读到官方索引摘要（含 accessToken/免登/通讯录），未逐条核实 |
| 15 | 机器人消息体 `senderUnionId` 的可用性 | ⚠️ | 文档报文里有该字段；**SDK 0.24.3 不解析**，只能从 `extensions` 取 |

---

## 8. 与 `方案/04-钉钉接入方案.md` 的不符 / 过时之处（只读比对，未修改该文件）

> 以下逐条给出「方案原文位置 → 官方原文 → 差异」。**共 11 条**。

### 8.1 §2.6 应用凭证与密钥管理

| # | 方案写法 | 核实结果 | 级别 |
|---|---|---|---|
| 1 | `DINGTALK_CLIENT_ID（= AppKey）` / `DINGTALK_CLIENT_SECRET` | ✅ **正确**。官方原文：「Client ID 对应旧版应用的 AppKey/SuiteKey」 | 一致 |
| 2 | `DINGTALK_ROBOT_CODE` 作为**独立配置项**，标注"非敏感" | ⚠️ 可用但冗余：对内部应用机器人 `robotCode` 就是 AppKey，**同一个值配两遍**容易漂移。建议 `robot_code: ${CLIENT_ID}` 派生，而不是独立填 | 轻微 |
| 3 | AppSecret **轮换周期 90 天** | ❓ 钉钉文档**不要求**轮换周期，也没有 90 天说法。这是项目自定纪律，**不是钉钉要求**，建议标注为「本项目自定」以免误读为平台约束 | 口径 |
| 4 | §2.6 未提 `DINGTALK_CORP_ID` 的用途来源 | ⚠️ 若走新版 `/v1.0/oauth2/{corpId}/token` 或 OAuth2（`login.dingtalk.com`）则 corpId 必需；若只走 `/v1.0/oauth2/accessToken` 则**完全不需要** corpId。方案把 corpId 列为"adapter、api 都要用"，但没有说明**在哪一步用**，建议明确 | 缺口 |

### 8.2 §3.5 机器人消息侧的用户身份确定

| # | 方案写法 | 核实结果 | 级别 |
|---|---|---|---|
| 5 | 第 1 步「`senderStaffId` 即发送者的 **userid**」 | ✅ 正确，但**漏了一个关键前提**：官方原文「该字段**在机器人发布线上版本后，才会返回**」。灰度/未发布版本期间 `senderStaffId` 为空——方案的「为空就丢弃」会**把所有消息都丢掉**而不给出可诊断的原因，排错时极易误判 | **重要补充** |
| 6 | 第 5 步「即使 `isInAtList == false` 也可收到（需自行判定是否需要响应）」 | ❌ **与官方文档不符**。原文投递条件是「用户 **@群机器人** 或与机器人单聊」；`isInAtList` 的官方定义只是"是否在@列表中"，**没有任何文档支持"非 @ 也会收到"** | **不符** |
| 7 | 第 5 步「群聊中 `conversationType == "2"`」 | ✅ 正确（且必须是**字符串**比较，方案写法正确） | 一致 |
| 8 | 第 2 步「`senderStaffId` 为空（外部联系人/机器人自身消息/系统消息）」 | ⚠️ 官方只在 `atUsers[].staffId` 上说明「外部群中的外部用户此字段为空」；对 `senderStaffId` 为空的具体成因**未核实到**。结论方向可用，但归因不严谨 | 轻微 |

### 8.3 §5.3 Stream 客户端完整可运行代码

| # | 方案写法 | 核实结果 | 级别 |
|---|---|---|---|
| 9 | `dingtalk_stream.Credential(client_id, client_secret)`、`DingTalkStreamClient(credential)`、`register_callback_handler(ChatbotMessage.TOPIC, ...)`、`register_callback_handler(Card_Callback_Router_Topic, ...)`、`register_all_event_handler(...)`、`return AckMessage.STATUS_OK, "OK"`、`ChatbotMessage.from_dict(callback.data)`、`incoming.message_id / sender_staff_id / conversation_id / conversation_type / is_in_at_list / text.content` | ✅ **全部与 SDK 0.24.3 源码一致**（类名、方法名、返回元组、字段名逐字核对通过） | 一致 |
| 10 | `StreamRunner._run_stream()` 里**先** `stream_state.set_connected(True)`，再 `run_in_executor(None, self.client.start_forever)` | ❌ **逻辑错误**：`set_connected(True)` 在**建立连接之前**执行，而 `open_connection()` 失败（凭据错误/网络不通/网关拒绝）时 SDK 只是 `sleep(10)` 无限重试、**永不抛异常**，于是 `finally` 也不执行 → `/readyz` 会**长期谎报 `connected=True`**，正是 §7.3 想避免的「谎报 ready」。应改为：连接成功后再置位（可用 SDK 的 `open_connection()` 探活或回调首次到达时置位），并给 `connected` 加**心跳超时**判据 | **不符（实现缺陷）** |
| 11 | `StreamState.reconnect_count` 只有声明与读取，**没有任何自增点** | ❌ `/readyz` 与 `/health/deps` 输出 `reconnect_count`，但它恒为 `"0"` → §7.3 表格里「重连」这一可观测项**实际不存在** | **不符（实现缺陷）** |
| 12 | §5.3 注释「SDK 的 `start_forever()` 内部已实现断线重连与心跳」 | ✅ 正确：心跳 = 每 60s WebSocket ping；重连 = 10s/3s/10s 三档退避 + `disconnect` 主动重连 + 外层 `asyncio.run` 循环 | 一致 |

### 8.4 §5.7 答案回复形态与卡片流式更新

| # | 方案写法 | 核实结果 | 级别 |
|---|---|---|---|
| 13 | `replier.streaming(id, "msgContent", pending, append=True, ...)` —— **增量追加**（`isFull=false`） | ❌ **与官方文档直接冲突**。原文：「如果流式变量绑定的是 **markdown**，该参数必须设置为 **true**，否则会报错」；官方「打字机效果」Python 示例的每一次流式调用都是 `append=False`（`isFull=true`，**每次发全量内容**）。方案在 markdown 变量上用增量模式，轻则内容错乱、重则直接报错 | **不符（关键）** |
| 14 | `replier.streaming(..., content_key="msgContent")` | ⚠️ `key` 必须是**卡片模板里那个变量的名字**（官方示例用 `content`）。本方案文档里的 `msgContent` 取自 SDK 内置模板 `AIMarkdownCardInstance`（`382e4302-…-a30acfab2e71.schema`）的约定；若用自建模板**必须换成模板真实变量名** | 需确认 |
| 15 | 卡片 JSON 里的 `"config": {"streamingMode": true}`、`"header"/"contents"/"sys_full_json_obj": "{...}"` 混排 | ⚠️ **官方卡片协议里没有 `config.streamingMode`**（我核查了创建/投放/更新/流式四个接口的请求体字段表）。官方做法是：模板类型选「AI 卡片」+ 在「输入中」状态的 **Markdown 组件上打开「流式组件开关」**。`sys_full_json_obj` 确实存在（SDK 用它排布 `order`），但它属于**卡片模板的编排数据**，不是「流式开关」 | **不符** |
| 16 | `flowStatus` 数值表 `1 处理中 / 2 输入中 / 4 执行中 / 3 完成 / 5 失败` | ⚠️ **文档只承认其中 4 个状态且不给数字**（处理中/输入中/完成/失败），并明确「处理中、输入中**无需手动设置**」。数值来自 SDK `AICardStatus`（含文档未描述的 `EXECUTING=4`）。正确姿势是**只用 `isFinalize`/`isError` 驱动状态**，不要手写 `flowStatus` 数字 | 口径 |
| 17 | `replier.put_card_data(id, {"staticMsgContent": …}, updateCardDataByKey=True)` | ❌ **参数位置错误**。官方 `updateCardDataByKey` 是 **`cardUpdateOptions` 的子字段**；SDK 把 `kwargs` 平铺到请求体顶层，所以正确写法是 `put_card_data(id, {...}, cardUpdateOptions={"updateCardDataByKey": True})`。按方案原文传，**顶层多了个无效字段，实际执行的是"覆盖更新"** → 会**清掉 `msgContent`** | **不符（关键）** |
| 18 | 流程「创建+投放两步（`card/instances` + `card/instances/deliver`）」 | ✅ 正确；但官方更推荐一步版 `POST /v1.0/card/instances/createAndDeliver`，官方 Python 示例用的就是它 | 建议 |
| 19 | `AICardReplier`、`AICardStatus`、`MarkdownCardInstance`、`set_title_and_logo`、`reply(markdown, at_sender=, at_all=)` | ✅ **逐个与 SDK 0.24.3 导出符号一致**（`dingtalk_stream/__init__.py`） | 一致 |
| 20 | 方案称「AI 卡片（流式）」需「卡片模板支持 `flowStatus` 与流式字段」 | ⚠️ 更准确的前置是：**权限点 `Card.Streaming.Write`** + 模板场景选「AI 卡片」+ Markdown 组件开流式开关。方案**完全没提这个权限点**——它是调用 `/v1.0/card/streaming` 的硬前置 | **缺口** |
| 21 | 方案未写卡片流式的内容长度约束 | ⚠️ 官方：`content` **单次 ≤ 1KB，总量建议 ≤ 3KB**。方案的分片节流阈值（`streaming_chunk_chars`）**应以 1KB 为硬上限**，否则触发 `param.stream.content`（content is too large） | **缺口** |

### 8.5 §6.5 出网安全

| # | 方案写法 | 核实结果 | 级别 |
|---|---|---|---|
| 22 | `ALLOWED_HOSTS = {api.dingtalk.com, oapi.dingtalk.com, login.dingtalk.com}` | ✅ 三个都是**必需**的：SDK 走 `api.dingtalk.com`（含 `/v1.0/gateway/connections/open`）；旧版 `topapi/*` 与 `media/upload` 走 `oapi.dingtalk.com`；OAuth2 登录页走 `login.dingtalk.com` | 一致 |
| 23 | 「Stream 网关域名（`*.dingtalk.com:443`）」 | ⚠️ 方向对但**不可作为白名单依据**：WebSocket 的 `endpoint` 是 `open_connection` **动态返回**的，域名未在文档中列举。**必须按实测域名收紧**，不能靠后缀猜（❓ 见 §7-11） | 需实测 |
| 24 | 「钉钉文件 CDN（`*.alicdn.com:443`）」+ `ALLOWED_SUFFIXES` | ❓ `downloadUrl` 的域名官方未给；`*.alicdn.com` 是历史经验值。**上线前必须用真实 `downloadUrl` 回填白名单**，否则会误拦 | 需实测 |
| 25 | §6.5 代码示例里 `get_access_token()` 调 `https://oapi.dingtalk.com/gettoken` | ❌ **过时**：官方已将该接口迁入「历史文档（不推荐）」目录（2023-08-17），且旧接口用 `errcode/errmsg` + URL 带 token。应改为 `POST https://api.dingtalk.com/v1.0/oauth2/accessToken`（`appKey`/`appSecret` → `accessToken`/`expireIn`） | **过时** |
| 26 | §6.5 的 token 刷新「提前 300s」 | ⚠️ 官方建议**缓存 7000s**（即提前 ~200s）或「过期前 10 分钟」。数值无对错，但方案未说明依据；建议统一为 7000s 并注明官方口径 | 口径 |
| 27 | §6.5 图里「适配器不得调用云端模型」「逐进程出网口径」 | ✅ 与钉钉文档无冲突（这是项目自定架构约束） | 一致 |
| 28 | 方案用 `resp.status_code in (401, 403)` 触发强制刷新 token | ⚠️ **新版规范凭据错误返回的是 `400`**（如 `invalidClientIdOrSecret` 是 HTTP 400；accessToken 失效类错误可能出现在 4xx 与旧版 errcode 中）。仅判 401/403 **可能漏掉刷新时机**；建议同时按 errcode 40014/42001 与 400 分支处理 | 需加固 |

### 8.6 §7.3 健康检查

| # | 方案写法 | 核实结果 | 级别 |
|---|---|---|---|
| 29 | `/readyz` 用 `stream_state.started_at is None → not_ready`，否则按 `connected` 报 | ⚠️ 判据设计**合理**，但被 §5.3 的实现缺陷击穿：`set_connected(True)` 在连上之前就置位，`started_at` 立即有值且 `connected=True` → **冷启动失败也会报 ready**（见 #10） | **不符** |
| 30 | `/readyz` 返回 `reconnect_count` | ❌ 该计数**从未自增**（见 #11），观测值为恒定 0，等于是个假指标 | **不符** |
| 31 | 「就绪」定义含「Stream 状态**已知**」 | ✅ 思路正确；但真正可靠的"已知"只有两种：SDK 的 `open_connection()` 成功 + 首条报文到达。建议把 `connected` 的定义改为**「最近 N 秒内收到过报文/心跳」**，而不是「已经调用过 start_forever」 | 建议 |

### 8.7 §10.1 钉钉 API 调用清单（顺带核对到的额外不符）

| # | 方案写法 | 核实结果 | 级别 |
|---|---|---|---|
| 32 | `contact/v3/departments/lists`、`contact/v3/departments/{deptId}`、`contact/v3/users`、`contact/v3/users/{userId}` 标注为「新版路径」并作为主路径 | ❌ **当前官方文档中未核实到这些路径**；企业内部应用当前文档给出的部门/成员/用户接口是 `oapi.dingtalk.com/topapi/v2/department/listsub`、`topapi/v2/department/get`、`topapi/v2/user/list`、`topapi/v2/user/get`（见 §5.1） | **不符** |
| 33 | `oauth2/userAccessToken` + `contact/users/me` 作为免登链路 | ⚠️ 接口**真实存在且字段已核实**（§4.3），但**不是 H5 免登链路**：`contact/users/me` 要**用户级 token**（OAuth2 登录得到），而 H5 免登官方链路是 `requestAuthCode → topapi/v2/user/getuserinfo`。方案 §3.2 把两者混为一条链 | **不符** |
| 34 | §3.2 称新版 `contact/users/me` 返回「`userid` / `unionId` / `nick` / `avatarUrl` / `mobile` 等」 | ❌ 该接口**不返回 `userid`**（只返回 `nick`/`avatarUrl`/`mobile`/`openId`/`unionId`/`email`/`stateCode`）。要 userid 必须再调 `topapi/user/getbyunionid` | **不符** |
| 35 | `topapi/v2/user/getuserinfo` 标注为「旧版免登换取 userid（兼容模式），默认关闭」 | ❌ **反了**：这是当前 H5 免登的**官方推荐链路**（requestAuthCode 文档第 3 步），不是兼容兜底 | **不符（关键）** |
| 36 | `gettoken` 标为「旧版路径」但作为唯一实现 | ⚠️ 结论对，但清单没写「已迁历史文档、不推荐」，也没列 `/v1.0/oauth2/accessToken` 作为主路径 | 过时 |
| 37 | 限流列「约 20/s」「中（约 20~50/s）」等 | ⚠️ **无出处**：官方未给这些每接口 QPS 数字，只有「每月调用量（版本相关）+ 90002 全局限流 + IP 20s/10000 次」。建议删除臆测数值，改为引用 §1.4 的三个真实维度 | 口径 |
| 38 | `card/streaming` 标为「`/v1.0/card/streaming`」+ 每次问答 5~50 次 | ✅ 路径正确（**PUT**）；调用次数取决于节流策略，非官方数值 | 部分一致 |
| 39 | 事件订阅列 `user_add_org` / `user_modify_org` / `user_leave_org` / `dept_add` / `dept_modify` / `dept_remove` | ⚠️ 前三个 ✅ 正确；后三个**事件名不对**：官方是 `org_dept_create` / `org_dept_modify` / `org_dept_remove`（另有 `org_remote`、`org_admin_add/remove`、`user_active_org`） | **不符** |

**条目统计（逐条编号核对，共 39 条）**

| 分类 | 条数 | 编号 |
|---|---|---|
| ❌ **明确不符 / 已过时** | **14** | #6、#10、#11、#13、#15、#17、#25、#29、#30、#32、#33、#34、#35、#39 |
| ⚠️ 缺口 / 需实测 / 口径问题 | 18 | #2、#3、#4、#5、#8、#12、#14、#16、#18、#20、#21、#23、#24、#26、#28、#31、#36、#37 |
| ✅ 核对一致 | 6 | #1、#7、#9、#19、#22、#27 |
| （另有 #38 部分一致） | 1 | #38 |

**按方案章节分布**：§2.6 → #1~#4；§3.5 → #5~#8；§5.3 → #9~#12；§5.7 → #13~#21；§6.5 → #22~#28；§7.3 → #29~#31；§10.1 → #32~#39。

**其中必须先修的 5 条（会直接导致功能不正确）**：#13（markdown 流式必须 `isFull=true`）、
#17（`updateCardDataByKey` 位置错误会清空卡片内容）、#10/#11（`/readyz` 谎报就绪、`reconnect_count` 假指标）、
#35（H5 免登主链路被方案标成"兼容兜底"）。

---

## 9. 照抄用的常量与最小调用序列（Python）

```python
# ---- 域名 ----
OPEN_API      = "https://api.dingtalk.com"        # 新版规范
OAPI          = "https://oapi.dingtalk.com"       # 旧版规范（topapi/*）
LOGIN         = "https://login.dingtalk.com"      # OAuth2 登录页（本场景不需要）

# ---- 端点（全部已核实）----
EP_APP_TOKEN      = f"{OPEN_API}/v1.0/oauth2/accessToken"        # POST appKey/appSecret -> accessToken/expireIn(7200)
EP_USER_TOKEN     = f"{OPEN_API}/v1.0/oauth2/userAccessToken"    # POST clientId/clientSecret/code|refreshToken/grantType
EP_STREAM_OPEN    = f"{OPEN_API}/v1.0/gateway/connections/open"  # SDK 内部使用
EP_ROBOT_OTO      = f"{OPEN_API}/v1.0/robot/oToMessages/batchSend"     # POST robotCode/userIds(<=20)/msgKey/msgParam
EP_ROBOT_GROUP    = f"{OPEN_API}/v1.0/robot/groupMessages/send"        # POST msgParam(<=15000B)/msgKey/openConversationId/robotCode
EP_ROBOT_FILE     = f"{OPEN_API}/v1.0/robot/messageFiles/download"     # POST downloadCode/robotCode -> downloadUrl
EP_CARD_CREATE    = f"{OPEN_API}/v1.0/card/instances"                  # POST
EP_CARD_DELIVER   = f"{OPEN_API}/v1.0/card/instances/deliver"          # POST
EP_CARD_CREATE_DELIVER = f"{OPEN_API}/v1.0/card/instances/createAndDeliver"  # POST（推荐一步版）
EP_CARD_UPDATE    = f"{OPEN_API}/v1.0/card/instances"                  # PUT（cardUpdateOptions.updateCardDataByKey）
EP_CARD_STREAM    = f"{OPEN_API}/v1.0/card/streaming"                  # PUT outTrackId/guid/key/content/isFull/isFinalize/isError
EP_CONTACT_ME     = f"{OPEN_API}/v1.0/contact/users/{{unionId}}"       # GET（userAccessToken；unionId 可传 me）
EP_EMP_LEAVE      = f"{OPEN_API}/v1.0/contact/empLeaveRecords"         # GET startTime/endTime
EP_GETUSERINFO    = f"{OAPI}/topapi/v2/user/getuserinfo"               # POST ?access_token=  code -> userid/unionid/name
EP_USER_GET       = f"{OAPI}/topapi/v2/user/get"                       # POST ?access_token=  userid
EP_DEPT_LISTSUB   = f"{OAPI}/topapi/v2/department/listsub"             # POST ?access_token=  dept_id(默认根)
EP_DEPT_GET       = f"{OAPI}/topapi/v2/department/get"                 # POST ?access_token=  dept_id
EP_DEPT_USER_LIST = f"{OAPI}/topapi/v2/user/list"                      # POST ?access_token=  dept_id/cursor/size
EP_USER_BY_UNION  = f"{OAPI}/topapi/user/getbyunionid"                 # POST ?access_token=  unionid -> userid
EP_LEGACY_TOKEN   = f"{OAPI}/gettoken"                                 # GET appkey/appsecret（历史文档，勿用）

# ---- Header 常量 ----
H_TOKEN = "x-acs-dingtalk-access-token"      # 新版规范一律用这个头
ROOT_DEPT_ID = 1                             # 根部门 ID

# ---- Stream topic ----
TOPIC_BOT_MESSAGE  = "/v1.0/im/bot/messages/get"
TOPIC_CARD_CALLBACK = "/v1.0/card/instances/callback"

# ---- 卡片流式权限点 ----
PERM_CARD_STREAMING_WRITE = "Card.Streaming.Write"

# ---- 内容硬约束 ----
CARD_STREAM_CONTENT_MAX_SINGLE = 1024        # 单次 <= 1KB
CARD_STREAM_CONTENT_MAX_TOTAL  = 3072        # 总量建议 <= 3KB
CARD_PARAM_KEY_MAX   = 100                   # 字节
CARD_PARAM_VALUE_MAX = 1024                  # 字节
GROUP_MSGPARAM_MAX   = 15000                 # 字节
OTO_USERIDS_MAX      = 20                    # 个/次（错误码表写 100，取严）
PAGINATION_CURSOR_START = 0                  # topapi/v2/user/list 游标起点
```

**端到端最小序列（免登 → 问答 → 卡片流式）**

```
# A. 网页应用免登
前端  dd.requestAuthCode({corpId, clientId}) → {code}
后端  1) POST /v1.0/oauth2/accessToken {appKey, appSecret} → accessToken   （缓存 7000s）
      2) POST oapi/topapi/v2/user/getuserinfo?access_token=…  body: code=… → result.userid / result.unionid
      3) POST oapi/topapi/v2/user/get?access_token=…        body: userid=…  → 完整用户画像
      4) 本地建会话（JWT 等），身份只取服务端解析结果，永不信任前端传参

# B. Stream 接消息（机器人）
DingTalkStreamClient(Credential(client_id=<AppKey>, client_secret=<AppSecret>))
  .register_callback_handler(TOPIC_BOT_MESSAGE, ChatbotHandler)
  .register_callback_handler(TOPIC_CARD_CALLBACK, CardHandler)
  .register_all_event_handler(OrgEventHandler)      # 通讯录事件
  .start_forever()                                   # 心跳 60s / 断线自动重连
handler.process(): 立刻 return (200, "OK")，重活丢后台 task

# C. AI 卡片流式回答
1) POST /v1.0/card/instances/createAndDeliver  {cardTemplateId, outTrackId(=guid 稳定值),
     cardData:{cardParamMap:{<流式变量名>: ""}},
     callbackType:"STREAM",
     openSpaceId:"dtv1.card//IM_GROUP.{conversationId}" | "dtv1.card//IM_ROBOT.{senderStaffId}",
     imGroupOpenDeliverModel:{robotCode}, imRobotOpenDeliverModel:{spaceType:"IM_ROBOT"},
     userIdType:1}
2) 反复  PUT /v1.0/card/streaming {outTrackId, guid:<每次新 uuid1>,
     key:<流式变量名>, content:<全量文本>, isFull:true, isFinalize:false, isError:false}
   ※ markdown 变量必须 isFull=true；单次 <=1KB
3) 收尾  PUT /v1.0/card/streaming {…, content:<最终全量>, isFull:true, isFinalize:true}
   失败  PUT /v1.0/card/streaming {…, content:"<错误提示>", isFull:true, isError:true}
4) 非流式补充  PUT /v1.0/card/instances {outTrackId, cardData:{cardParamMap:{<其它变量>: …}},
     cardUpdateOptions:{updateCardDataByKey:true}}     ← 局部更新，必须带 cardUpdateOptions
```

---

## 10. 参考 URL 汇总（本文引用到的全部页面）

**凭证与规范**
- <https://open.dingtalk.com/document/development/obtain-the-access-token-of-an-internal-app>（新版 accessToken）
- <https://open.dingtalk.com/document/development/api-gettoken>（`/v1.0/oauth2/{corpId}/token`）
- <https://open.dingtalk.com/document/development/obtain-orgapp-token>（旧版 `gettoken`，历史文档）
- <https://open.dingtalk.com/document/development/differences-between-server-apis-and-new-server-apis>（新旧版区别）
- <https://open.dingtalk.com/document/development/server-api-calling-guide>（API 调用步骤详解）
- <https://open.dingtalk.com/document/development/call-frequency-limit>（调用频率限制）
- <https://open.dingtalk.com/document/development/server-api-error-codes-1>（全局错误码）

**Stream 与事件**
- <https://open.dingtalk.com/document/development/develop-stream-mode-push-server>（开发事件推送服务：SDK/topic/ACK/错误码）
- <https://open.dingtalk.com/document/development/configure-stream-push>（配置事件推送方式）
- <https://open.dingtalk.com/document/development/receive-message>（接收消息协议：报文字段/conversationType/@）
- <https://open.dingtalk.com/document/org/event-list-1>（事件列表：`user_*` / `org_dept_*`）
- <https://github.com/open-dingtalk/dingtalk-stream-sdk-python> ／ <https://pypi.org/project/dingtalk-stream/>（SDK 0.24.3）

**机器人消息与卡片**
- <https://open.dingtalk.com/document/development/chatbots-send-one-on-one-chat-messages-in-batches>
- <https://open.dingtalk.com/document/development/the-robot-sends-a-group-message>
- <https://open.dingtalk.com/document/development/robot-message-type>（msgKey 与消息类型）
- <https://open.dingtalk.com/document/development/download-the-file-content-of-the-robot-receiving-message>
- <https://open.dingtalk.com/document/development/interface-for-creating-a-card-instance>
- <https://open.dingtalk.com/document/development/delivery-card-interface>
- <https://open.dingtalk.com/document/development/create-and-deliver-cards>
- <https://open.dingtalk.com/document/development/interactive-card-update-interface>
- <https://open.dingtalk.com/document/development/api-streamingupdate>（**AI 卡片流式更新**）
- <https://open.dingtalk.com/document/development/ai-card-template>（AI 卡片状态）
- <https://open.dingtalk.com/document/development/typewriter-effect-streaming-ai-card>（打字机效果完整教程 + Python 示例）
- <https://open.dingtalk.com/document/development/event-callback-card>（卡片回调）
- <https://open.dingtalk.com/document/development/robots-send-interactive-cards>（机器人发送互动卡片·普通版）

**免登**
- <https://open.dingtalk.com/document/development/jsapi-request-auth-code>（`dd.requestAuthCode`）
- <https://open.dingtalk.com/document/development/jsapi-get-auth-code>（`dd.getAuthCode`，小程序）
- <https://open.dingtalk.com/document/development/enterprise-internal-application-logon-free>（网页应用免登流程）
- <https://open.dingtalk.com/document/development/obtain-the-userid-of-a-user-by-using-the-log-free>（`topapi/v2/user/getuserinfo`）
- <https://open.dingtalk.com/document/development/dingtalk-retrieve-user-information>（`/v1.0/contact/users/{unionId}`）
- <https://open.dingtalk.com/document/development/obtain-user-token>（`/v1.0/oauth2/userAccessToken`）
- <https://open.dingtalk.com/document/development/obtain-identity-credentials>（OAuth2 授权码）

**通讯录**
- <https://open.dingtalk.com/document/development/user-management-acquires-the-list-departments>
- <https://open.dingtalk.com/document/development/query-department-details0-v2>
- <https://open.dingtalk.com/document/development/queries-the-complete-information-of-a-department-user>
- <https://open.dingtalk.com/document/development/query-the-list-of-department-userids>
- <https://open.dingtalk.com/document/development/query-user-details>
- <https://open.dingtalk.com/document/development/query-a-user-by-the-union-id>
- <https://open.dingtalk.com/document/development/query-the-details-of-employees-who-have-left-office>
- <https://help.dingtalk.io/zh/open/development/common-errors>（userId 与 unionId 区别；50002 与离职）

**索引入口（强烈建议收藏）**
- <https://open.dingtalk.com/llms.txt>（官方 LLM 文档索引）
- <https://open.dingtalk.com/llms-docs/zh-CN/llms-api-catalog.txt>（全量 API 目录）
- <https://open.dingtalk.com/llms-docs/zh-CN/llms-contacts-and-organization.txt>、`llms-robots-and-messaging.txt`（产品线索引）
