# 40 · 云端模型调用契约（阿里云百炼 / DashScope）

> 用途：写 Python 客户端前照着核对的**当前真实契约**。本文只写「怎么调、字段在哪、多少钱、怎么重试」，
> 复刻自阿里云百炼官方文档 + 本仓库已安装的 `dashscope==1.20.14` SDK 源码。
> **每条结论后括注依据 URL**；文档未说明的一律写「**未核实到**」，不猜。
> 待确认项集中在 §8（**8 条**）；与本项目现状的冲突集中在 §7（**13 条**）。
>
> 涉及模型：**以配置为准**（嵌入 `models.embed.model`、重排 `retrieval.rerank.model`、生成 `models.llm.model`）；
> 当前 cloud/deploy 实际 = `qwen3.7-text-embedding-flash`（dim=1024）/ `qwen3.7-text-rerank` / `qwen-max`。

---

## 0. 结论速览

| 事项 | 结论 | 依据 |
|---|---|---|
| `text-embedding-v4` 走 OpenAI 兼容 | ✅ 可用（`/compatible-mode/v1/embeddings`） | <https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api> |
| `text-embedding-v4` 走 DashScope 原生 HTTP | ✅ 可用 | 同上 |
| `gte-rerank-v2` 走 **OpenAI 兼容模式** | ❌ **不可用**。`/compatible-mode/v1` 下**没有 rerank 路由**；rerank 的 OpenAI 风格入口是 `/compatible-**api**/v1/reranks`（**不是** `compatible-mode`），且**只接受 `qwen3-rerank`** | <https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope>、<https://help.aliyun.com/zh/model-studio/rerank>、<https://help.aliyun.com/zh/model-studio/text-rerank-api> |
| `gte-rerank-v2` 唯一可用调用方式 | DashScope 原生 HTTP + dashscope SDK（`/api/v1/services/rerank/text-rerank/text-rerank`） | <https://help.aliyun.com/zh/model-studio/text-rerank-api> |
| `dimensions` 合法取值（v4） | `2048 / 1536 / 1024（默认）/ 768 / 512 / 256 / 128 / 64`，**1024 合法且是默认值** | <https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api> |
| 向量是否已归一化 | **未核实到**（官方文档未就「v4 输出向量是否 L2 归一化」给出说明） | — |
| `relevance_score` 范围 | `0.0 ~ 1.0`，且官方明确「**请求内相对分数**，不可跨请求比较」 | <https://help.aliyun.com/zh/model-studio/text-rerank-api> |
| 是否 sigmoid 归一化 | **未核实到** | — |
| `gte-rerank-v2` 生命周期 | ⚠️ **2026-05-30 下线**，官方推荐换 `qwen3-rerank` | <https://www.aliyun.com/notice/118217>、<https://help.aliyun.com/zh/model-studio/text-rerank-api> |
| 单请求嵌入条数上限（v4） | **10 条**（本仓库 `batch_size=20` 超限，见 §7-D1） | <https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api> |
| 批量（Batch File API）折扣 | 实时价 **5 折**；但**新加坡不支持**嵌入批处理 | <https://www.alibabacloud.com/help/en/model-studio/batch-interfaces-compatible-with-openai> |
| 默认限流（华北2 北京） | 嵌入 **1800 RPM / 1.2M TPM**；rerank **5040 RPM / 4.98B TPM**；`qwen-max` **1200 RPM / 1M TPM** | 各模型页：<https://help.aliyun.com/zh/model-studio/text-embedding-v4>、<https://help.aliyun.com/zh/model-studio/gte-rerank-v2>、<https://help.aliyun.com/zh/model-studio/qwen-max> |
| 官方重试算法 | **指数退避 + 随机抖动**，`base=1s`、`max=60s`、上限 5~6 次；仅 **429 / 500 / 503 / 连接错误** 可重试 | <https://help.aliyun.com/zh/model-studio/rate-limiting-best-practices> |
| `qwen-max` 当前单价（北京） | 输入 **2.4 元**、输出 **9.6 元** / 百万 token；无阶梯计价；Batch 5 折 | <https://help.aliyun.com/zh/model-studio/qwen-max> |
| 免费额度 | 仅**华北2（北京）**有；嵌入/LLM 各 100 万 Token，开通后 **90 天**内有效；新加坡无 | <https://help.aliyun.com/zh/model-studio/new-free-quota> |

---

## 1. 认证、地域与 base_url

### 1.1 API Key

| 项 | 内容 | 依据 |
|---|---|---|
| 获取入口 | 百炼控制台 → API Key 页：<https://bailian.console.aliyun.com/cn-beijing/model/settings/api-key>（页内右上角**先选地域**） | <https://help.aliyun.com/zh/model-studio/get-api-key> |
| 开通前提 | 需用**阿里云主账号**开通百炼；若提示「您尚未进行实名认证」，**必须先完成实名认证** | <https://help.aliyun.com/zh/model-studio/first-api-call-to-qwen> |
| 环境变量 | `DASHSCOPE_API_KEY`（OpenAI SDK 与 DashScope SDK 通用） | <https://help.aliyun.com/zh/model-studio/get-api-key> |
| 有效期 | API Key 本身无失效时间；临时 API Key 最长 1800 秒 | <https://help.aliyun.com/zh/model-studio/get-api-key> |
| 数量上限 | 单业务空间 20 个；单主账号 20 个业务空间 | <https://help.aliyun.com/zh/model-studio/get-api-key> |

**⚠️ 地域绑定（这是 401 的头号原因）**：API Key **按地域独立**，调用某地域 endpoint 必须用**同地域**创建的 Key。
用北京 Key 调新加坡 endpoint 会返回 **HTTP 401**，错误码 `invalid_api_key`，消息 `Incorrect API key provided`——
这表示**地域不匹配**，不是 Key 失效。
（依据：<https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope>）

### 1.2 base_url / endpoint（按量付费）

`{WorkspaceId}` = 业务空间 ID，在百炼控制台「业务空间详情」查看。**北京 / 新加坡 / 东京 / 法兰克福 / 中国香港**
地域的专属域名必须填 WorkspaceId。

| 地域 | OpenAI 兼容 base_url | DashScope 原生前缀 |
|---|---|---|
| 华北2（北京）· 共享域名 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `https://dashscope.aliyuncs.com/api/v1` |
| 华北2（北京）· 业务空间专属 | `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` | `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1` |
| 新加坡 · 共享域名 | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | `https://dashscope-intl.aliyuncs.com/api/v1` |
| 新加坡 · 业务空间专属 | `https://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1` | `https://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/api/v1` |
| 美国（弗吉尼亚） | `https://dashscope-us.aliyuncs.com/compatible-mode/v1` 或 `https://{WorkspaceId}.us-east-1.maas.aliyuncs.com/compatible-mode/v1` | 对应 `/api/v1` |
| 中国香港 | `https://cn-hongkong.dashscope.aliyuncs.com/compatible-mode/v1` 或 `https://{WorkspaceId}.cn-hongkong.maas.aliyuncs.com/compatible-mode/v1` | 对应 `/api/v1` |
| 日本（东京） | `https://{WorkspaceId}.ap-northeast-1.maas.aliyuncs.com/compatible-mode/v1` | 对应 `/api/v1` |
| 德国（法兰克福） | `https://{WorkspaceId}.eu-central-1.maas.aliyuncs.com/compatible-mode/v1` | 对应 `/api/v1` |
| 试用域名 | `https://trial.{region}.maas.aliyuncs.com/compatible-mode/v1`（限流更小） | — |

依据：<https://help.aliyun.com/zh/model-studio/base-url>、<https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope>

**该填哪个？** 官方口径：DashScope 共享域名（`dashscope.aliyuncs.com` 等）**当前可继续使用**，
但**推荐迁移到业务空间专属域名**（`{WorkspaceId}.{region}.maas.aliyuncs.com`），
理由是高吞吐、低时延、业务空间级流量隔离。
本项目为单机内网部署、单业务空间，**建议直接用业务空间专属域名**；若图省事可先用共享域名，两者契约一致。
（依据：<https://help.aliyun.com/zh/model-studio/base-url>）

### 1.3 SDK 默认 base_url（实测）

本仓库已安装 `dashscope==1.20.14`，其默认 HTTP 入口**不是**工作空间专属域名：

```python
# .venv/lib/python3.11/site-packages/dashscope/common/env.py:15-17
base_http_api_url = os.environ.get('DASHSCOPE_HTTP_BASE_URL',
    'https://dashscope.aliyuncs.com/api/%s' % (api_version))   # → https://dashscope.aliyuncs.com/api/v1
```

所以用 SDK 时若要走业务空间专属域名，**必须显式**赋 `dashscope.base_http_api_url = "https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1"`。
（依据：本仓库 `.venv/.../dashscope/common/env.py`；官方示例亦如此写：<https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api>）

---

## 2. 嵌入 `text-embedding-v4`

### 2.0 硬限制（北京）

| 项 | 值 | 依据 |
|---|---|---|
| 单行最大 Token | 8,192 | <https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api> |
| 单请求最大行数 | **10**（输入为字符串列表或文件时） | 同上 |
| 输入为单个字符串时 | 最长 8,192 Token，返回 1 个向量 | 同上 |
| 批量推理 | 北京支持；**新加坡不支持** | <https://help.aliyun.com/zh/model-studio/text-embedding-v4> |
| 最大 Token 数（模型概览口径） | 8,192 | <https://help.aliyun.com/zh/model-studio/embedding-rerank-model> |

> 超行数会报 400，消息形如 `Value error, batch size is invalid, it should not be larger than xxx.`
> （依据：<https://www.alibabacloud.com/help/en/model-studio/error-code>）

### 2.1 方式 A：OpenAI 兼容模式（**官方推荐**）

官方在「同步接口 API 详情」里把 OpenAI 兼容放在 DashScope 原生之前，并说明
「若熟悉 OpenAI 生态，可使用兼容 API 快速迁移；DashScope API 则提供更丰富的独有特性」。
（依据：<https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api>）

```
POST https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/embeddings
Authorization: Bearer $DASHSCOPE_API_KEY
Content-Type: application/json
```

请求体：

```json
{
  "model": "text-embedding-v4",
  "input": "待向量化文本",
  "dimensions": 1024,
  "encoding_format": "float"
}
```

| 字段 | 类型 | 必选 | 说明 |
|---|---|---|---|
| `model` | string | 是 | `text-embedding-v4` |
| `input` | `string` \| `array<string>` \| `file` | 是 | 单条字符串，或**最多 10 条**的字符串列表 |
| `dimensions` | integer | 否 | **注意复数**。`2048`/`1536`/`1024`/`768`/`512`/`256`/`128`/`64`；默认 `1024`。`2048`、`1536` 仅 `text-embedding-v4` 支持 |
| `encoding_format` | string | 否 | `float` 或 `base64`。**返回格式取决于路由到的网关**：老网关无论传什么**只返回 float**；新网关短请求按设置返回，长请求路由到老网关仍只返回 float |

（依据：<https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api>）

OpenAI Python SDK：

```python
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.getenv("DASHSCOPE_API_KEY"),
    base_url="https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
)
completion = client.embeddings.create(
    model="text-embedding-v4",
    input=["text-1", "text-2"],   # ≤10 条
    dimensions=1024,
    encoding_format="float",
)
```

响应（**已核实**，官方完整示例）：

```json
{
  "data": [
    { "embedding": [0.0023064255, -0.009327292, "..."], "index": 0, "object": "embedding" }
  ],
  "model": "text-embedding-v4",
  "object": "list",
  "usage": { "total_tokens": 23 },
  "id": "f62c2ae7-0906-9758-ab34-47c5764f07e2"
}
```

| 路径 | 说明 |
|---|---|
| `data[i].embedding` | 向量（float 数组） |
| `data[i].index` | 对应输入下标 |
| `data[i].object` | 固定 `"embedding"` |
| `object` | 固定 `"list"` |
| `model` | 回显模型名 |
| **`usage.total_tokens`** | 本次计费 token 数 ✅ **确认存在** |
| `usage.prompt_tokens` | ⚠️ **文档口径不一致**：**中文页**示例只有 `{"usage":{"total_tokens":23}}`；**英文页**示例为 `{"usage":{"prompt_tokens":23,"total_tokens":23}}`。→ 只依赖 `total_tokens` 最稳 |
| `id` | 请求 ID（此协议下叫 `id`，**不叫 `request_id`**） |

错误响应（**注意是嵌套 `error` 对象**，与原生协议的顶层 `code`/`message` 不同）：

```json
{"error": {"message": "Incorrect API key provided. ", "type": "invalid_request_error",
           "param": null, "code": "invalid_api_key"}}
```

> ⚠️ **静默失败陷阱（务必注意）**：OpenAI 兼容接口**不支持稀疏向量输出**。
> 官方原文：「使用 `text-embedding-v4` 时，如需设置 `output_type=sparse`，请调用 DashScope 接口。
> **通过 OpenAI 兼容接口传入 `output_type=sparse` 会返回 HTTP 200，但 embedding 结果为空。**」
> → 若代码写 `output_type=sparse` 却走兼容模式，会**拿到 200 + 空向量而不报错**，必须在客户端显式断言
> `len(embedding) == dimensions`。本项目用稠密向量（FAISS），**不要传 `output_type`**。
>
> ⚠️ 多模态 Embedding（`qwen3-vl-embedding`、`tongyi-embedding-vision*`）**不支持** OpenAI 兼容接口。

（依据：<https://help.aliyun.com/zh/model-studio/embedding-interfaces-compatible-with-openai>）

### 2.2 方式 B：DashScope 原生 HTTP

```
POST https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding
```

共享域名等价形态：`https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding`
（依据：<https://help.aliyun.com/zh/model-studio/embedding>、<https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api>）

请求头与请求体（**嵌套 `input` / `parameters` 结构**）：

```
Authorization: Bearer $DASHSCOPE_API_KEY
Content-Type: application/json
```

```json
{
  "model": "text-embedding-v4",
  "input": {
    "texts": ["文本1", "文本2"]
  },
  "parameters": {
    "dimension": 1024
  }
}
```

| 路径 | 说明 |
|---|---|
| `input.texts` | **必选**，字符串数组（SDK 常量 `TEXT_EMBEDDING_INPUT_KEY = 'texts'`，已实测）。≤10 条 |
| `parameters.dimension` | 可选，**单数**（官方明示「place dimension in the parameters object」）。原生协议**没有** `dimensions` |
| `parameters.text_type` | 可选，`query` \| `document`，**默认 `document`** |
| `parameters.output_type` | 可选，`dense` \| `sparse` \| `dense&sparse`，**默认 `dense`**。稀疏**只能走原生** |
| `parameters.instruct` | 可选，自定义任务指令；官方说明**仅 `text-embedding-v4` 且 `text_type=query` 时生效**，建议英文，收益约 1~5% |
| `parameters.batch_size` | **未核实到**（原生同步接口未文档化该参数；批量大小由「最多 10 条」隐式约束） |

> 依据：<https://help.aliyun.com/zh/model-studio/embedding>（curl 原文用 `"input": {"texts": [...]}`）；
> `texts` 键名与 SDK 源码交叉验证：`.venv/.../dashscope/common/constants.py:26`。
> `parameters.dimension` 由 SDK 调用链反推（见 §2.3）。

响应（原生协议）：

| 路径 | 说明 |
|---|---|
| `output.embeddings[].embedding` | 向量数组（float 列表） |
| `output.embeddings[].text_index` | 该向量对应输入 `texts` 的下标 |
| `output.embeddings[].sparse_embedding[]` | **仅当 `output_type` 含 `sparse` 时返回**；元素为 `{index, value, token}` |
| `usage.total_tokens` | 计量与计费用 |
| `request_id` | 请求唯一标识，排障用 |
| `status_code` | **SDK 层**会带（HTTP 原始响应为 HTTP 状态行） |
| `code` / `message` | 失败时返回；SDK 成功时固定为 `''` |

官方原生响应示例（稀疏调用，含 `sparse_embedding`）：

```json
{
  "status_code": 200,
  "request_id": "1ba94ac8-e058-99bc-9cc1-7fdb37940a46",
  "code": "", "message": "",
  "output": {
    "embeddings": [
      { "sparse_embedding": [ {"index":7149,"value":0.829,"token":"swift"} ],
        "embedding": [-0.006929283495992422, -0.005336422007530928, "..."],
        "text_index": 0 }
    ]
  },
  "usage": { "total_tokens": 27 }
}
```

原生协议失败响应（**扁平**，与 OpenAI 兼容的嵌套 `error` 不同）：

```json
{"code": "InvalidApiKey", "message": "Invalid API-key provided.", "request_id": "xxxxxxxx"}
```

（依据：<https://www.alibabacloud.com/help/en/model-studio/text-embedding-synchronous-api>）

> **关于共享域名（遗留域名）形态**：官方 curl 示例**只印业务空间专属域名**。
> `https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding`
> 是依据官方规则「将域名后的路径替换为 `/api/v1`」+ 已文档化的路径**拼出来的**，
> **未逐字见于官方页面**。若用共享域名，建议先做一次连通性验证。
> （依据：<https://help.aliyun.com/zh/model-studio/base-url>）

### 2.3 方式 C：dashscope Python SDK（本仓库 `1.20.14`，已实测签名）

```python
import dashscope
from http import HTTPStatus

dashscope.base_http_api_url = "https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1"

resp = dashscope.TextEmbedding.call(
    model="text-embedding-v4",
    input=["文本1", "文本2"],   # str 或 List[str]
    dimension=1024,             # 经 **kwargs → parameters.dimension
)
if resp.status_code == HTTPStatus.OK:
    vecs = [e["embedding"] for e in resp.output["embeddings"]]
```

实测签名（`.venv/lib/python3.11/site-packages/dashscope/embeddings/text_embedding.py`）：

```python
@classmethod
def call(cls, model: str, input: Union[str, List[str]],
         workspace: str = None, api_key: str = None, **kwargs) -> DashScopeAPIResponse
```

- `input` 为 `str` 时 SDK 自动包成 `{"texts": [input]}`；为 list 时 `{"texts": input}`。
- **`dimension` 不在显式形参里**，靠 `**kwargs` 透传。调用链
  `TextEmbedding.call → BaseApi.call → _build_api_request → ApiRequestData.add_parameters(**kwargs)`
  把剩余 kwargs 全部塞进请求体的 `parameters`。
  （依据：`.venv/.../dashscope/api_entities/api_request_factory.py:110` → `request_data.add_parameters(**kwargs)`）
- 因此**原生协议下参数名是单数 `dimension`**；OpenAI 兼容协议下是复数 `dimensions`。**两者不要混用。**
- URL 由模块路径推导：`services/{task_group}/{task}/{function}` =
  `services/embeddings/text-embedding/text-embedding`
  （实测 `_get_task_group_and_task('dashscope.embeddings.text_embedding') == ('embeddings','text-embedding')`）——
  与官方 curl 的 URL 完全一致，交叉验证通过。

返回对象 `DashScopeAPIResponse`（`.venv/.../dashscope/api_entities/dashscope_response.py:72-89`）：

| 属性 | 说明 |
|---|---|
| `resp.status_code` | HTTP 状态码，`200` 成功 |
| `resp.request_id` | 请求 ID |
| `resp.code` | 失败时的错误码，成功时为空串 `''` |
| `resp.message` | 失败时的错误消息，成功时为空串 `''` |
| `resp.output` | `{'embeddings': [{'embedding': [...], 'text_index': 0}, ...]}` |
| `resp.usage` | `{'total_tokens': N}` |

> ⚠️ **判成功用 `status_code`，不要用 `code`**：与 `TextReRank` 不同，`TextEmbedding.call` **不做**
> `from_api_response` 包装，直接返回原始 `DashScopeAPIResponse`（`text_embedding.py` 直接
> `return super().call(...)`；只有 `text_rerank.py` 才 `return ReRankResponse.from_api_response(response)`）。
> 因此始终以 `resp.status_code == HTTPStatus.OK` 判定，不要依赖 `resp.code == ''`。

> ⚠️ **SDK 版本差异（本项目需注意）**：本仓库 pin 的是 `dashscope==1.20.14`
> （见 `deploy/requirements.txt:55`）。该版本 `TextEmbedding.call` 的**显式形参只有**
> `model / input / workspace / api_key`，`dimension`、`text_type`、`output_type`、`instruct`
> **均靠 `**kwargs` 透传**（已实测 `1.20.14` 源码）。
> 而更新的 SDK（如 `1.27.6`）已把这些提升为**显式形参**
> （`call(model, input, workspace, api_key, text_type, dimension, output_type, instruct, **kwargs)`）。
> **两种版本下 `dimension=1024` 都能用**（老版本走 kwargs → `parameters`），行为一致；
> 但 IDE 补全与类型检查在老版本下会缺失。
> （依据：本仓库 `.venv/.../dashscope/embeddings/text_embedding.py`；
> 新版本签名对照 <https://www.alibabacloud.com/help/en/model-studio/text-embedding-synchronous-api>）

> ⚠️ **`encoding_format` 文档冲突**：**英文**同步接口页称「Currently, only `float` is supported」；
> **中文**页称入参支持 `float`/`base64`，且出参格式取决于路由到的网关（老网关恒为 float）。
> → **以「只保证 float」为设计前提**，不要依赖 base64 输出。
> （依据：<https://www.alibabacloud.com/help/en/model-studio/text-embedding-synchronous-api>、
> <https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api>）

### 2.4 维度参数（`dimensions`）

| 问题 | 答案 | 依据 |
|---|---|---|
| 合法取值 | **`2048`、`1536`、`1024`、`768`、`512`、`256`、`128`、`64`** | <https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api> |
| 默认值 | **`1024`** | 同上 |
| 1024 是否合法 | ✅ **合法，且就是默认值** | 同上 |
| 官方选型建议 | 大规模搜索省存储 → 256/512；通用 → **1024（默认，平衡效果好）**；高精度 → 1536/2048 | <https://help.aliyun.com/zh/model-studio/embedding-rerank-model> |
| 2048/1536 的限制 | 仅 `text-embedding-v4` 支持（`2560` 仅 `qwen3.7-text-embedding`） | <https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api> |
| 传非法值的错误码 | **未核实到** | — |

**→ 本项目 `dim=1024` 的决策正确且是默认值，无需改动。**

### 2.5 向量归一化

**未核实到。** 已检索 `text-embedding-v4` 模型页、同步接口 API 页、向量与重排序总览页，
**均未**就「输出向量是否已 L2 归一化 / 是否需自行归一化后再算余弦相似度」作任何说明。
（检索来源：<https://help.aliyun.com/zh/model-studio/text-embedding-v4>、
<https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api>、
<https://help.aliyun.com/zh/model-studio/embedding-rerank-model>）

> 工程建议（**非官方口径，需实测**）：FAISS 若用 `IndexFlatIP`（内积）就必须归一化。
> 稳妥做法是**自己归一化**（`v / norm(v)`）：若模型已归一化，二次归一化是无害的幂等操作；
> 若未归一化，则避免踩坑。**不要**依赖「官方已归一化」这一未经核实的假设。

### 2.6 限流（默认值）

| 地域 | RPM | TPM |
|---|---|---|
| 华北2（北京） | 1,800 | 1,200,000 |
| 新加坡 | 1,800 | 1,000,000 |
| 中国香港 | 1,800 | 1,200,000 |

依据：<https://help.aliyun.com/zh/model-studio/text-embedding-v4>

### 2.7 计费

| 计费项 | 价格（北京） | 依据 |
|---|---|---|
| 文本输入 | **0.0005 元 / 千 token**（= 0.5 元 / 百万 token） | <https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api> |
| 向量输入（Batch File） | **0.00025 元 / 千 token**（**5 折**） | 同上 |
| 新加坡文本输入 | 0.000514 元 / 千 token | 同上 |

- **按 token 计费（输入 token），不是按次**。 ✅
- **批量有折扣**：Batch File API 官方原话「Costs are only **50%** of real-time calls」。
  （依据：<https://www.alibabacloud.com/help/en/model-studio/batch-interfaces-compatible-with-openai>）
- ⚠️ 但**新加坡地域的 Batch 支持模型列表里没有 `text-embedding-v4`**（只有 qwen-max/plus/flash/turbo）；
  **北京**的 Batch 列表**包含** `text-embedding-v4`。走批量嵌入只能用北京。
  （依据：同上；及 <https://help.aliyun.com/zh/model-studio/text-embedding-v4> 的「批量推理」能力项）
- 免费额度：北京各 **100 万 Token**，有效期百炼开通后 **90 天**内；新加坡 **无免费额度**。
  ⚠️ **免费额度只覆盖实时推理，Batch 调用不计入免费额度**。
  （依据：<https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api>、
  <https://help.aliyun.com/zh/model-studio/new-free-quota>）

**Batch 调用方式**（OpenAI 兼容形态，北京 base）：上传 JSONL →
`POST {base}/files`（`purpose=batch`）→ `POST {base}/batches`（`endpoint` 填 **`/v1/embeddings`**）→
轮询 `GET {base}/batches/{id}` → 取 `output_file_id` 下载。
`completion_window` 取值 `24h` ~ `336h`。
（依据：<https://www.alibabacloud.com/help/en/model-studio/batch-interfaces-compatible-with-openai>）

---

## 3. 重排 `gte-rerank-v2`

### 3.1 OpenAI 兼容模式：**不可用**（关键结论）

**结论：`gte-rerank-v2` 不能通过 OpenAI 兼容模式调用。** 依据链：

1. 官方「OpenAI Chat 接口兼容」页的模型列表只覆盖 **Chat 类**模型
   （Qwen 大语言、Qwen-VL、Qwen-Coder、Qwen-Omni、Qwen-Math、DeepSeek、Kimi、GLM、MiniMax），
   **不含任何 rerank 模型**，并明确「Qwen-Audio 不支持 OpenAI 兼容协议，仅支持 DashScope 协议」这类例外写法。
   （依据：<https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope>）
2. 官方 rerank API 页只给出**两个** HTTP 入口，**都不在 `/compatible-mode/v1` 下**：
   - `POST {base}/api/v1/services/rerank/text-rerank/text-rerank` —— 用于
     `qwen3.7-text-rerank`、`qwen3-vl-rerank`、**`gte-rerank-v2`**
   - `POST {base}/compatible-api/v1/reranks` —— **仅**用于 `qwen3-rerank`
   （依据：<https://help.aliyun.com/zh/model-studio/text-rerank-api>）
3. **易错点（务必看清）**：第二条是 `compatible-**api**`，**不是** `compatible-**mode**`。这是两个不同的路径前缀。
   - `compatible-api/v1/reranks` **确实被官方当作「OpenAI 兼容」来呈现**：rerank 指南里该小节标题就是
     「**OpenAI 兼容**」，示例用 OpenAI SDK `client.post("/reranks", body={...}, cast_to=object)`，
     `base_url=".../compatible-api/v1"`。
     （依据：<https://help.aliyun.com/zh/model-studio/rerank>）
   - **但它被官方按模型划定了范围**：API 参考明确写
     「**qwen3-rerank**：`POST .../compatible-api/v1/reranks`」vs
     「**qwen3.7-text-rerank / qwen3-vl-rerank / gte-rerank-v2**：`POST .../api/v1/services/rerank/text-rerank/text-rerank`」，
     且指南原文「多模态重排序需要使用 DashScope SDK 或 API，**不支持 OpenAI 兼容接口**」。
     （依据：<https://help.aliyun.com/zh/model-studio/text-rerank-api>、<https://help.aliyun.com/zh/model-studio/rerank>）
   - 结论：**`compatible-api/v1/reranks` 不接收 `gte-rerank-v2`**，`gte-rerank-v2` 没有任何已文档化的 OpenAI 协议路由。
4. **官方文档自身有一处不一致（不要被误导）**：英文版 API 参考里 `qwen3-rerank` 的 curl 片段印的是
   `.../compatible-**mode**/v1/reranks`，而同一页的端点列表写的是 `compatible-**api**/v1/reranks`。
   其他地方一致使用 `compatible-api` → **以 `compatible-api` 为准**。
   （依据：<https://www.alibabacloud.com/help/en/model-studio/text-rerank-api>）

> **对本项目的直接影响**：`deploy/requirements.txt:54` 的注释写
> 「经 OpenAI 兼容协议调用云端 LLM / 嵌入 / **rerank**」——**rerank 这半句是错的**。
> 若 `ekbs/client/model_gateway.py` 用 OpenAI SDK 统一出网，rerank 必须走**独立的 HTTP 客户端**（见 §7-D2）。

### 3.2 DashScope 原生 HTTP（`gte-rerank-v2` 的唯一可用 HTTP 形）

```
POST https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank
Authorization: Bearer $DASHSCOPE_API_KEY
Content-Type: application/json
```

共享域名等价：`https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank`
（依据：<https://help.aliyun.com/zh/model-studio/text-rerank-api>）

请求体：

```json
{
  "model": "gte-rerank-v2",
  "input": {
    "query": "什么是文本排序模型",
    "documents": ["文档1", "文档2", "文档3"]
  },
  "parameters": {
    "return_documents": true,
    "top_n": 2
  }
}
```

| 路径 | 类型 | 必选 | 说明 |
|---|---|---|---|
| `model` | string | 是 | `gte-rerank-v2`（或 `qwen3.7-text-rerank` / `qwen3-rerank` / `qwen3-vl-rerank`） |
| `input.query` | string | 是 | 查询，最大 4,000 Token（`gte-rerank-v2`） |
| `input.documents` | array\<string\> | 是 | 候选文档，每元素为字符串 |
| `parameters.top_n` | int | 否 | 返回排序后的前 N 条；**默认返回全部**；指定值 > 文档总数时**返回全部** |
| `parameters.return_documents` | bool | 否 | 是否返回文档原文，**默认 `false`**（省网络开销）。支持模型：**`gte-rerank-v2`**、`qwen3-vl-rerank` |
| `parameters.instruct` | string | 否 | **仅 `qwen3.7-text-rerank` / `qwen3-rerank` / `qwen3-vl-rerank` 生效**；`gte-rerank-v2` **不支持** |
| `parameters.fps` | float | 否 | 仅 `qwen3-vl-rerank` |

（依据：<https://help.aliyun.com/zh/model-studio/text-rerank-api>）

响应体（逐字段）：

| 路径 | 类型 | 说明 |
|---|---|---|
| `output.results` | array | 按 `relevance_score` **从高到低**排列 |
| `output.results[].index` | int | 对应输入 `documents` 中的**原始下标** |
| `output.results[].relevance_score` | double | 相关性得分，**范围 0.0 ~ 1.0** |
| `output.results[].document` | dict | `{"text": "文档原文"}`；**仅当 `return_documents=true` 时返回** |
| `usage.total_tokens` | int | 本次请求消耗的 Token 总数，用于计量计费和审计 |
| `usage.prompt_tokens` | int | **仅 `qwen3.7-text-rerank` 返回**；`gte-rerank-v2` **不返回** |
| `request_id` | string | 请求唯一标识 |
| `code` | string | **仅失败时返回**，错误码 |
| `message` | string | **仅失败时返回**，错误详情 |

官方成功响应示例（`gte-rerank-v2` / `qwen3-vl-rerank` 分组）：

```json
{
  "output": {
    "results": [
      { "document": {"text": "文本排序模型广泛用于搜索引擎..."}, "index": 0, "relevance_score": 0.9334521178273196 },
      { "document": {"text": "预训练语言模型的发展..."}, "index": 2, "relevance_score": 0.34100082626411193 }
    ]
  },
  "usage": { "total_tokens": 79 },
  "request_id": "85ba5752-1900-47d2-8896-23f99b13f6e1"
}
```

官方失败响应示例：

```json
{ "code": "InvalidApiKey", "message": "Invalid API-key provided.",
  "request_id": "fb53c4ec-1c12-4fc4-a580-cdb7c3261fc1" }
```

> ⚠️ **错误响应没有 `output` 字段**，且 HTTP 状态码与 `code` 需要分别判断。见 §5。

（依据：<https://help.aliyun.com/zh/model-studio/text-rerank-api>）

### 3.3 dashscope Python SDK（已实测签名）

```python
import dashscope
from http import HTTPStatus

dashscope.base_http_api_url = "https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1"

resp = dashscope.TextReRank.call(
    model="gte-rerank-v2",
    query="什么是文本排序模型",
    documents=["文档1", "文档2", "文档3"],
    top_n=5,
    return_documents=False,
)
if resp.status_code == HTTPStatus.OK:
    for r in resp.output.results:          # 或 resp.output["results"]
        print(r.index, r.relevance_score, r.document)
```

实测签名（`.venv/.../dashscope/rerank/text_rerank.py`）：

```python
@classmethod
def call(cls, model: str, query: str, documents: List[str],
         return_documents: bool = None, top_n: int = None,
         api_key: str = None, **kwargs) -> ReRankResponse
```

- 参数名：**`model` / `query` / `documents` / `return_documents` / `top_n` / `api_key`**，均为**扁平**参数。
  官方明确：「SDK 的参数命名与 HTTP 接口基本一致，参数结构进行了一定封装。
  比如 HTTP 使用嵌套的 `input` 和 `parameters` 结构，但 SDK 使用**扁平参数**。」
  （依据：<https://help.aliyun.com/zh/model-studio/text-rerank-api>）
  > 注：官方文档的 SDK 示例**只演示了 `model`/`query`/`documents`/`top_n`/`return_documents`**，
  > **未在文档中列出 `api_key` 形参**（示例依赖环境变量 `DASHSCOPE_API_KEY`）。
  > 但本仓库实测 SDK 源码中 `api_key` **确实存在**（`text_rerank.py` 显式形参），可安全使用。
- SDK 内部构造 `input = {'query': ..., 'documents': ...}`、`parameters = {'return_documents': ..., 'top_n': ...}`，
  再把 `**kwargs` 合并进 parameters。
- `query is None or documents is None or not documents` → 抛 `InputRequired`（**客户端异常，非 HTTP 错误**）。
- 返回的是 `ReRankResponse`（经 `from_api_response` 包装），字段：
  `status_code` / `request_id` / `code` / `message` / `output.results[]` / `usage.total_tokens`。
  **成功时 `code` 和 `message` 被固定为 `''` 空串**（官方说明：「SDK 对原始 HTTP 响应进行了封装，
  成功时会固定返回 `code` 和 `message` 字段，值为空字符串」）。
- `usage` 是 `ReRankUsage`，**只有 `total_tokens` 一个字段**。

官方 SDK 输出示例：

```json
{
  "status_code": 200, "request_id": "4b0805c0-...",
  "code": "", "message": "",
  "output": { "results": [ {"index": 0, "relevance_score": 0.9334521178273196} ] },
  "usage": { "prompt_tokens": 79, "total_tokens": 79 }
}
```

（依据：<https://help.aliyun.com/zh/model-studio/text-rerank-api>；
类型定义交叉验证：`.venv/.../dashscope/api_entities/dashscope_response.py:499-553`）

### 3.4 `relevance_score` 语义

| 问题 | 答案 | 依据 |
|---|---|---|
| 取值范围 | **`0.0` ~ `1.0`**，分数越高相关性越强 | <https://help.aliyun.com/zh/model-studio/text-rerank-api> |
| 是否 sigmoid 归一化 | **未核实到**。官方只说明范围与「相对分数」，**未说明**是 sigmoid/logit 变换 | — |
| 能否跨请求比较 | ❌ **不能**。官方原文：「此分数为当前请求中的**相对分数**，主要用于对本次请求内的文档排序，**不可作为跨请求比较的绝对值**」 | <https://help.aliyun.com/zh/model-studio/text-rerank-api> |

> ⚠️ **对本项目的直接影响**：`retrieval.rerank.min_score = 0.30` 是**跨请求阈值**，
> 而官方明确说分数只在单请求内可比。这意味着「低于 0.30 即拒答」在不同 query 下**语义不稳定**：
> 同一段文本换个 query 可能分数整体抬高或压低。这不是 bug，但**阈值需要按实测分布校准**，
> 且不要把它当成「绝对相关性」。相关冲突见 `30-api.md` §7-C10（0.30 vs 0.35 两处不一致）。

### 3.5 `top_n` / `return_documents` / 条数行为

| 行为 | 结论 | 依据 |
|---|---|---|
| `top_n` 默认 | **返回全部文档** | <https://help.aliyun.com/zh/model-studio/text-rerank-api> |
| `top_n` > 文档数 | **返回全部文档**（不报错、不补空） | 同上 |
| `return_documents` 默认 | **`false`** | 同上 |
| `return_documents` 支持模型 | `gte-rerank-v2`、`qwen3-vl-rerank`（`qwen3.7-text-rerank`/`qwen3-rerank` **不支持**） | 同上 |
| **返回条数是否恒等于 `top_n`** | **未核实到**。文档未承诺「一定有 top_n 条」，也未承诺「分数不足会被丢弃」 | — |
| 最大文档数 | **500** 条 | <https://help.aliyun.com/zh/model-studio/text-rerank-api> |
| 单条（Query 或 Document）最大 Token | **4,000** | 同上 |
| 请求最大输入 Token | **30,000**（公式：`Query Tokens × Document 数量 + Document Tokens 总和`） | 同上 |
| 超**单条**上限（4,000） | **返回 HTTP 400，不截断输入内容** | 同上 |
| 超**单请求**上限（30,000） | **未核实到**。官方「HTTP 400 + 不截断」的表述挂在**单条**上限上，**未**明确覆盖单请求总量上限 | 同上 |

> ⚠️ **对本项目的直接影响**：`10-retrieval-permission.md` 要求
> 「返回条数不足即视为失败并走降级，禁止补 0 分」。由于「是否可能返回少于 `top_n` 条」**未核实到**，
> 建议实现时**按「实际返回条数」处理**，而不是断言 `len(results) == top_n`，
> 否则可能把正常响应误判为失败并触发不必要的降级。

### 3.6 生命周期：`gte-rerank-v2` 即将下线 ⚠️

**官方下线通知（已核实原文）**：

> 【大模型服务平台百炼】**GTE-RERANK 模型下线通知**
> 公告发布：2026-04-28 16:11:12
> 影响时间：**北京时间 2026-05-30 00:00:00**【以实际变更时间为准】

依据：<https://www.aliyun.com/notice/118217>

rerank API 页同时标注：

> **重要** gte-rerank 模型将于 **2026 年 05 月 30 日下线**，推荐使用 **`qwen3-rerank`** 模型替代。

依据：<https://help.aliyun.com/zh/model-studio/text-rerank-api>、<https://help.aliyun.com/zh/model-studio/rerank>

**⚠️ 一个未解的文档矛盾（如实记录，不代为判断）**：截至「Last Updated: Sep 2026」，
- **API 参考** 与 **`gte-rerank-v2` 模型页** 仍把该模型当作**可用**在写
  （完整的 curl/SDK 示例、0.8 元/百万 token 价格、RPM/TPM 表，**无任何不可用标记**）；
- 而 **rerank 指南** 已改用**过去时**：「The gte-rerank model **was discontinued** on May 30, 2026.」

因此「**`gte-rerank-v2` 今天是否还能调通**」→ **未核实到**（无任何文档明确说调用当前会失败）。
（依据：<https://help.aliyun.com/zh/model-studio/gte-rerank-v2>、
<https://www.alibabacloud.com/help/en/model-studio/rerank>、<https://www.alibabacloud.com/help/en/model-studio/text-rerank-api>）

> **上线前必须先做一次真实探活**（见 §8-U9）：若已下线，所有 rerank 路径都会失败并触发降级。

**迁移注意**：`qwen3-rerank` 的**调用契约与 `gte-rerank-v2` 完全不同**：
- 端点不同：`{base}/compatible-api/v1/reranks`（**不是** `api/v1/services/rerank/...`）
- 请求体**扁平**：`model` / `documents` / `query` / `top_n` / `instruct` **同层**，**没有** `input` / `parameters` 嵌套
- 响应**没有 `output` 外壳**：`results` 直接在**顶层**，`usage` 只有 `total_tokens`
- **不支持 `return_documents`**（改用 `instruct` 参数）
- 单条最大输入 Token 从 4,000 降到 4,000（同），但最大文档数 500（同）

（依据：<https://help.aliyun.com/zh/model-studio/text-rerank-api>）

> **建议**：本项目 `retrieval.rerank.model` 直接改用 `qwen3-rerank` 并把 client 按扁平协议实现，
> 避免 2026-05-30 后被动改代码。若必须留在 `gte-rerank-v2`，需在 §7 记一条迁移 deadline。

### 3.7 限流与计费

| 项 | 值 | 依据 |
|---|---|---|
| RPM（北京） | **5,040** | <https://help.aliyun.com/zh/model-studio/gte-rerank-v2> |
| TPM（北京） | **4,980,000,000** | 同上 |
| 计费项（北京） | 文本输入 **0.8 元 / 百万 token** | 同上 |
| 是否按输入 token 计费 | ✅ 是（`usage.total_tokens` 即 query + documents 的 token 总和） | 推断自 <https://help.aliyun.com/zh/model-studio/text-rerank-api> 的 usage 字段说明 |
| 是否按次计费 | **未核实到**（价格表只有「文本输入」一个计费项，无按次费用；但「不存在未列出的按次费用」无法证实） | <https://help.aliyun.com/zh/model-studio/gte-rerank-v2> |
| 免费额度 | **未核实到**（该模型页无「免费额度」列；通用规则「仅华北2（北京）享免费额度，每模型通常 100 万 Token、90 天有效」见 <https://help.aliyun.com/zh/model-studio/new-free-quota>，但**未点名 gte-rerank-v2**） | <https://help.aliyun.com/zh/model-studio/new-free-quota> |
| 新加坡价格 | **未核实到**（模型页只有华北2（北京）一节；rerank 模型总表也只在北京列 `gte-rerank-v2`，新加坡表里是 `qwen3-rerank`） | <https://help.aliyun.com/zh/model-studio/gte-rerank-v2> |
| 新加坡限流 | **未核实到** | — |
| 参考：`qwen3-rerank` 新加坡价 | 0.749 元 / 百万 token（若迁移可对比） | <https://help.aliyun.com/zh/model-studio/qwen3-rerank> |

---

## 4. 生成 `qwen-max`

### 4.1 状态与限流

`qwen-max` **仍在役**，有独立的、在维护的模型页；但官方「选择模型」页把它归到
**「旧版模型 > 旧版Qwen」**，该分节标题写明「**以下模型不再作为首选推荐**」。
**未找到 `qwen-max` 的下线/EOL 公告**（官方模型下线机制页只给政策：快照模型提前 30 天通知、主线模型提前 3 个月，**未点名 qwen-max**）。
`qwen-max` 是**滚动别名**：官方原文「随着模型的升级，qwen-max 将滚动更新升级……该模型版本功能等同于快照模型 `qwen-max-2025-01-25`」。
（依据：<https://help.aliyun.com/zh/model-studio/qwen-max>、<https://help.aliyun.com/zh/model-studio/text-generation-model>、<https://help.aliyun.com/zh/model-studio/model-depreciation>）

| 能力/参数 | 值 | 依据 |
|---|---|---|
| 上下文长度 | **32,768**（32k） | <https://help.aliyun.com/zh/model-studio/qwen-max> |
| 最大输入长度 | **30,720** | 同上 |
| 最大输出长度 | **8,192** | 同上 |
| Function Calling | 支持 | 同上 |
| 结构化输出 | 支持 | 同上 |
| 思考模式 | **不支持** | 同上 |

限流（含输入与输出 Token）：

| 地域 | RPM | TPM |
|---|---|---|
| 华北2（北京） | **1,200** | **1,000,000** |
| 新加坡 | 600 | 1,000,000 |
| 美国（弗吉尼亚） | 600 | 1,000,000 |

（依据：<https://help.aliyun.com/zh/model-studio/rate-limit>、<https://help.aliyun.com/zh/model-studio/qwen-max>；
`qwen-max` 行附注「Use Batch API 调用时不受限流约束」）

**`qwen-max` 能否走 OpenAI 兼容协议？** → **官方没有一句话直接点名**，但有三条强证据说明可以：
1. OpenAI 兼容页的 **Function Calling** 分节原文：「当前支持的模型包括 **qwen-turbo、qwen-plus 和 qwen-max**」；
2. OpenAI 兼容 **Batch API** 的支持模型列表在**北京与新加坡都明确列出 `qwen-max`**；
3. 兼容页的支持范围写「Qwen 大语言模型（商业版、开源版）」，`qwen-max` 属商业版 Qwen LLM。
（依据：<https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope>、
<https://www.alibabacloud.com/help/en/model-studio/batch-interfaces-compatible-with-openai>）
> 反例：若某模型不支持兼容模式，会返回 **404 `model_not_supported`**，消息
> `Unsupported model xxx for OpenAI compatibility mode.`，提示改用 DashScope 原生协议。
> （依据：<https://help.aliyun.com/zh/model-studio/error-code>）

> **新一代替换建议**：官方 quickstart 与兼容页示例**已全部改用 `qwen3.8-max`**。
> 若追求效果与长期维护，建议评估 `qwen3-max` / `qwen3.8-max`；但 `qwen-max` 当前可用，
> **不构成阻塞**。（依据：<https://help.aliyun.com/zh/model-studio/first-api-call-to-qwen>）

### 4.2 OpenAI 兼容模式（**官方推荐**）

```
POST https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions
Authorization: Bearer $DASHSCOPE_API_KEY
```

请求体（与 OpenAI 对齐的参数，官方已支持列表）：
`model`、`messages`、`top_p`、`temperature`、`presence_penalty`、`n`、`max_tokens`、`seed`、
`stream`、`stream_options.include_usage`、`stop`、`tools` 等。

约束：`messages[0]` 才支持 `role=system`；最后一个元素 `role` 必须为 `user`；
`temperature` 取值 `[0, 2)`；`top_p` 取值 `(0, 1.0]`。

响应字段路径（官方示例）：

| 路径 | 说明 |
|---|---|
| `id` | `chatcmpl-xxx` |
| `object` | `chat.completion` |
| `model` | 回显模型名 |
| `choices[0].message.content` | 生成文本 |
| `choices[0].message.role` | `assistant` |
| `choices[0].finish_reason` | `stop` 等 |
| `choices[0].index` | `0` |
| `usage.prompt_tokens` | 输入 token |
| `usage.completion_tokens` | 输出 token |
| `usage.total_tokens` | 合计 |

流式：`stream=True`，`object` 为 `chat.completion.chunk`，增量在 `choices[0].delta.content`；
**usage 只在最后一个 chunk 出现**（需 `stream_options={"include_usage": True}`，该 chunk 的 `choices` 为空数组）；
流以 `data: [DONE]` 结束。

错误包络（OpenAI 兼容协议，**注意是嵌套 `error` 对象**）：

```json
{"error": {"message": "Invalid API-key provided.", "type": "invalid_request_error",
           "param": null, "code": "invalid_api_key"}}
```

官方 OpenAI 兼容状态码表：

| 状态码 | 消息 | 含义 | 可重试 |
|---|---|---|---|
| 400 | Invalid Request Error | 请求非法 | ❌ |
| 401 | Invalid API-key provided | 鉴权失败 | ❌ |
| 429 | Rate limit reached for requests | QPS/QPM 超限 | ✅ |
| 429 | You exceeded your current quota, please check your plan and billing details | 额度超限或欠费 | ⚠️ 视原因 |
| 500 | The server had an error while processing your request | 服务端错误 | ✅ |
| 503 | The engine is currently overloaded, please try again later | 服务端负载过高，**可重试** | ✅ |

（依据：<https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope>、
<https://help.aliyun.com/zh/model-studio/stream>、
<https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions>）

### 4.3 DashScope 原生 HTTP

```
POST https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/text-generation/generation
Authorization: Bearer $DASHSCOPE_API_KEY
```

URL 交叉验证：SDK 里 `_get_task_group_and_task('dashscope.aigc.generation') == ('aigc', 'generation')`，
且 `Generation.task = 'text-generation'`，拼出 `services/aigc/text-generation/generation`，与官方一致。

请求体为嵌套结构：`input.messages` + `parameters.*`（`result_format`、`incremental_output` 等）。
官方明确：**通过 HTTP 调用时，必须把 `messages` 放进 `input` 对象中**。

```json
{
  "model": "qwen-max",
  "input": { "messages": [ {"role": "user", "content": "你是谁？"} ] },
  "parameters": { "result_format": "message", "incremental_output": true }
}
```

流式需加请求头 `X-DashScope-SSE: enable`。

响应（原生协议，**已核实**，取自官方文本生成端点的 `data:` 行）：

| 路径 | 说明 |
|---|---|
| `output.choices[0].message.content` | 生成文本 |
| `output.choices[0].message.role` | `assistant` |
| `output.choices[0].finish_reason` | 结束原因 |
| **`usage.input_tokens`** | 输入 token |
| **`usage.output_tokens`** | 输出 token |
| **`usage.total_tokens`** | 合计 |
| `usage.prompt_tokens_details.cached_tokens` | 命中缓存的输入 token |
| `request_id` | 请求 ID |
| `code` / `message` | **仅失败时返回** |

官方非流式响应示例：

```json
{"output":{"choices":[{"finish_reason":"stop","message":{"role":"assistant",
  "content":"我是来自阿里云的大规模语言模型，我叫千问。"}}]},
 "usage":{"total_tokens":38,"output_tokens":16,"input_tokens":22},
 "request_id":"87f776d7-3c82-9d39-b238-d1ad38c9b6a9"}
```

> ⚠️ **口径差异（已核实）**：原生协议用 **`input_tokens` / `output_tokens` / `total_tokens`**，
> **不是** `prompt_tokens` / `completion_tokens`；后者专属 OpenAI 兼容协议。核算成本时**不要混用**。

（依据：<https://help.aliyun.com/zh/model-studio/qwen-api-via-dashscope>、
<https://help.aliyun.com/zh/model-studio/stream>、
<https://help.aliyun.com/zh/model-studio/first-api-call-to-qwen>）

### 4.4 dashscope Python SDK（已实测签名）

```python
from http import HTTPStatus
import dashscope

resp = dashscope.Generation.call(
    model="qwen-max",
    messages=[{"role": "user", "content": "你好"}],
    result_format="message",
    temperature=0.1,
    top_p=0.8,
    max_tokens=2048,
)
if resp.status_code == HTTPStatus.OK:
    print(resp.output.choices[0].message.content)
```

实测签名（`.venv/.../dashscope/aigc/generation.py:29-38`）：

```python
@classmethod
def call(cls, model: str, prompt: Any = None, history: list = None,
         api_key: str = None, messages: List[Message] = None,
         plugins: Union[str, Dict[str, Any]] = None, workspace: str = None,
         **kwargs) -> Union[GenerationResponse, Generator[GenerationResponse, None, None]]
```

- `prompt` 与 `messages` **至少给一个**，否则抛 `InputRequired('prompt or messages is required!')`。
- `stream=True` 时返回 **generator**，否则返回 `GenerationResponse`。
- SDK 内 `Generation.Models.qwen_max = 'qwen-max'`，SDK 认这个名字（已实测）。
- 其余参数（`temperature` / `top_p` / `max_tokens` / `result_format` / `seed` / `stop` /
  `repetition_penalty` / `enable_search` / `incremental_output` 等）经 `**kwargs` 进入 `parameters`。

### 4.5 计费（已核实）

单位为 **元 / 百万 token**，均为**列表原价**（不含限时优惠）。**无阶梯计价**。

| 计费项 | 华北2（北京） | 新加坡 |
|---|---|---|
| 输入 | **2.4** | **11.743** |
| 输出 | **9.6** | **46.971** |
| 输入（命中缓存） | 0.48 | 2.349 |
| Batch File 输入 | 1.2（**5 折**） | 5.871 |
| Batch File 输出 | 4.8（**5 折**） | 23.486 |

- 免费额度：**北京 100 万 Token**（开通后 90 天内有效）；**新加坡无免费额度**。
- Batch 折扣：官方统一口径为实时价 **50%**；北京与新加坡的 Batch 支持模型列表**都包含 `qwen-max`**。

（依据：<https://help.aliyun.com/zh/model-studio/qwen-max>、
<https://help.aliyun.com/zh/model-studio/model-pricing>、
<https://www.alibabacloud.com/help/en/model-studio/batch-interfaces-compatible-with-openai>）

> **成本速算（北京）**：每 1M 输入 ≈ 2.4 元、每 1M 输出 ≈ 9.6 元。
> 本项目 `LLMCfg.max_tokens = 2048`、`temperature=0.1`、上下文上限 8192（见 `05`），
> 单次问答成本量级 = `(提示 token × 2.4 + 输出 token × 9.6) / 1e6` 元。

---

## 5. 错误码与退避重试策略

### 5.1 平台级限流机制

- 限流按**阿里云主账号**维度计算，**聚合**该账号下所有 RAM 子账号、业务空间、API Key 的调用量。
- **不同模型有独立的限流配额**。
- 除 RPM/TPM 外，还可能按 **RPS（RPM/60）与 TPS（TPM/60）** 执行；
  **即使每分钟总量未超，短时突发也会触发限流**。
- 触发后**通常 1 分钟内自动恢复**。
- 充值**不会**提高默认 RPM/TPM；需要更高配额要联系商务经理。
- 限流只拒绝超限请求（返回 **429**），不影响已接受请求的生成速度。

（依据：<https://www.alibabacloud.com/help/en/model-studio/rate-limit>）

### 5.2 限流的三种错误消息（据此区分退避策略）

| 错误消息 | 触发的限流类型 | 应对 |
|---|---|---|
| `Requests rate limit exceeded` 或 `You exceeded your current requests list` | **RPM** 限流 | 降低调用频率 |
| `Allocated quota exceeded` 或 `You exceeded your current quota` | **TPM** 限流 | 缩短输入或限制输出长度 |
| `Request rate increased too quickly` | **短时频率突增**（系统稳定性保护），**即使总量未超 RPM/TPM 也会触发** | **匀速调度**、指数退避、请求队列，削平瞬时峰值 |

（依据：<https://www.alibabacloud.com/help/en/model-studio/rate-limit>）

> 第三种最关键：本项目入库时会批量并发调嵌入，**必须做匀速/信号量限流**，
> 否则即使 RPM 1800 远未打满也会被拒。

### 5.3 HTTP 状态码语义

| 状态码 | 含义 | 是否可重试 |
|---|---|---|
| **200** | 成功 | — |
| **400** | 参数错误 / 输入超限 / 欠费 / 内容审核拦截 | 多半**不可重试**，需修参数或改内容 |
| **401** | 鉴权失败（`InvalidApiKey` / `invalid_api_key`），含**地域不匹配** | ❌ 不可重试 |
| **403** | **免费额度耗尽**（`AllocationQuota.FreeTierOnly`，仅开通「免费额度用完即停」时） | ❌ 重试无用，需关闭该模式 |
| **404** | `ModelNotFound` / `model_not_found`、`model_not_supported`（模型不支持 OpenAI 兼容模式） | ❌ 不可重试 |
| **413** | 请求体超网关大小限制（>16 MiB 等） | ❌ 不可重试，需切分 |
| **429** | **限流**（`Throttling*`） | ✅ **可重试**（指数退避 + 抖动） |
| **500** | `RequestTimeOut` / `InternalError*` / `ServiceUnavailable` | ✅ **可重试** |
| **503** | `ServiceUnavailable` / `ModelUnavailable` / 引擎过载 | ✅ **可重试** |

官方原文口径：「**429 为速率限制，403 为配额耗尽**，两者都是拒绝机制。」
`403 AllocationQuota.FreeTierOnly` 的消息：
`The free tier of the model has been exhausted. If you wish to continue access the model on a paid basis,
please disable the "use free tier only" mode in the management console.`

（依据：<https://help.aliyun.com/zh/model-studio/rate-limit>、
<https://help.aliyun.com/zh/model-studio/error-code>、
<https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope>）

### 5.4 错误结构（两条协议不同）

| 协议 | 错误字段位置 |
|---|---|
| DashScope 原生（rerank/embedding/generation） | 响应体顶层 `code` + `message` + `request_id`；**没有 `output`** |
| OpenAI 兼容（chat/embeddings） | OpenAI 标准错误体；DashScope 侧错误码如 `invalid_api_key`、`invalid_request_error` |
| dashscope SDK | `resp.status_code` + `resp.code` + `resp.message`（`ReRankResponse`/`GenerationResponse` 成功时 `code`/`message` 为 `''`） |

（依据：<https://help.aliyun.com/zh/model-studio/text-rerank-api> 的失败响应示例；
`.venv/.../dashscope/api_entities/dashscope_response.py`）

### 5.5 错误码清单与可重试性（修参数/换内容才有用）

| 错误码 | HTTP | 含义 | 依据 |
|---|---|---|---|
| `InvalidParameter` / `InternalError.Algo.InvalidParameter` | 400 | 参数非法（函数名前缀见下） | <https://www.alibabacloud.com/help/en/model-studio/error-code> |
| `invalid_request_error` / `invalid_value` | 400 | OpenAI 兼容协议下的请求非法 | 同上 |
| `Arrearage` | 400 | **账号欠费**：`Access denied, please make sure your account is in good standing.` | 同上 |
| `isv.OUT_OF_SERVICE` | 400 | 账号余额不足，服务被暂停 | 同上 |
| `DataInspectionFailed` / `data_inspection_failed` | 400 | **内容审核拦截**（输入或输出命中敏感内容，「Green Net」） | 同上 |
| `InvalidParameter.DataInspection` | 400 | 数据检查阶段媒体下载失败/超时 | 同上 |
| `Contain.Forbidden.Label.Error` | 400 | 账号带禁止下单标签 | 同上 |
| `Model not exist.` | 400 | 模型名错误/拼写不符 | 同上 |
| `InvalidApiKey` / `invalid_api_key` | 401 | API Key 非法（含地域不匹配） | <https://help.aliyun.com/zh/model-studio/text-rerank-api> |
| `ModelNotFound` / `model_not_found` | 404 | 模型不存在 | <https://help.aliyun.com/zh/model-studio/error-code> |
| `model_not_supported` | 404 | `Unsupported model xxx for OpenAI compatibility mode.` → 该模型不支持 OpenAI 兼容模式，改用 DashScope 原生 | 同上 |
| `AllocationQuota.FreeTierOnly` | 403 | 免费额度用尽（「免费额度用完即停」模式） | 同上 |
| `No backend server available` | 400 | 后端暂时不可用（服务更新/扩缩容）；先查是否欠费，再稍后重试 | 同上 |

**✅ 可重试错误码清单（已核实确切字符串）**：

| 错误码 | HTTP | 消息 | 依据 |
|---|---|---|---|
| `Throttling` | **429** | `Requests throttling triggered.` | <https://help.aliyun.com/zh/model-studio/error-code> |
| `Throttling.RateQuota`（别名 `LimitRequests` / `limit_requests` / `ResourceExhausted` / `Too many requests`） | **429** | `You have exceeded your request limit.` / `Requests rate limit exceeded, please try again later.` / `You exceeded your current requests list.` | 同上 |
| `Throttling.AllocationQuota`（别名 `insufficient_quota`） | **429** | `Allocated quota exceeded, please increase your quota limit.` / `You exceeded your current quota, please check your plan and billing details.` | 同上 |
| `Throttling.BurstRate`（`limit_burst_rate`） | **429** | `Request rate increased too quickly. To ensure system stability, please adjust your client logic to scale requests more smoothly over time.` | 同上 |
| `Throttling.Concurrency` | **429** | `Too many concurrent requests.` | 同上 |
| `Throttling.ServiceOverloaded` / `Throttling.ResourceExhausted` | 429 | 经 `InternalError.Algo: ... [Too many requests.]` 暴露 | 同上 |
| `RequestTimeOut` | **500** | `Request timed out, please try again later.` / `Response timeout!` | 同上 |
| `InternalError`（`internal_error`） | **500** | `An internal error has occured, please try again later or contact service support.` | 同上 |
| `InternalError.Algo` | **500** | `inference internal error.`（官方建议：**先尝试重试**，排除偶发） | 同上 |
| `InternalError.Timeout` / `InternalError.Configuration` / `InternalError.DataInspection` | 500 | 各类内部错误 | 同上 |
| `SystemError` / `ModelServiceFailed` | 500 | `An system error has occured, please try again later.` | 同上 |
| `ServiceUnavailable` | **500/503** | `Too many requests. Your requests are being throttled due to system capacity limits. Please try again later.` | 同上 |
| `ModelUnavailable` | **503** | `Model is unavailable, please try again later.` | 同上 |

> **重要核实结论**：`RequestTimeOut` 挂在 **HTTP 500**（不是 429），官方说明超时报错时间为 **300 秒**（非流式），
> 并建议**改用流式输出**规避。→ 本项目 `LLMCfg.timeout_s = 120` 比服务端 300s 更早触发，
> 属于**客户端主动超时**，可安全重试。
> （依据：<https://help.aliyun.com/zh/model-studio/error-code>）

**分级处置建议**（据 429 子码分流）：

| 子码 | 含义 | 处置 |
|---|---|---|
| `Throttling.BurstRate` | 短时突发（**总量未超也会触发**） | 匀速调度 / 退避；可试 `X-DashScope-Wait-Timeout` 头 |
| `Throttling.RateQuota` | RPM/RPS 超限 | 降频 + 退避 |
| `Throttling.AllocationQuota` | TPM/TPS 超限 | 缩减输入 / 限制输出长度 |
| `Throttling.Concurrency` | 并发过高 | 降并发（信号量） |

`400-InvalidParameter` 下与本项目直接相关的具体消息：

| 消息 | 原因 | 依据 |
|---|---|---|
| `Value error, batch size is invalid, it should not be larger than xxx.` | **嵌入输入条数超上限**（v4 为 10） | <https://www.alibabacloud.com/help/en/model-studio/error-code> |
| `Value error, contents is neither str nor list of str.: input.contents` | 嵌入 input 既不是 str 也不是 list[str] | 同上 |
| `Range of input length should be [1, xxx] (InternalError.Algo.InvalidParameter)` | 输入超过模型最大长度 | 同上 |
| `Required body invalid, please check the request body format.` | 请求体不是合法 JSON（尾逗号、括号/引号未闭合） | 同上 |
| `Either "prompt" or "messages" must exist and cannot both be none` | 原生协议下 `messages` 放错层级（必须放 `input` 内） | 同上 |

客户端侧（**无 HTTP 状态码**）：

| 异常 | 原因 | 依据 |
|---|---|---|
| `APIConnectionError` / `Connection error.` | 本地网络问题（通常是开了代理） | <https://www.alibabacloud.com/help/en/model-studio/error-code> |

### 5.6 官方退避/重试建议（**已核实，含具体参数**）

官方有**专门一页**《限流应对最佳实践》，明确推荐 **带随机抖动的指数退避**：

> 「不限制发送速率，仅在收到 **429 或 5xx** 时触发带随机抖动的指数退避重试。」
> 「**固定间隔重试会让所有失败请求同时重发，再次触发限流**。指数退避 + 随机抖动将重试打散。」
> 对 `Throttling.BurstRate`：「建议优化客户端调用逻辑，采用平滑请求策略
> （如**匀速调度、指数退避或请求队列缓冲**），将请求均匀分散在时间窗口内，避免瞬时高峰。」

**官方示例代码里的具体数值**：

| 实现 | 参数 |
|---|---|
| tenacity 版 | `wait_random_exponential(min=1, max=60)` + `stop_after_attempt(6)` |
| 手写版 | `max_retries = 5`、`base_delay = 1`、`max_delay = 60`、`backoff = min(max_delay, base_delay * 2**(attempt-1))`、`sleep_time = backoff + random.uniform(0, 1)` |

**官方指定要重试的异常**：`openai.RateLimitError`、`openai.InternalServerError`、`openai.APIConnectionError`。

**服务端排队（官方推荐的优先手段）**：请求头
`X-DashScope-Wait-Timeout: <秒>`（`0` = 不排队；建议 `3~120`）。
⚠️ **仅对「增速/突发」限流（`Throttling.BurstRate`）有效，不适用于 RPM/TPM 绝对配额超限**。
使用时客户端需相应延长超时（非流式：基础超时 + wait；流式：首 chunk 超时 > wait）。

其他官方建议：

> 「限流通常 **1 分钟内**自动恢复。」
> 「添加**备用模型**：触发限流后切换到备用模型继续生成……也可选择不同系列的模型（如 qwen-flash）作为备用，
> 进一步降低主备同时被限流的风险。」
> 「**Batch API 请求不受实时限流约束**（但要考虑排队与处理时间）。」

（依据：<https://help.aliyun.com/zh/model-studio/rate-limiting-best-practices>、
<https://help.aliyun.com/zh/model-studio/rate-limit>）

> 仍未核实到：是否返回 **`Retry-After`** 响应头（官方示例用自算退避，未提及该头）。

### 5.7 建议的重试判定伪码（据已核实错误码）

```
可重试:  HTTP 429 | HTTP 500 | HTTP 503 | APIConnectionError
         | code in {Throttling, Throttling.RateQuota, Throttling.AllocationQuota,
                    Throttling.BurstRate, Throttling.Concurrency,
                    Throttling.ServiceOverloaded, Throttling.ResourceExhausted,
                    RequestTimeOut, InternalError, InternalError.Algo,
                    InternalError.Timeout, SystemError, ModelServiceFailed,
                    ServiceUnavailable, ModelUnavailable}

不可重试: HTTP 400 | 401 | 403 | 404 | 413
         | code in {InvalidParameter*, invalid_request_error, invalid_value,
                    Arrearage, isv.OUT_OF_SERVICE, DataInspectionFailed,
                    data_inspection_failed, InvalidApiKey, invalid_api_key,
                    Model not exist, ModelNotFound, model_not_found,
                    model_not_supported, AllocationQuota.FreeTierOnly,
                    Contain.Forbidden.Label.Error}

特例:     No backend server available → 先提示检查欠费，再稍后重试
         Throttling.BurstRate → 可带 X-DashScope-Wait-Timeout 重试

退避:     base_delay=1s, max_delay=60s, 指数 + 抖动 uniform(0,1)
         嵌入 max_retries=3（对齐 EmbedCfg.max_retries）
         LLM   max_retries=2（对齐 LLMCfg.max_retries）
```

---

## 6. 最小可用调用清单（照抄即可）

| 用途 | 协议 | 方法与路径 | 关键参数 |
|---|---|---|---|
| 嵌入 | OpenAI 兼容 | `POST {base}/compatible-mode/v1/embeddings` | `model=text-embedding-v4`, `input=[≤10 条]`, `dimensions=1024`, `encoding_format="float"` |
| 重排 | **DashScope 原生** | `POST {base}/api/v1/services/rerank/text-rerank/text-rerank` | body `{model, input:{query,documents[]}, parameters:{top_n,return_documents}}` |
| 生成 | OpenAI 兼容 | `POST {base}/compatible-mode/v1/chat/completions` | `model=qwen-max`, `messages`, `temperature`, `top_p`, `max_tokens` |

统一出网主机名（本项目 `04 §6.5` 白名单只需一个）：`{WorkspaceId}.cn-beijing.maas.aliyuncs.com`
（或共享域名 `dashscope.aliyuncs.com`）。**两条路径都在同一主机下**，无需额外放通。

---

## 7. 与本项目现状的差异（需处理）

| # | 事项 | 现状 | 官方契约 | 处置建议 |
|---|---|---|---|---|
| **D1** | 嵌入批量条数 | `RerankCfg`… 实为 `EmbedCfg.batch_size = 20`（`ekbs/config.py`） | **单请求最多 10 条**（`text-embedding-v4`） | **必须改为 ≤10**，否则运行期报 400 `batch size is invalid` |
| **D2** | rerank 出网协议 | `deploy/requirements.txt:54` 注释称「经 OpenAI 兼容协议调用云端 LLM / 嵌入 / **rerank**」；`05` §1934 已正确写成 `/api/v1/services/rerank` | rerank **不在** OpenAI 兼容模式内（§3.1） | **修正注释**；rerank 用独立 HTTP 客户端，不要塞进 OpenAI SDK |
| **D3** | rerank 模型即将下线 | `retrieval.rerank.model = "gte-rerank-v2"` | **2026-05-30 下线**，推荐 `qwen3-rerank` | 建议直接切 `qwen3-rerank`（注意扁平协议 + 无 `output` 外壳 + 不支持 `return_documents`） |
| **D4** | rerank 阈值语义 | `min_score = 0.30` 作为跨请求拒答阈值 | 官方：`relevance_score` **仅请求内相对可比，不可跨请求比较** | 阈值需按实测分布校准；文档中不得表述为「绝对相关性」 |
| **D5** | rerank 条数假设 | `10` §174「返回条数不足即视为失败并走降级」 | 「是否可能少于 `top_n`」**未核实到** | 用「实际返回条数」处理，勿硬断言 `len == top_n` |
| **D6** | 向量归一化 | 未说明是否归一化 | 官方**未核实到** | 入库前自行 L2 归一化（幂等安全），FAISS 用 IP 或 L2 均可 |
| **D7** | token 校准 | `20` §63 用云端 `usage.total_tokens` 校准 tiktoken | ✅ **已核实可用**：OpenAI 兼容 `/embeddings` 返回 `usage.total_tokens`（官方示例 `23`）；SDK 为 `resp.usage['total_tokens']` | 可直接用于校准，无需再探 |
| **D13** | 稀疏向量静默失败 | — | OpenAI 兼容接口传 `output_type=sparse` 会**返回 HTTP 200 但 embedding 为空** | 客户端必须断言 `len(embedding) == dimensions`；本项目用稠密向量，**不要传 `output_type`** |
| **D8** | 嵌入维度参数名 | — | 原生=**单数** `dimension`；OpenAI 兼容=**复数** `dimensions` | 两个 provider 分支不要共用参数名常量 |
| **D9** | ~~qwen-max 单价缺失~~ | — | **已补齐**：北京 2.4/9.6 元每百万 in/out（§4.5） | 已可直接用于成本核算 |
| **D10** | 重试策略未落地 | `EmbedCfg.max_retries=3`、`LLMCfg.max_retries=2` 已配，但**未规定退避算法** | 官方《限流应对最佳实践》：指数退避 + 抖动，`base=1s`/`max=60s`，重试 429/500/503 | 按 §5.6 实现；对突发限流可选 `X-DashScope-Wait-Timeout` |
| **D11** | LLM 客户端超时 vs 服务端超时 | `LLMCfg.timeout_s = 120` | 服务端非流式超时为 **300s**（`RequestTimeOut` @500） | 120s 属客户端主动超时，**可安全重试**；若改流式可规避服务端 300s 限制 |
| **D12** | qwen-max 已被官方降级为「旧版模型」 | 配置 `model = "qwen-max"` | 归入「旧版模型 > 旧版Qwen」，标注「不再作为首选推荐」；但**无下线公告** | 不阻塞；建议排期评估 `qwen3-max` / `qwen3.8-max` |

> **注意**：`方案/` 与其它已有文件按任务要求**本次不改**，上表只作规格记录，待人工决策。

---

## 8. 「未核实到」清单（共 8 条）

> 以下条目**官方文档未说明**或**本次抓取未覆盖**，实施前需实测或补查。**请勿据推测编码。**

| # | 未核实到的事项 | 影响的实现点 |
|---|---|---|
| **U1** | `relevance_score` 是否为 **sigmoid 归一化**（官方仅声明范围 0.0~1.0） | 阈值语义解释（§3.4） |
| **U2** | `text-embedding-v4` 输出向量**是否需要自行归一化**（官方 Model Studio、Qwen 博客均无表述；GitHub/HF 本次不可达，故 Qwen3-Embedding README 亦未验证） | FAISS 索引类型与入库流程（§7-D6） |
| **U3** | 传入**非法 `dimensions` / `dimension` 值**时的错误码（官方只文档化了通用 400 `InvalidParameter`；**是快速失败还是静默兜底到默认 1024，未核实到**） | 配置校验的错误处理 |
| **U4** | rerank 是否可能**返回少于 `top_n` 条**；低于某分数是否会被丢弃 | 降级判定逻辑（§7-D5） |
| **U5** | `gte-rerank-v2` 的**新加坡**价格与限流；是否有**免费额度** | 新加坡部署成本核算（§3.7） |
| **U6** | 是否返回 **`Retry-After`** 响应头 | 退避策略实现（§5.6） |
| **U7** | **`gte-rerank-v2` 今天是否还能调通**：API 参考与模型页仍按「可用」写（完整示例/价格/限流表），而 rerank 指南已用过去时「was discontinued」→ **文档自相矛盾**，无任何文档明确说当前调用会失败 | 上线前**必须真实探活**；否则整条 rerank 链路会静默降级（§3.6、§7-D3） |
| **U8** | rerank **单请求 30,000 token 上限**被突破时的行为（官方「HTTP 400 + 不截断」只挂在**单条** 4,000 上限上） | 候选集规模控制（§3.5） |

**已由本次核实补齐（原为未核实到，现有官方依据）**：

| 事项 | 结论 | 依据 |
|---|---|---|
| OpenAI 兼容 `/embeddings` 的 `usage` | ✅ `usage.total_tokens` 确认存在。⚠️ `usage.prompt_tokens` **中英文档不一致**（中文页只给 `total_tokens`，英文页给 `prompt_tokens`+`total_tokens`）→ 只依赖 `total_tokens` | <https://help.aliyun.com/zh/model-studio/embedding-interfaces-compatible-with-openai>、<https://www.alibabacloud.com/help/en/model-studio/embedding-interfaces-compatible-with-openai> |
| 原生嵌入响应逐字段 | ✅ `output.embeddings[].embedding` / `.text_index` / `.sparse_embedding[]`、`usage.total_tokens`、`request_id`、`code`/`message`；失败响应**扁平** | <https://www.alibabacloud.com/help/en/model-studio/text-embedding-synchronous-api> |
| `output_type` / `text_type` 取值 | ✅ `output_type` ∈ `dense`\|`sparse`\|`dense&sparse`（默认 `dense`）；`text_type` ∈ `query`\|`document`（默认 `document`）；`instruct` 仅 v4 + query 生效 | 同上 |
| `qwen-max` 原生协议逐字段 | ✅ `output.choices[0].message.content`、`usage.input_tokens`/`output_tokens`/`total_tokens` | <https://help.aliyun.com/zh/model-studio/stream> |
| `qwen-max` 单价与上下文 | ✅ 北京 2.4/9.6 元每百万；上下文 32k（入 30720 / 出 8192）；**无阶梯计价** | <https://help.aliyun.com/zh/model-studio/qwen-max> |
| `Throttling` / `RequestTimeOut` 等确切错误码 | ✅ `Throttling`@429、`Throttling.RateQuota`@429、`Throttling.AllocationQuota`@429、`Throttling.BurstRate`@429、`RequestTimeOut`@**500** | <https://help.aliyun.com/zh/model-studio/error-code> |
| 最大重试次数 / 退避基数 | ✅ 官方示例：`max_retries=5`、`base_delay=1`、`max_delay=60`、`wait_random_exponential(1,60)` + `stop_after_attempt(6)` | <https://help.aliyun.com/zh/model-studio/rate-limiting-best-practices> |

**两处官方文档自相矛盾（非本次不确定，是文档本身冲突）**：

| 冲突点 | 中文页 | 英文页 | 建议 |
|---|---|---|---|
| `encoding_format` | 入参支持 `float`/`base64`，出参取决于网关 | 「Currently, only `float` is supported」 | **按只保证 `float` 设计** |
| OpenAI 兼容嵌入的 `usage` | 示例只有 `total_tokens` | 示例有 `prompt_tokens` + `total_tokens` | **只读 `total_tokens`** |

补充说明两条「部分核实」：

- `qwen-max` 是否走 OpenAI 兼容 Chat：**没有一句官方原文直接点名**，
  但有三条强证据（Function Calling 支持模型列表点名 `qwen-turbo/qwen-plus/qwen-max`、
  Batch 支持模型表在北京与新加坡都列 `qwen-max`、商业版 Qwen LLM 在兼容范围内）→ 判定**可用**（§4.1）。
- `qwen-max` 是否 legacy：归入「旧版模型」且标注「不再作为首选推荐」，但**无下线公告** → **在役但非主推**。
- 嵌入/重排是否**按次**额外计费：文档只列「文本输入（每百万 tokens）」一个计费项，未见按次计费
  → 但「是否存在未列出的按次费用」**未核实到**。

---

## 9. 依据来源汇总

| 主题 | URL |
|---|---|
| OpenAI 兼容（Chat）base_url / 模型列表 / 响应示例 | <https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope> |
| Base URL 总览（全地域 / 三类域名） | <https://help.aliyun.com/zh/model-studio/base-url> |
| 获取与配置 API Key | <https://help.aliyun.com/zh/model-studio/get-api-key> |
| 首次调用千问 API（实名认证 / WorkspaceId 前提） | <https://help.aliyun.com/zh/model-studio/first-api-call-to-qwen> |
| **限流应对最佳实践（退避参数 / Wait-Timeout 头）** | <https://help.aliyun.com/zh/model-studio/rate-limiting-best-practices> |
| qwen-max 模型页（上下文 / 价格 / 限流） | <https://help.aliyun.com/zh/model-studio/qwen-max> |
| 文本生成模型列表（旧版模型分节） | <https://help.aliyun.com/zh/model-studio/text-generation-model> |
| 模型价格总表（阶梯计价核对） | <https://help.aliyun.com/zh/model-studio/model-pricing> |
| 模型下线机制说明 | <https://help.aliyun.com/zh/model-studio/model-depreciation> |
| 新人免费额度规则 | <https://help.aliyun.com/zh/model-studio/new-free-quota> |
| 文本生成 API（DashScope 原生协议） | <https://help.aliyun.com/zh/model-studio/qwen-api-via-dashscope> |
| 文本生成 API（OpenAI 兼容协议） | <https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions> |
| 流式输出（原生 SSE / usage 位置） | <https://help.aliyun.com/zh/model-studio/stream> |
| OpenAI 兼容 Embedding 接口 | <https://help.aliyun.com/zh/model-studio/embedding-interfaces-compatible-with-openai> |
| 重排序指南（OpenAI 兼容小节 / 限制） | <https://help.aliyun.com/zh/model-studio/rerank> |
| qwen3-rerank 模型页（迁移对照价格） | <https://help.aliyun.com/zh/model-studio/qwen3-rerank> |
| GTE-RERANK 下线公告（国际站镜像） | <https://www.alibabacloud.com/en/notice/detail?id=1873> |
| 同步嵌入接口 API 详情（OpenAI 兼容 + 维度 + 单价） | <https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api> |
| 同步嵌入接口 API 详情（**英文版**，原生响应示例 / output_type） | <https://www.alibabacloud.com/help/en/model-studio/text-embedding-synchronous-api> |
| OpenAI 兼容 Embedding 接口（**英文版**，usage 示例） | <https://www.alibabacloud.com/help/en/model-studio/embedding-interfaces-compatible-with-openai> |
| 向量化（Embedding，原生 HTTP curl） | <https://help.aliyun.com/zh/model-studio/embedding> |
| text-embedding-v4 模型页（能力/单价/限流） | <https://help.aliyun.com/zh/model-studio/text-embedding-v4> |
| 向量与重排序总览（维度选型建议 / 模型表） | <https://help.aliyun.com/zh/model-studio/embedding-rerank-model> |
| 文本排序 API（rerank 端点/字段/分数语义/SDK） | <https://help.aliyun.com/zh/model-studio/text-rerank-api> |
| gte-rerank-v2 模型页（价格/限流） | <https://help.aliyun.com/zh/model-studio/gte-rerank-v2> |
| GTE-RERANK 下线公告 | <https://www.aliyun.com/notice/118217> |
| 限流（账号级/RPM/TPM/三种限流消息/退避建议） | <https://www.alibabacloud.com/help/en/model-studio/rate-limit> |
| 错误码（400/401/403/413/429 与各错误码含义） | <https://www.alibabacloud.com/help/en/model-studio/error-code> |
| 选择模型（官方主推模型现状） | <https://help.aliyun.com/zh/model-studio/models> |
| OpenAI Batch API（5 折 / 支持模型 / 端点） | <https://www.alibabacloud.com/help/en/model-studio/batch-interfaces-compatible-with-openai> |
| dashscope Python SDK（版本 1.20.14，签名与返回结构） | 本仓库 `.venv/lib/python3.11/site-packages/dashscope/`（`embeddings/text_embedding.py`、`rerank/text_rerank.py`、`aigc/generation.py`、`api_entities/dashscope_response.py`、`api_entities/api_request_factory.py`、`common/env.py`、`common/constants.py`） |
