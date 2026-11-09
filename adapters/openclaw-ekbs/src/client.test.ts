import assert from "node:assert/strict"
import { describe, it } from "node:test"
import { formatToolText, type EkbsAskResult } from "./client.js"

describe("formatToolText", () => {
  it("marks refused answers", () => {
    const r: EkbsAskResult = {
      answer: "未找到可靠依据。",
      refused: true,
      refusal_reason: "below_threshold",
      citations: [],
      suggested_questions: [],
      raw: {},
    }
    const text = formatToolText(r)
    assert.match(text, /^\[EKBS 拒答/)
    assert.match(text, /below_threshold/)
  })

  it("appends citations", () => {
    const r: EkbsAskResult = {
      answer: "年假 5 天。",
      refused: false,
      citations: [{ cite_index: 1, doc_title: "休假制度", page_from: 3 }],
      suggested_questions: [],
      raw: {},
    }
    const text = formatToolText(r)
    assert.match(text, /年假 5 天/)
    assert.match(text, /\[1\] 休假制度 p\.3/)
  })
})
