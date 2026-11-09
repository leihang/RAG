/**
 * OpenClaw 插件：注册 ekbs_ask。
 * 有 SDK 时走 definePluginEntry；否则导出兼容 entry（本仓库可独立 build）。
 */
import { Type } from "typebox"
import { ekbsAsk, formatToolText, type EkbsClientConfig } from "./client.js"

export type PluginConfig = EkbsClientConfig

type ToolParams = { question: string; conversation_id?: string }

function buildTool(getConfig: () => PluginConfig) {
  return {
    name: "ekbs_ask",
    description:
      "查询企业知识库（EKBS）：制度、手册、合同条款、表格口径等。" +
      "若返回以 [EKBS 拒答 开头，表示无可靠依据，不要编造。",
    parameters: Type.Object({
      question: Type.String({ description: "用户问题" }),
      conversation_id: Type.Optional(Type.String({ description: "多轮会话 id（可选）" })),
    }),
    async execute(_id: string, params: ToolParams) {
      const result = await ekbsAsk(params.question, getConfig(), {
        conversationId: params.conversation_id,
      })
      return {
        content: [{ type: "text" as const, text: formatToolText(result) }],
        details: {
          refused: result.refused,
          refusal_reason: result.refusal_reason,
          trace_id: result.trace_id,
          citations: result.citations,
        },
      }
    },
  }
}

export function register(api: {
  registerTool: (tool: ReturnType<typeof buildTool>) => void
  pluginConfig?: PluginConfig
  config?: PluginConfig
}): void {
  api.registerTool(buildTool(() => (api.pluginConfig || api.config || {}) as PluginConfig))
}

const fallbackEntry = {
  id: "ekbs-knowledge",
  name: "EKBS 企业知识库",
  description: "企业知识库查询（权限与拒答由 EKBS 执行）",
  register,
}

async function createEntry(): Promise<unknown> {
  try {
    const { definePluginEntry } = await import("openclaw/plugin-sdk/plugin-entry")
    return definePluginEntry({
      id: "ekbs-knowledge",
      name: "EKBS 企业知识库",
      description: "企业知识库查询（权限与拒答由 EKBS 执行）",
      register: (api: unknown) => register(api as Parameters<typeof register>[0]),
    })
  } catch {
    return fallbackEntry
  }
}

export default await createEntry()
export { ekbsAsk, formatToolText, buildTool, fallbackEntry }
