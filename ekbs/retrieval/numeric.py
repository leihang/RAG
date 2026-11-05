"""取数（数值问答）—— 把「数字准确」从**匹配问题**变成**类型问题**（课程 19 口径）。

## 为什么要单独一条路

现有的业务问答是**自由文本 + 事后核对**：允许模型写「约 6 万度」「同比减少 18%」，
然后拿规则去查这些数字有没有落在资料里。这条路对**换算、四舍五入、派生、张冠李戴**
四类都判不了（实测：`474.004万元` vs 单元格 `474.004` 这类换算是靠补丁修的，
而 `25.5万 + 18万` 这种派生值永远核不了）。

课程 19 的做法相反：**不解析数字，只在约束下抄数字** —— 输出空间被锁死成
`float | int | "N/A"`，模型没有机会算，宁可返回 N/A。两种路线不是谁替代谁：

* **业务问答（prose）**：制度/合同/汇报的解释性提问 —— 答案是一段话，必须保留；
* **取数（numeric）**：单指标取数（"电梯维保一年多少钱"）—— 答案是一个数 + 出处。

## 三条铁律（与课程一致，也是本模块的全部立场）

1. **不许算**：上下文没直接给出就 N/A，即使能由其它指标算出来也 N/A；
2. **不许猜**：指标等价性有任何疑问就 N/A（防"数字是真的但答非所问"）；
3. **抄完要验**：模型给的数必须是原文里的那个数字，不把「万 / 千 / 亿」换算成基本单位
   （见 :func:`verify_copied`）。展示用原文写法。

第 3 条是我们比课程**更强**的地方：课程的保证来自类型锁死，我们额外要求
"这个数确实是从这段原文抄的" —— 因此数值模式**不需要**再跑自由文本的忠实度校验
（那是"事后对账"，这里是"结构验证"，两者互斥但等价强度更高，不构成矛盾）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from ekbs.logging import get_logger

log = get_logger(component="retrieval.numeric")

#: 取数用的系统提示词（**与全局 SYSTEM_PROMPT 完全独立**）。
#:
#: 为什么必须独立：全局提示词是"业务问答"口径（鼓励解释、鼓励引用图片），
#: 而这里的铁律是"宁可 N/A 也不解释"。把铁律塞进全局提示词会污染业务问答
#: （"汇总 7 月能耗"不是单指标题，会被判成无法回答）。
#: 也正因独立，``PROMPT_REVISION`` **不需要**跟着变（那条规则针对的是全局提示词）。
NUMERIC_SYSTEM_PROMPT = (
    "你是财务/运营指标取数助手。**只从下面的资料里抄数字，不做任何计算**。\n"
    "只输出一个 JSON 对象，不要输出任何其它文字、解释或 Markdown 代码块。\n"
    '字段：{"value": 数字或 "N/A", "unit": "单位", "page": 页码或 null, '
    '"quote": "资料里包含该数字的原文片段（逐字复制）", "indicator": "你取的是哪个指标", '
    '"scope": "该数值的**口径限定词**（年份/月份/期间/表名/对象），如 \"2026年\"、"2024年"、'
    '\"第4页 用电分析表\"；必须是从资料里逐字复制的限定词；资料里确实没有限定词才留空", '
    '"reason": "若为 N/A，说明原因"}\n\n'
    "取值规则（必须遵守）：\n"
    "1. **禁止计算与推导**：资料未直接给出该指标时，即使能由其它数字算出，也返回 \"N/A\"。\n"
    "2. **禁止猜测指标**：上下文里所有可能指标都要看实际衡量内容；含义与问题**不完全一致**\n"
    "   （范围不同、只是相关概念、需要聚合）一律返回 \"N/A\"。有疑问也返回 \"N/A\"。\n"
    "3. 数字按资料原文抄，**禁止换算**。不要把「万」「千」「亿」折成基本单位\n"
    "   （原文是「1.2 万度」时 value 写 1.2，不要写成 12000；「万」留在原文里）。\n"
    "   千分位逗号写入 value 前可以去掉；括号负数的 value 用负号。quote 必须是逐字原文。\n"
    "4. 币种/口径与问题不符 → \"N/A\"。\n"
    "5. quote 必须是从资料里**逐字复制**的一段，且里面真的含有这个数字（原样形式）。\n"
    "6. **scope 是必填口径**：同一个指标往往有多行（2024年/2025年/2026年、不同表），\n"
    "   你必须把**你取的那一行的口径限定词**原样抄进 scope。资料里确实没有限定词才留空。\n"
    "7. 资料里没有 → \"N/A\"。\n\n"
    "示例一（正例）：\n"
    "问题：2026年电气保养合同的总价是多少？\n"
    "资料：[1] ……本工程合同总价￥19000元，大写：壹万玖仟元整……\n"
    '输出：{"value": 19000, "unit": "元", "page": 3, "quote": "本工程合同总价￥19000元", '
    '"indicator": "电气保养合同总价", "scope": "", "reason": ""}\n\n'
    "示例二（该 N/A 就 N/A）：\n"
    "问题：员工每人每年培训经费是多少？\n"
    "资料：[1] ……公司年度培训总预算 120000 元，覆盖全体员工……\n"
    '输出：{"value": "N/A", "unit": "", "page": null, "quote": "", "indicator": "人均培训经费", '
    '"scope": "", "reason": "资料只有总额、没有人数，需计算才能得到人均，按规则返回 N/A"}\n\n'
    "资料：\n<<CONTEXT>>"
)  # noqa: E501  （⚠️ 用 <<CONTEXT>> 而不是 {context}：本提示词里全是 JSON 花括号，
   #                走 str.format 会把这些花括号当占位符 → KeyError: '"value"'。）


def build_numeric_prompt(context: str) -> str:
    """把检索上下文填进取数提示词。

    刻意不用 ``str.format``：提示词里通篇是 JSON 示例（``{"value": ...}``），
    ``format`` 会把它们当占位符并抛 ``KeyError``（实测踩到）。``replace`` 没这个坑。
    """
    return NUMERIC_SYSTEM_PROMPT.replace("<<CONTEXT>>", context or "")

#: 取数模式下的用户名（进答案缓存键，避免与业务问答互相命中）
MODE_NUMERIC = "numeric"
MODE_PROSE = "prose"
MODE_AUTO = "auto"


def resolve_answer_mode(
    requested: str | None,
    intent: str,
    *,
    auto_numeric: bool = True,
) -> str:
    """把请求 mode + 意图解析成实际作答模式（``prose`` | ``numeric``）。

    * 显式 ``prose`` / ``numeric`` → 原样（管理台手动切换）；
    * ``auto``（默认）：``auto_numeric`` 开启且意图为 numeric → 取数，否则业务问答。
    """
    req = (requested or MODE_AUTO).strip().lower()
    if req in {MODE_PROSE, MODE_NUMERIC}:
        return req
    if auto_numeric and (intent or "").strip().lower() == MODE_NUMERIC:
        return MODE_NUMERIC
    return MODE_PROSE

_SURFACE_RE = re.compile(
    r"\(?-?\d[\d,]*(?:\.\d+)?\)?(?:\s*(?:万亿|十亿|千万|百万|千亿|亿|万|千))?"
)
_SURFACE_NUMBER = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?")
_NUMBER_TOKEN = re.compile(r"\(?-?\d[\d,\s]*(?:\.\d+)?\)?")


@dataclass
class NumericAnswer:
    """取数结果。``value is None`` 表示 N/A（拒答）。"""

    value: float | None = None
    unit: str = ""
    page: int | None = None
    quote: str = ""
    indicator: str = ""
    #: 口径限定词（年份/期间/表名…）——**必须能从原文逐字核到**，否则拒答：
    #: 编出来的口径比错的数字更危险（看着权威但答非所问）。
    scope: str = ""
    #: 同一行标签下的**其它口径候选**（如"2024年：25178448"）——发现歧义时披露给用户，
    #: 而不是假装只有一个答案（见 README §四 第 79 条）。
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    raw: str = ""
    #: 展示用的原文数字（如 ``439200.00``、``1.2 万``）。取数成功后由抄写校验填上。
    surface: str = ""
    #: 抄写校验命中的引用序号（1-based，对应进上下文的分块顺序）
    cite_index: int | None = None
    verified: bool = False
    #: 输出**不是合法 JSON/缺 value 字段**（= 格式失败，可重试一次）；
    #: 与"模型按规则返回 N/A"必须区分开 —— 后者重试毫无意义、还多烧一次调用。
    parse_error: bool = False

    @property
    def is_na(self) -> bool:
        return self.value is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "unit": self.unit,
            "page": self.page,
            "quote": self.quote,
            "indicator": self.indicator,
            "scope": self.scope,
            "alternatives": list(self.alternatives),
            "reason": self.reason,
            "verified": self.verified,
            "cite_index": self.cite_index,
            "surface": self.surface,
        }


def normalize_number(text: Any, unit: str = "") -> float | None:
    """把"资料/模型给出的数字字符串"归一成基本单位下的 float（不做任何内容推导）。

    覆盖千分位逗号、欧陆式逗号当小数点、括号负数。
    **不**按「万 / 千 / 亿」换算：单位里的量级词不改变数值。
    """
    if text is None:
        return None
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        value = float(text)
    else:
        raw = str(text).strip().replace("％", "%").replace("，", ",")
        if not raw or raw.upper() in {"N/A", "NA", "NIL", "无", "未知", "-", "--"}:
            return None
        negative = raw.startswith("(") and raw.endswith(")")
        raw = raw.strip("()").replace("%", "").replace(" ", "")
        raw = re.sub(r"[^\d,.\-]", "", raw)
        if not raw:
            return None
        if "." not in raw and raw.count(",") >= 1:
            tail = raw.split(",")[-1]
            # 一个逗号且后面 1~2 位 → 欧陆式小数；其余当千分位
            raw = raw.replace(",", "." if (raw.count(",") == 1 and 1 <= len(tail) <= 2) else "")
        else:
            raw = raw.replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            return None
        if negative:
            value = -value
    return value


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出第一个 JSON 对象（容忍 ```json 包裹与前后噪声）。"""
    raw = (text or "").strip()
    if not raw:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    if fence:
        raw = fence.group(1)
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def parse_numeric_payload(text: str) -> NumericAnswer:
    """解析模型输出 → :class:`NumericAnswer`（解析失败即 N/A，不猜）。"""
    answer = NumericAnswer(raw=(text or "").strip())
    payload = _extract_json_object(text)
    if payload is None:
        answer.reason = "输出不是合法 JSON"
        answer.parse_error = True
        return answer
    if "value" not in payload:
        answer.reason = "输出缺少 value 字段"
        answer.parse_error = True
        return answer
    unit = str(payload.get("unit") or "").strip()
    quote = str(payload.get("quote") or "").strip()
    indicator = str(payload.get("indicator") or "").strip()
    scope = str(payload.get("scope") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    page_raw = payload.get("page")
    try:
        page = int(page_raw) if page_raw is not None and str(page_raw).strip() != "" else None
    except (TypeError, ValueError):
        page = None
    value = normalize_number(payload.get("value"), unit)
    return NumericAnswer(
        value=value,
        unit=unit,
        page=page,
        quote=quote,
        indicator=indicator,
        scope=scope,
        reason=reason or ("未能从资料中直接取得该指标" if value is None else ""),
        raw=answer.raw,
    )


def _literal_surfaces(text: str) -> list[tuple[str, float]]:
    """原文中的数字写法。``1.2 万`` 的数值是 1.2，不乘一万。"""
    found: list[tuple[str, float]] = []
    for match in _SURFACE_RE.finditer(text or ""):
        surface = match.group(0).strip()
        number = _SURFACE_NUMBER.match(surface)
        if number is None:
            continue
        value = normalize_number(number.group(0))
        if value is None:
            continue
        found.append((surface, value))
    return found


def _normalize_for_match(text: str) -> str:
    """抄写校验的归一化：只抹掉排版分隔符，不改数字/文字。

    Excel 行级表格序列化成 ``单价（元）=3.456``；模型常写成
    ``单价（元） 3.456``（空格）或 ``单价（元）：3.456``（冒号）。
    这些是同一段原文的排版差异，不应判成“编造”。
    """
    # 模型有时会把表格 HTML 片段（</td><td>）抄进 quote；标签不是原文内容，
    # 先剥掉再比对，否则 DeepSeek 的取数结果会被误判成“编造”。
    cleaned = re.sub(r"<[^>]+>", "", str(text or ""))
    return re.sub(r"[\s=:：|]+", "", cleaned)


def _quote_matches_chunk(
    answer: NumericAnswer, chunks: Sequence[Any], *, quote_key: str, value: float, rel_tol: float,
) -> tuple[bool, int | None]:
    """第一轮：``quote`` 整段出现在某分块里，且该分块有等值数字。"""
    surfaces = _literal_surfaces(answer.quote)
    for index, chunk in enumerate(chunks, start=1):
        content = _normalize_for_match(str(getattr(chunk, "content", "") or ""))
        if not content or quote_key not in content:
            continue
        for surface, literal in surfaces:
            if abs(literal - value) <= max(rel_tol * max(abs(value), 1.0), 1e-9):
                answer.surface = surface
                return True, index
    return False, None


def _picked_number(raw_quote: str, value: float, rel_tol: float) -> Any:
    """取“数值所在的那个数字”匹配对象；取不到时退回最后一个数字。"""
    num_matches = list(_NUMBER_TOKEN.finditer(raw_quote))
    for match in reversed(num_matches):
        try:
            literal = float(match.group(0).replace(",", ""))
        except ValueError:
            continue
        if abs(literal - value) <= max(rel_tol * max(abs(value), 1.0), 1e-9):
            return match
    return num_matches[-1] if num_matches else None


def _line_label_candidates(answer: NumericAnswer, value: float, rel_tol: float) -> list[str]:
    """行标签候选：数值前的内容 + 首个空白 token（模型常把整行抄进来）。"""
    raw_quote = re.sub(r"\s+", "", answer.quote or "")
    picked = _picked_number(raw_quote, value, rel_tol)
    label = _normalize_for_match(raw_quote[: picked.start()]) if picked else ""
    label = re.sub(r"[:：=＝]+$", "", label)
    candidates = [label] if len(label) >= 2 else []
    first_token = re.split(r"\s+", (answer.quote or "").strip())[0] if (answer.quote or "").strip() else ""
    token_label = re.sub(r"[:：=＝]+$", "", _normalize_for_match(first_token))
    if len(token_label) >= 2 and token_label not in candidates:
        candidates.append(token_label)
    return candidates


def _match_by_line_label(
    candidates: list[str], chunks: Sequence[Any], value: float, rel_tol: float,
) -> tuple[bool, int | None, str]:
    """回退：标签和数值出现在**同一行**。"""
    for label_text in candidates:
        for index, chunk in enumerate(chunks, start=1):
            for line in str(getattr(chunk, "content", "") or "").splitlines():
                line_norm = _normalize_for_match(line)
                if label_text not in line_norm:
                    continue
                for surface, literal in _literal_surfaces(line):
                    if abs(literal - value) <= max(rel_tol * max(abs(value), 1.0), 1e-9):
                        return True, index, surface
    return False, None, ""


def verify_copied(
    answer: NumericAnswer,
    chunks: Sequence[Any],
    *,
    rel_tol: float = 1e-6,
) -> tuple[bool, int | None]:
    """抄写校验：模型给的数必须是引用片段里原样出现的数字，不做万/千换算。

    1. ``quote`` 去掉空白后必须出现在某个入选分块里；
    2. ``value`` 等于该片段里某个数字的字面值（``1.2 万`` 只等于 1.2，不等于 12000）。
    命中后把原文写法写进 ``answer.surface``，供展示，不再另做千分位或补零。
    """
    if answer.is_na:
        return False, None
    quote_key = _normalize_for_match(answer.quote or "")
    if not quote_key:
        return False, None
    value = float(answer.value)
    matched, index = _quote_matches_chunk(
        answer, chunks, quote_key=quote_key, value=value, rel_tol=rel_tol
    )
    if matched:
        return True, index
    # 回退：模型常把行压缩成 "锅炉181201207896 : 0"（省略了中间的“2026年9月用量”列名），
    # 只要求标签和数值出现在**同一行**，仍能防止把别的行/别的数字抄过来。
    candidates = _line_label_candidates(answer, value, rel_tol)
    matched, index, surface = _match_by_line_label(candidates, chunks, value, rel_tol)
    if matched:
        answer.surface = surface
        return True, index
    return False, None


def _format_value(value: float) -> str:
    """人读的数字串：整数带千分位、小数不用科学计数法。

    ``f"{value:g}"`` 会把 26087023 渲染成 ``2.6087e+07`` —— 取数结果的答案文本要给
    人看，也进审计，科学计数法既难读又容易被当成"没抄准"。结构化字段 ``numeric.value``
    仍然是精确数值，这里只影响显示。
    """
    if value == int(value) and abs(value) < 1e15:
        return f"{int(value):,}"
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def verify_scope(answer: NumericAnswer, chunks: Sequence[Any], cite_index: int | None) -> bool:
    """口径必须能从**它引用的那个分块**里核到（空口径视为通过）。

    模型给的口径常是多段拼起来的（``2026年9月 燃气分析 食堂7259882331``），
    在分块里可能分散在表名/表头/数据行，不能要求整串连续子串；
    按空白/逗号/分号拆开，每一段都必须在该分块里出现。
    """
    scope = str(answer.scope or "").strip()
    if not scope:
        return True
    if cite_index is None or cite_index < 1 or cite_index > len(chunks):
        return False
    target = chunks[cite_index - 1]
    content = _normalize_for_match(str(getattr(target, "content", "") or ""))
    extra = " ".join(
        str(getattr(target, attr, "") or "") for attr in ("doc_title", "heading_path")
    )
    extra = _normalize_for_match(extra)
    if not content and not extra:
        return False
    # 先把页码从整段口径里剥掉，再按空白拆段；否则 "第 8 页" 会被空格拆成
    # "（第"/"8"/"页）" 三个必然匹配不上的片段。
    scope_clean = re.sub(r"[（(]?\s*第\s*\d+\s*页\s*[）)]?", "", scope)
    parts = [p for p in re.split(r"[\s,，;；/]+", scope_clean) if p.strip()]
    if not parts:
        return True

    def _part_matches(part: str) -> bool:
        key = _normalize_for_match(part)
        if not key:
            return True
        return key in content or key in extra

    return all(_part_matches(part) for part in parts)

#: 行标签与数字之间的分隔符（`总用电/度: 26087023` 里的 `: `）
_LABEL_SEP = re.compile(r"[:：=＝\s\-—/]*$")


#: 问题里可能**自带**的"期间/位置"限定词：年份、年月、月、日、第N页、季度、Q1。
_QUALIFIER_RE = re.compile(
    r"\d{4}\s*年|\d{1,2}\s*月|\d{4}(?![\d年])|第\s*\d+\s*页|第\s*[一二三四1-4]\s*季度|[Qq][1-4]|\d{1,2}\s*日"
)


def question_pins_scope(question: str, scope: str) -> bool:
    """问题是否**自带**了与本次返回口径相符的限定词。

    相符 → 用户已经指明要哪一行，"另有 2024 年…"这类提示只是噪声，不再披露；
    不相符（或问题压根没提口径）→ 照旧披露，用户能看出"抄的"和"问的"可能不是同一行。

    只做"相符就收声"这一件事，**不做反向判断**：问题说 2026 而返回 2024 时，
    披露照旧（那种情形更需要用户看见），但不由规则去断言模型选错了 —— 那属语义判断。
    """
    quals = {re.sub(r"\s+", "", q) for q in _QUALIFIER_RE.findall(question or "")}
    if not quals:
        return False
    scope_norm = re.sub(r"\s+", "", scope or "")
    if not scope_norm:
        return False
    return any(q and q in scope_norm for q in quals)


def find_scope_alternatives(
    chunks: Sequence[Any],
    *,
    quote: str,
    value: float | None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """在入选分块里找**同一行标签下的其它数值**（不同口径），用于向用户披露歧义。

    为什么用"行标签"而不是语义：实测（README §四 第 79 条）同一个问题两次返回
    `26087023`（2026年）与 `25178448`（2024年），两者**都真实存在**、都能过抄写校验 ——
    规则校验证明不了"抄的是你要的那一行"。这里退一步做**结构性披露**：
    从已核验的 quote 里取出数字前的标签（如 `总用电/度`），再去其它分块里找同标签的
    不同数值，作为"另有 …"一起返回。**不猜、不拒答，把歧义摊开给用户看。**
    """
    if value is None or not quote:
        return []
    flat = re.sub(r"\s+", "", quote)
    numbers = _NUMBER_TOKEN.findall(flat)
    if not numbers:
        return []
    first = numbers[0]
    label = flat[: flat.find(first)]
    label = _LABEL_SEP.sub("", label)
    if len(label) < 2:  # 标签太短（如"值"）会把无关行也算进来
        return []
    label = label[-24:]  # 取尾部，避免把整段前缀当标签
    found: dict[str, dict[str, Any]] = {}
    for chunk in chunks:
        content = _normalize_for_match(str(getattr(chunk, "content", "") or ""))
        if not content:
            continue
        for match in re.finditer(re.escape(label) + r"[:：=＝]?([0-9][0-9,]*(?:\.\d+)?)", content):
            raw = match.group(1)
            candidate = normalize_number(raw)
            if candidate is None:
                continue
            if abs(candidate - float(value)) <= 1e-6 * max(abs(float(value)), 1.0):
                continue  # 与已选值相同（同一行在别处重复出现）→ 不是歧义
            key = f"{candidate:g}"
            found.setdefault(
                key,
                {
                    "value": candidate,
                    "raw": raw,
                    "label": label,
                    "chunk_id": int(getattr(chunk, "chunk_id", 0) or 0),
                    "doc_title": str(getattr(chunk, "doc_title", "") or ""),
                    "page": getattr(chunk, "page_from", None),
                },
            )
    return list(found.values())[:limit]


def render_answer(answer: NumericAnswer, cite_index: int | None) -> str:
    """渲染成**人读的一行**（审计与前端都按文本存/显示；结构化值另走 numeric 字段）。"""
    value = answer.value
    if value is None:
        return "未找到可靠依据。"
    text = answer.surface.strip() if answer.surface else _format_value(float(value))
    if answer.unit:
        text += f" {answer.unit}"
    subject = " ".join(x for x in (answer.scope, answer.indicator) if x)
    head = f"{subject}：" if subject else ""
    tail = f"（第 {answer.page} 页）" if answer.page else ""
    marker = f" [{cite_index}]" if cite_index else ""
    return f"{head}{text}{tail}{marker}"


def _clean_quote(text: str) -> str:
    """展示用 quote：模型可能把表格 HTML 标签抄进来，标签不是原文，展示前剥掉。"""
    return re.sub(r"<[^>]+>", "", text or "").strip()


def format_numeric_user_reply(answer: NumericAnswer, cite_index: int | None) -> str:
    """取数成功时的用户可见文案（含依据片段，供钉钉/API 直接展示）。"""
    lines = [render_answer(answer, cite_index)]
    quote = _clean_quote(answer.quote)
    if quote:
        lines.append(f"依据：{quote}")
    return "\n".join(lines)


def format_scope_clarify_reply(
    answer: NumericAnswer,
    alternatives: Sequence[dict[str, Any]],
) -> str:
    """多口径歧义时的反问文案（拒答，不放出任一数值当唯一答案）。"""
    lines = [
        "资料中同一指标存在多个口径，无法唯一确定。请补充年份、月份或表名后再问。",
        "",
        "例如：「2026年总用电是多少度」",
        "",
        "资料中可见的不同取值：",
    ]
    shown: list[str] = []
    if answer.value is not None:
        scope = (answer.scope or "").strip() or "未标注口径"
        unit = f" {answer.unit}" if answer.unit else ""
        shown.append(
            f"· {scope}："
            f"{answer.surface.strip() if answer.surface else _format_value(float(answer.value))}"
            f"{unit}"
        )
    for alt in alternatives:
        scope = str(alt.get("scope") or alt.get("label") or "未标注口径").strip()
        raw = alt.get("raw")
        val = alt.get("value")
        if raw is not None:
            num_text = str(raw)
        elif isinstance(val, (int, float)):
            num_text = _format_value(float(val))
        else:
            continue
        page = alt.get("page")
        page_s = f"（第 {page} 页）" if page else ""
        title = str(alt.get("doc_title") or "").strip()
        title_s = f" · {title}" if title else ""
        shown.append(f"· {scope}：{num_text}{page_s}{title_s}")
    lines.extend(shown[:6] if shown else ["· （未能列出候选行，请改用带年份的问法）"])
    return "\n".join(lines)


@dataclass
class NumericRun:
    """一次取数的完整留痕（进答案缓存与审计）。"""

    payload: NumericAnswer
    ok: bool = False
    reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
