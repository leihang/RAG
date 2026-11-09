# openclaw-ekbs

OpenClaw 工具插件：把 **EKBS 企业知识库**挂成 `ekbs_ask`。

编排层（OpenClaw）只负责调用；权限、精排、拒答、审计仍在 EKBS。

## 1. 先不装 OpenClaw：HTTP 冒烟

```bash
# 仓库根；API 已起（cloud/dev 均可）
set -a && source ~/.ekbs/secrets.env && set +a
export EKBS_API_BASE=http://127.0.0.1:8000/api/v1
export EKBS_TOOL_USER_ID=364   # 换成你的真实 user_id

cd adapters/openclaw-ekbs
npm install
npx tsx scripts/smoke.mjs '工程手册的主要内容'
```

## 2. 装进 OpenClaw

```bash
cd adapters/openclaw-ekbs
npm install
npm run build

# 在 OpenClaw 环境（需已安装 openclaw CLI）
openclaw plugins install .
# 或开发期：openclaw plugins install /绝对路径/adapters/openclaw-ekbs
```

插件配置（Gateway / 插件设置）示例：

```json5
{
  baseUrl: "http://127.0.0.1:8000/api/v1",
  internalToken: "与 EKBS_INTERNAL_TOKEN 相同",
  userId: 364,
  timeoutMs: 120000,
  channel: "openclaw"
}
```

启用后 Agent 应能看到工具 **`ekbs_ask`**。问制度类问题时应调用它；若返回 `[EKBS 拒答 …]`，模型不得编造。

## 3. 身份红线

`userId` 决定可见分片。联调可先写死；上线应按 OpenClaw 登录用户映射到 EKBS `user_id`，禁止全员共用超级管理员。
