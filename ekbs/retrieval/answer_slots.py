"""答案模版槽位：LLM 先出带 ``[[槽]]`` 的正文模版 → 计算模块回填 → 出口闸门。

用于方案/预算类「文中已写明」的问句（Word/xlsx/ppt 统一）：
* 正文描述由模版写出；
* 凡数字只用槽位，禁止模型在模版里写死数；
* 取数失败填 ``—``，不编造；
* 闸门拒绝答案里出现核定结果以外的金额/用量数字。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from ekbs.logging import get_logger
from ekbs.retrieval.structured_query import StructuredQueryHit

log = get_logger(component="retrieval.answer_slots")

SLOT_RE = re.compile(r"\[\[([^\]]+?)\]\]")
#: 答案里「像核定数字」的 token（千分位、小数、可选约/%）
_ANSWER_NUMBER_RE = re.compile(
    r"(?<!\[)(?<!\[\[)(?P<num>[约大约]?\s*[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|"
    r"[约大约]?\s*[+-]?\d+(?:\.\d+)?)(?!\])"
)

TEMPLATE_SYSTEM = (
    "你是企业知识库答题模版器。根据用户问题与检索片段，输出**一个** JSON 对象，"
    "不要其它文字或 Markdown 代码块。\n"
    "字段：\n"
    '{"template":"中文答案模版，数字位置用 [[槽id]]",'
    '"slots":[{"id":"槽id","op":"extract","columns":["列名"]}],'
    '"reason":"一句话"}\n'
    "规则：\n"
    "1. 若问题要改造方案/措施/概述：template **必须先写清楚正文描述**"
    "（风柜/主机/冷却塔等要点），再写成本与节约；禁止只列数字。\n"
    "2. **凡阿拉伯数字一律写成 [[槽id]]**（含预算、节约电费、回收期、工期、"
    "电价、百分比、温度设定），禁止写死 22000、0.773、10%、30 天等。\n"
    "3. 槽 id/columns 优先用片段「相关表头」或 `键=` 中出现的列名"
    "（改造预算、预计年节约电费、投资回收期、计划工期等）。"
    "电价、风柜/主机/冷却塔节约比例若只在叙述中出现也可建槽，"
    "但 columns 不要用宽表实体列名（如不要把「冷却塔」当成节约比例列）。\n"
    "4. slots 与 template 中的 [[槽]] 一一对应；op 固定 extract；columns 通常等于 [id]。\n"
    "5. 问题不涉及数字时可 slots=[]，template 纯描述。\n"
    "6. 不要输出引用编号 [1]；引用由系统后置追加。\n"
    "7. **排版（管理台与钉钉 sampleMarkdown 通用）**：template 内用换行；"
    "小节标题写成单独一行的【概述】/【改造方案】/【改造成本】/【节能效益】；"
    "分项措施用 `- **风柜**：…` 列表；关键数字可用 `**[[槽]]**`；"
    "禁止管道表、代码围栏、HTML、把全文挤成一段。\n"
)


@dataclass
class AnswerSlot:
    """单个取数槽。"""

    id: str
    op: str = "extract"
    columns: list[str] = field(default_factory=list)


@dataclass
class AnswerTemplate:
    """LLM 产出的答案模版。"""

    template: str
    slots: list[AnswerSlot] = field(default_factory=list)
    reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def build_template_messages(
    question: str, snippets: Sequence[str], *, max_snippets: int = 8
) -> list[dict[str, str]]:
    blocks: list[str] = [f"问题：{question.strip()}", "", "检索片段："]
    for index, snippet in enumerate(snippets[:max_snippets], start=1):
        text = re.sub(r"\s+", " ", (snippet or "").strip())[:500]
        if text:
            blocks.append(f"[{index}] {text}")
    if len(blocks) == 3:
        blocks.append("（无片段）")
    return [
        {"role": "system", "content": TEMPLATE_SYSTEM},
        {"role": "user", "content": "\n".join(blocks)},
    ]


def parse_answer_template(text: str) -> AnswerTemplate | None:
    """解析模版 JSON；非法则 None。"""
    payload = _extract_json_object(text)
    if not payload:
        return None
    template = str(payload.get("template") or "").strip()
    if not template:
        return None
    slots: list[AnswerSlot] = []
    raw_slots = payload.get("slots")
    if isinstance(raw_slots, list):
        for item in raw_slots:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("id") or "").strip()
            if not sid:
                continue
            cols = item.get("columns")
            columns: list[str] = []
            if isinstance(cols, list):
                columns = [str(c).strip() for c in cols if str(c).strip()][:6]
            elif isinstance(cols, str) and cols.strip():
                columns = [cols.strip()]
            if not columns:
                columns = [sid]
            slots.append(
                AnswerSlot(
                    id=sid[:40],
                    op=str(item.get("op") or "extract").strip().lower() or "extract",
                    columns=columns,
                )
            )
    # 模版里有 [[槽]] 但 slots 漏了 → 从模版补
    for sid in SLOT_RE.findall(template):
        name = sid.strip()
        if name and name not in {s.id for s in slots}:
            slots.append(AnswerSlot(id=name[:40], columns=[name[:40]]))
    return AnswerTemplate(
        template=template,
        slots=slots,
        reason=str(payload.get("reason") or "").strip()[:200],
        raw=payload,
    )


def resolve_slot_values(
    slots: Sequence[AnswerSlot],
    chunks: Sequence[Any],
    *,
    prefer_doc_ids: Sequence[int] | None,
) -> dict[str, str]:
    """按槽执行 extract；表抽不到时再扫叙述（电价/节约比例）；仍无则 ``—``。"""
    from ekbs.retrieval.table_extract import extract_from_table_chunks

    values: dict[str, str] = {}
    for slot in slots:
        if slot.op not in {"extract", "lookup", ""}:
            values[slot.id] = "—"
            continue
        hit = extract_from_table_chunks(
            chunks,
            columns=list(slot.columns) or [slot.id],
            prefer_doc_ids=prefer_doc_ids,
        )
        formatted = _format_slot_value(slot, hit)
        if formatted == "—":
            prose = _extract_prose_slot_value(
                slot, chunks, prefer_doc_ids=prefer_doc_ids
            )
            if prose:
                formatted = prose
        values[slot.id] = formatted
    return values


def _extract_prose_slot_value(  # noqa: C901 — 多标签正则分支同路径
    slot: AnswerSlot,
    chunks: Sequence[Any],
    *,
    prefer_doc_ids: Sequence[int] | None,
) -> str:
    """从表外叙述补电价、分项节约比例（``节约能耗 10%`` / ``电价 0.773``）。"""
    prefer = {int(d) for d in (prefer_doc_ids or ())}
    labels = " ".join([slot.id, *(slot.columns or ())])
    parts: list[str] = []
    for chunk in chunks:
        doc_id = getattr(chunk, "doc_id", None)
        if prefer and doc_id is not None and int(doc_id) not in prefer:
            continue
        text = str(getattr(chunk, "content", "") or "")
        if text.strip():
            parts.append(text)
    blob = "\n".join(parts)
    if not blob:
        return ""

    if "电价" in labels:
        match = re.search(
            r"电价\s*([0-9]+(?:\.[0-9]+)?)\s*元", blob
        )
        if match:
            return match.group(1)

    if any(token in labels for token in ("余额", "尚余", "剩余预算", "未使用")):
        match = re.search(r"尚余\s*([0-9]{1,3}(?:,\d{3})*|[0-9]+)\s*元", blob)
        if match:
            return match.group(1).replace(",", "")

    if not any(token in labels for token in ("比例", "百分比", "%")):
        return ""

    patterns: list[str] = []
    if "风柜" in labels or "公共风柜" in labels:
        patterns.extend(
            [
                r"节约能耗\s*([0-9]+(?:\.[0-9]+)?)\s*%",
                r"风柜\s*([0-9]+(?:\.[0-9]+)?)\s*%",
            ]
        )
    if "主机" in labels or "空调主机" in labels:
        patterns.extend(
            [
                r"主机能耗下降\s*([0-9]+(?:\.[0-9]+)?)\s*%",
                r"主机\s*([0-9]+(?:\.[0-9]+)?)\s*%",
            ]
        )
    if "冷却塔" in labels:
        patterns.extend(
            [
                r"冷却塔能耗降低\s*([0-9]+(?:\.[0-9]+)?)\s*%",
                r"冷却塔\s*([0-9]+(?:\.[0-9]+)?)\s*%",
            ]
        )
    if not patterns:
        patterns.append(
            r"(?:节约|下降|降低)[^。\n%]{0,12}?([0-9]+(?:\.[0-9]+)?)\s*%"
        )
    for pattern in patterns:
        match = re.search(pattern, blob)
        if match:
            return f"{match.group(1)}%"
    return ""


def fill_answer_template(template: str, slot_values: dict[str, str]) -> str:
    """把 ``[[槽]]`` 换成取值；未知槽填 ``—``。"""

    def _repl(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        return str(slot_values.get(key, "—"))

    return SLOT_RE.sub(_repl, template or "")


_ALREADY_LAYOUT_RE = re.compile(r"(?m)^(#{1,6}\s+|【[^】]+】\s*$|- \*\*|- )")
_PLAN_CUE_RE = re.compile(r"改造方案(?:包括|如下)?[：:]")
_COST_CUE_RE = re.compile(r"改造成本(?:方面)?[：:，,]?")
_BENEFIT_CUE_RE = re.compile(r"(?:节能效益(?:方面)?[：:，,]?|依据\s*20\d{2})")
_TRAILING_CITE_RE = re.compile(r"((?:\[\d+\])+)\s*$")


def layout_slotted_answer(answer: str) -> str:
    """把一坨槽位回填稿收成钉钉/管理台可读小节。

    已有 ``【节】`` / 标题 / 列表则不动；只做换行与列表，不改动任何数字。
    """
    text = str(answer or "").strip()
    if not text:
        return text
    if _ALREADY_LAYOUT_RE.search(text) or text.count("\n\n") >= 2:
        return text
    if not _PLAN_CUE_RE.search(text):
        # 非方案结构：至少把句号后的长段拆成软段落，避免钉钉粘成一块
        return _soft_paragraphs(text)

    cite = ""
    cite_match = _TRAILING_CITE_RE.search(text)
    if cite_match:
        cite = cite_match.group(1)
        text = text[: cite_match.start()].rstrip()

    plan_m = _PLAN_CUE_RE.search(text)
    assert plan_m is not None
    cost_m = _COST_CUE_RE.search(text)
    benefit_m = _BENEFIT_CUE_RE.search(text)

    blocks: list[str] = []
    overview = text[: plan_m.start()].strip(" \n。；;")
    if overview:
        blocks.append(f"【概述】\n{overview}")

    plan_end = (
        cost_m.start()
        if cost_m
        else (benefit_m.start() if benefit_m else len(text))
    )
    plan_body = text[plan_m.end() : plan_end].strip(" \n。；;")
    if plan_body:
        blocks.append(f"【改造方案】\n{_scheme_bullets(plan_body)}")

    if cost_m:
        cost_end = (
            benefit_m.start()
            if benefit_m and benefit_m.start() > cost_m.start()
            else len(text)
        )
        cost_body = text[cost_m.end() : cost_end].strip(" \n。；;")
        if cost_body:
            blocks.append(f"【改造成本】\n{_sentence_lines(cost_body)}")

    if benefit_m and (not cost_m or benefit_m.start() >= cost_m.end()):
        start = benefit_m.start()
        cue = benefit_m.group(0)
        if cue.startswith("节能"):
            start = benefit_m.end()
        benefit_body = text[start:].strip(" \n。；;")
        if benefit_body:
            blocks.append(f"【节能效益】\n{_sentence_lines(benefit_body)}")

    if not blocks:
        return str(answer or "").strip()
    out = "\n\n".join(blocks)
    if cite:
        out = f"{out}{cite}"
    return out


_SCHEME_LABEL_RE = re.compile(
    r"^(?:[一二三四五六]、|（[一二三四五六]）|[1-9]\.|对)"
    r"(?:中央空调|公区|公共)?"
    r"(风柜|主机|冷却塔|空调主机)"
)


def _scheme_bullets(body: str) -> str:
    """把「对风柜…；对主机…；对冷却塔…」收成列表。"""
    pieces = [
        p.strip(" \n；;。")
        for p in re.split(r"[；;]|(?<=[。])(?=对)", body)
        if p.strip(" \n；;。")
    ]
    if len(pieces) < 2:
        return body
    lines: list[str] = []
    for piece in pieces:
        label = _SCHEME_LABEL_RE.match(piece)
        if label:
            name = label.group(1).strip()
            if name == "空调主机":
                name = "主机"
            lines.append(f"- **{name}**：{piece}")
        else:
            lines.append(f"- {piece}")
    return "\n".join(lines)


def _sentence_lines(body: str) -> str:
    parts = [p.strip() for p in re.split(r"(?<=[。；;])\s*", body) if p.strip()]
    if len(parts) <= 1:
        return body
    return "\n".join(parts)


def _soft_paragraphs(text: str) -> str:
    """无方案结构时：按句号拆段，方便 pre-wrap / 钉钉换行。"""
    if len(text) < 160 or "\n" in text:
        return text
    parts = [p.strip() for p in re.split(r"(?<=[。])\s*", text) if p.strip()]
    if len(parts) < 3:
        return text
    return "\n\n".join(parts)


def merge_slot_hits(
    slots: Sequence[AnswerSlot],
    chunks: Sequence[Any],
    *,
    prefer_doc_ids: Sequence[int] | None,
) -> StructuredQueryHit:
    """汇总各槽 extract 结果，供 provenance / numeric 字段。"""
    from ekbs.retrieval.table_extract import extract_from_table_chunks

    detail: list[dict[str, Any]] = []
    chunk_ids: list[int] = []
    doc_ids: list[int] = []
    found = 0
    for slot in slots:
        hit = extract_from_table_chunks(
            chunks,
            columns=list(slot.columns) or [slot.id],
            prefer_doc_ids=prefer_doc_ids,
        )
        if hit is not None and not hit.is_na and hit.detail:
            found += 1
            for row in hit.detail:
                detail.append({slot.id: row.get(slot.id, next(iter(row.values()), "—"))})
            chunk_ids.extend(hit.chunk_ids or ())
            doc_ids.extend(hit.doc_ids or ())
            continue
        prose = _extract_prose_slot_value(
            slot, chunks, prefer_doc_ids=prefer_doc_ids
        )
        if prose:
            found += 1
            detail.append({slot.id: prose})
            # 叙述命中：挂上 prefer 文档内首个有正文的块作溯源
            for chunk in chunks:
                doc_id = getattr(chunk, "doc_id", None)
                if (
                    prefer_doc_ids
                    and doc_id is not None
                    and int(doc_id) not in {int(d) for d in prefer_doc_ids}
                ):
                    continue
                if str(getattr(chunk, "content", "") or "").strip():
                    cid = getattr(chunk, "chunk_id", None)
                    if cid is not None:
                        chunk_ids.append(int(cid))
                    if doc_id is not None:
                        doc_ids.append(int(doc_id))
                    break
        else:
            detail.append({slot.id: "—"})
    if not slots:
        return StructuredQueryHit(
            value=None, op="extract", reason="no_slots", surface="", detail=[]
        )
    if found == 0:
        return StructuredQueryHit(
            value=None,
            op="extract",
            reason="not_found",
            surface="N/A",
            detail=detail,
            extras={"source": "answer_slots"},
        )
    surface = "；".join(
        f"{k}={v}" for row in detail for k, v in row.items() if v != "—"
    )
    return StructuredQueryHit(
        value=found,
        surface=surface or "—",
        op="extract",
        rows_touched=found,
        chunk_ids=tuple(dict.fromkeys(chunk_ids)),
        doc_ids=tuple(sorted(set(int(d) for d in doc_ids))),
        detail=detail,
        extras={"source": "answer_slots", "slot_ids": [s.id for s in slots]},
    )


def context_allowed_numbers(snippets: Sequence[str]) -> set[str]:
    """检索原文中出现过的数字（归一化），可作为闸门白名单。"""
    allowed: set[str] = set()
    for snippet in snippets:
        for match in _ANSWER_NUMBER_RE.finditer(str(snippet or "")):
            token = _normalize_number_token(match.group("num"))
            if token and token != "—":
                allowed.add(token)
        # 百分比：10%、1%
        for match in re.finditer(r"(\d+(?:\.\d+)?)\s*%", str(snippet or "")):
            allowed.add(match.group(1))
            allowed.add(f"{match.group(1)}%")
    return allowed


def verify_slotted_answer(
    answer: str,
    slot_values: dict[str, str],
    *,
    template: str = "",  # noqa: ARG001
    context_snippets: Sequence[str] = (),
) -> bool:
    """出口闸门：无残留槽；数字须来自槽值或检索原文（禁止凭空编造）。"""
    text = answer or ""
    if SLOT_RE.search(text):
        return False
    allowed = {_normalize_number_token(v) for v in slot_values.values()}
    allowed.discard("")
    allowed.discard("—")
    allowed |= context_allowed_numbers(context_snippets)
    for match in _ANSWER_NUMBER_RE.finditer(text):
        token = _normalize_number_token(match.group("num"))
        if not token or token == "—":
            continue
        # 纯年份（20xx）：常见于「2026年方案」叙述，放行
        if re.fullmatch(r"20\d{2}", token):
            continue
        if token in allowed:
            continue
        return False
    # 百分比单独扫一遍
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*%", text):
        token = match.group(1)
        if token not in allowed and f"{token}%" not in allowed:
            return False
    return True


def scrub_unverified_numbers(
    answer: str,
    slot_values: dict[str, str],
    *,
    context_snippets: Sequence[str] = (),
) -> str:
    """把未核定数字替换为 ``—``，保留正文结构（闸门失败时的保底）。"""
    allowed = {_normalize_number_token(v) for v in slot_values.values()}
    allowed.discard("")
    allowed.discard("—")
    allowed |= context_allowed_numbers(context_snippets)

    def _repl_pct(match: re.Match[str]) -> str:
        token = match.group(1)
        if token in allowed or f"{token}%" in allowed:
            return match.group(0)
        return "—"

    def _repl_num(match: re.Match[str]) -> str:
        raw = match.group("num")
        token = _normalize_number_token(raw)
        if not token or token == "—":
            return match.group(0)
        if re.fullmatch(r"20\d{2}", token):
            return match.group(0)
        if token in allowed:
            return match.group(0)
        return "—"

    text = re.sub(r"(\d+(?:\.\d+)?)\s*%", _repl_pct, answer or "")
    return _ANSWER_NUMBER_RE.sub(_repl_num, text)


def fallback_slotted_table(slot_values: dict[str, str], *, missing_note: bool = True) -> str:
    """无正文模版时的表格式安全稿。"""
    lines = [f"{k}={v}" for k, v in slot_values.items()]
    body = "\n".join(lines) if lines else "未提取到可核定数字。"
    if missing_note and any(v == "—" for v in slot_values.values()):
        body += "\n部分数字未在资料中找到，已标为 —。"
    return body


def _format_slot_value(slot: AnswerSlot, hit: StructuredQueryHit) -> str:
    if hit is None or hit.is_na:
        return "—"
    for row in hit.detail or []:
        for col in slot.columns or [slot.id]:
            if col in row and row[col] not in (None, "", "—"):
                return _display_number(row[col])
        for key, value in row.items():
            if key == slot.id and value not in (None, "", "—"):
                return _display_number(value)
        for value in row.values():
            if value not in (None, "", "—"):
                return _display_number(value)
    surface = str(hit.surface or "").strip()
    if surface and surface != "N/A":
        # surface 可能是「改造预算=22000；…」→ 取本槽段
        for part in surface.split("；"):
            if "=" in part and part.split("=", 1)[0].strip() == slot.id:
                return part.split("=", 1)[1].strip() or "—"
        return surface
    return "—"


def _display_number(value: Any) -> str:
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return str(value)
    text = str(value).strip()
    return text or "—"


def _normalize_number_token(raw: str) -> str:
    text = str(raw or "").strip()
    text = re.sub(r"^(约|大约|近|逾)\s*", "", text)
    text = text.replace(",", "").replace("，", "")
    text = re.sub(r"(元|万元|度|吨|个月|月|天|日|%)$", "", text)
    return text.strip()


def _extract_json_object(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1)
    else:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return None
        raw = raw[start : end + 1]
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
