#!/usr/bin/env node
/**
 * 不经过 OpenClaw，直接打 EKBS /qa/chat（联调第一步）。
 *
 *   set -a && source ~/.ekbs/secrets.env && set +a
 *   export EKBS_TOOL_USER_ID=364
 *   npm run smoke -- 年假有多少天
 */
import { ekbsAsk, formatToolText } from "../dist/client.js"

const question = process.argv.slice(2).join(" ") || "年假有多少天"
const result = await ekbsAsk(question)
console.log(formatToolText(result))
if (result.trace_id) console.error(`trace_id=${result.trace_id} refused=${result.refused}`)
process.exit(result.refused ? 2 : 0)
