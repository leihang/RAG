"""发现层 / 冷启动 UX（D22）—— 可见文档导览 + 示例问。

硬约束：清单与示例问一律按当前用户 ACL/密级过滤，不得绕过 D10/D18。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ekbs.logging import get_logger
from ekbs.permission.acl import DocumentFacts
from ekbs.permission.context import PermContext

log = get_logger(component="discovery")

#: 钉钉欢迎/帮助触发词（整句匹配，去空白后小写）
HELP_TRIGGERS = frozenset(
    {
        "帮助",
        "help",
        "菜单",
        "能问什么",
        "有什么",
        "有什么可以问",
        "可以问什么",
        "怎么用",
        "使用说明",
        "你好",
        "hi",
        "hello",
        "开始",
        "？",
        "?",
    }
)

#: 纯问候（与「帮助」区分）：只回一句介绍，不展开帮助/文档。
_GREETING_PREFIXES = ("你好", "您好", "hi", "hello", "在吗")
_GREETING_REST_OK = frozenset(
    {"", "呀", "啊", "哈", "您好", "早", "早上好", "~", "～", "！", "!"}
)

#: 「文档/文档清单」短口令：只回可查标题清单（P0：帮助与清单分开）
_DOC_LIST_COMMANDS = frozenset(
    {
        "文档",
        "可查文档",
        "文档列表",
        "文档目录",
        "有哪些文档",
        "能查哪些文档",
        "查文档",
        "看看文档",
        "看文档",
        "文件清单",
    }
)

#: 短问候/帮助前缀：允许「你好啊」「帮助一下」等语气词，但仍限制总长，避免吞掉真实提问
_HELP_PREFIXES = ("你好", "hello", "hi", "帮助", "help", "菜单")
_HELP_PREFIX_MAX_LEN = 12
_HELP_PREFIX_REST_OK = frozenset(
    {"", "一下", "帮忙", "看看", "哈", "呀", "啊", "呢", "哦", "啦", "呗", "喔", "么", "嘛"}
)

_DEFAULT_SEEDS: list[dict[str, Any]] = [
    {
        "question": "中央空调循环水处理合同的工程总价是多少钱",
        "title_keywords": ["中央空调", "循环水"],
    },
    {
        "question": "电梯维护保养每台每月多少钱",
        "title_keywords": ["电梯"],
    },
    {
        "question": "空调保养合同的总价是多少",
        "title_keywords": ["空调保养", "空调"],
    },
    {
        "question": "设备编号是怎么组成的",
        "title_keywords": ["设备"],
    },
    {
        "question": "交接班管理制度的目的",
        "title_keywords": ["交接班"],
    },
    {
        "question": "2026年7月的用电分析表",
        "title_keywords": ["用电", "能源", "分析"],
    },
]


_CATALOG_PHRASES = (
    "哪些文档",
    "哪些文件",
    "哪些资料",
    "什么文档",
    "能查哪些",
    "能看哪些",
    "可以查哪些",
    "可以看哪些",
    "有权限的文档",
    "有权限的文件",
    "可见的文档",
    "可见文档",
    "文档列表",
    "我能查什么",
    "我可以查什么",
)

# 「列出我可以查询/查阅的文件/文档清单」或「目录清单」。
# 必须同时有列出、查询能力、清单，避免「把某某文件发给我」「年假制度是什么」误入。
_ACCESSIBLE_DOC_LIST_RE = re.compile(
    r"(?:列出|列一下|列下|列个)"
    r"\s*.{0,8}?"
    r"(?:我)?"
    r"\s*(?:可以|能|可)"
    r"\s*(?:查询|查阅|查)"
    r"\s*的?"
    r"\s*(?:文件|文档|资料|目录)"
    r"\s*的?"
    r"\s*(?:目录)?清单"
)

_FILE_MARKERS = ("文件", "原文", "原件", "原文档", "原始文件", "源文件", "附件")
#: 「要我发文件」的动词。不能只认「发给我」连写 —— 实测用户说
#: 「把XX原文件**发送**给我」「**给我**发原文件」「**发一下**原文件」都被漏掉。
_SEND_MARKERS = (
    "发给我", "发我", "发过来", "传给我", "传我", "下载",
    "发送", "给我", "发一下", "发个", "发份", "传一下", "传过来",
)
_QUESTION_MARKERS = ("什么", "怎么", "如何", "多少", "为什么", "哪些", "是否", "有没有", "吗", "谁", "怎样")


def is_catalog_query(text: str) -> bool:
    """问「我能查哪些文档 / 列出我可以查询的文件清单」，应回可见清单，不进检索。"""
    normalized = (text or "").strip()
    if not normalized or is_help_command(normalized):
        return False
    # 私有库口令即使措辞靠近清单，也不在这里接；handler 里它们本来就更靠前。
    if is_personal_list_command(normalized) or personal_delete_title(normalized) is not None:
        return False
    if any(phrase in normalized for phrase in _CATALOG_PHRASES):
        return True
    if "权限" in normalized and any(
        word in normalized for word in ("文档", "文件", "资料", "查询", "查看", "检索")
    ):
        return True
    return _ACCESSIBLE_DOC_LIST_RE.search(normalized) is not None


def file_request_hint(text: str) -> str | None:
    """「把某某文件发给我」抽出标题线索。普通提问返回 None，避免抢问答。"""
    raw = (text or "").strip()
    if not raw:
        return None
    if not any(marker in raw for marker in _SEND_MARKERS):
        return None
    if not any(marker in raw for marker in _FILE_MARKERS):
        return None
    if any(marker in raw for marker in _QUESTION_MARKERS):
        return None
    hint = raw
    # 顺序敏感（长词必须先于短词，否则会留残字）：
    #   * 文件类："原始文件/原文件" 先于 "原文"/"文件"；
    #   * 动词类："发给我/给我发/传给我" 先于裸的 "给我"/"发我"——
    #     否则 "…原文件发给我" 会先删 "给我"，留下一个孤零零的 "发"。
    for noise in (
        "请把", "请将", "帮我把", "帮我将", "麻烦把", "麻烦将",
        "请", "帮我", "麻烦",
        "一份", "这份", "那个", "这个",
        "原始文件", "原文件", "原文档", "原文", "原件", "源文件", "附件", "文件",
        "发送给我", "发送过来", "发送",
        "发给我", "发我一份", "发一份", "发一下", "发个", "发份", "发我", "发过来",
        "传给我", "传我一下", "传我", "传一下", "传过来",
        "给我发", "给我",
        "下载一下", "下载",
        "把", "将",
        "一下", "谢谢",
    ):
        hint = hint.replace(noise, "")
    hint = re.sub(r"[\s，,。.!！的]+", "", hint)
    if len(hint) < 2:
        return None
    return hint


def is_help_command(text: str) -> bool:
    """是否走欢迎/帮助短路（不进问答流水线）。

    除精确触发词外，短句「你好啊 / 帮助一下」也视为帮助；超过长度阈值的
    「你好，请问年假有多少天」仍走正常问答。
    """
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    # 去掉常见 @机器人 前缀残留
    if normalized.startswith("@"):
        parts = normalized.split(None, 1)
        normalized = parts[1].strip() if len(parts) > 1 else ""
    if not normalized:
        return False
    if normalized in HELP_TRIGGERS:
        return True
    if len(normalized) > _HELP_PREFIX_MAX_LEN:
        return False
    for prefix in _HELP_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix):
            rest = normalized[len(prefix) :].strip(" \t，,。.!！?？~～")
            if rest in _HELP_PREFIX_REST_OK:
                return True
    return False


def is_greeting_command(text: str) -> bool:
    """纯问候（你好/您好/hi/hello/在吗）——只回一句介绍，不展开帮助。"""
    normalized = (text or "").strip().lower()
    if normalized.startswith("@"):
        parts = normalized.split(None, 1)
        normalized = parts[1].strip() if len(parts) > 1 else ""
    if not normalized or len(normalized) > 12:
        return False
    for prefix in _GREETING_PREFIXES:
        if normalized == prefix:
            return True
        if normalized.startswith(prefix):
            rest = normalized[len(prefix) :].strip(" \t，,。.!！?？~～")
            if rest in _GREETING_REST_OK:
                return True
    return False


def is_document_list_command(text: str) -> bool:
    """短口令「文档 / 文档清单 / 有哪些文档」——只列可查标题。"""
    normalized = re.sub(r"\s+", "", (text or "").strip().lower())
    return normalized in _DOC_LIST_COMMANDS


#: 文档标题里开头的编号（``XJWY-GC-09`` / ``XJWY-GCYJ-05`` / ``XJ-01-11-1F02``）
_TITLE_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:[-_/][A-Za-z0-9]+)+")


#: P0 命令集（帮助屏只展示这四个；其余为兼容别名）
_PERMISSION_COMMANDS = frozenset(
    {"查询权限", "我的权限", "查看权限", "权限", "我的查询权限"}
)
_EXAMPLE_COMMANDS = frozenset(
    {"问答示例", "示例问答", "示例", "给我示例", "问答例子", "问法示例"}
)
_PERSONAL_MANAGE_COMMANDS = frozenset(
    {"管理个人私有文件", "管理私有文件", "私有文件管理", "个人文件管理", "我的私有文件"}
)
_SEND_SOURCE_COMMANDS = frozenset(
    {"发原文", "我要原文", "怎么发原文", "发原文件", "原文怎么发"}
)
#: 发剩余全部（按批上限）
_SEND_ALL_FIGURES_COMMANDS = frozenset(
    {
        "发全图",
        "全部图片",
        "发全部图片",
        "剩下的图片",
        "剩余图片",
        "补发图片",
        "补发图",
        "发剩余图片",
        "全部发来",
        "发全部",
    }
)
#: 发下一张（计数 +1，不重复）
_SEND_ONE_FIGURE_COMMANDS = frozenset(
    {
        "发图",
        "发图片",
        "发一张",
        "发一张图",
        "发1张",
        "发1张图",
        "再发一张",
        "再发1张",
        "继续发图",
        "下一张图",
        "再来一张",
    }
)
#: 「发3张」「发送12张图」
_SEND_N_FIGURES_RE = re.compile(
    r"^(?:再)?发(?:送)?(?P<n>\d+)张(?:图|图片)?$"
)


def _compact_command(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip().lower())


def is_permission_command(text: str) -> bool:
    return _compact_command(text) in _PERMISSION_COMMANDS


def is_example_command(text: str) -> bool:
    return _compact_command(text) in _EXAMPLE_COMMANDS


def is_personal_manage_command(text: str) -> bool:
    return _compact_command(text) in _PERSONAL_MANAGE_COMMANDS


def is_send_source_command(text: str) -> bool:
    return _compact_command(text) in _SEND_SOURCE_COMMANDS


def is_send_all_figures_command(text: str) -> bool:
    """兼容旧名：是否「发剩余全部」类指令。"""
    return parse_figure_send_count(text) == -1


def parse_figure_send_count(text: str) -> int | None:
    """解析发图指令张数。

    * ``None``：不是发图指令（应走正常问答）
    * ``-1``：发剩余全部（通道按批上限切片）
    * ``>0``：本次发 N 张（按序、跳过已发）
    """
    compact = _compact_command(text)
    if not compact:
        return None
    if compact in _SEND_ALL_FIGURES_COMMANDS:
        return -1
    if compact in _SEND_ONE_FIGURE_COMMANDS:
        return 1
    match = _SEND_N_FIGURES_RE.fullmatch(compact)
    if match:
        return max(1, int(match.group("n")))
    return None


def is_send_figures_command(text: str) -> bool:
    return parse_figure_send_count(text) is not None


def friendly_title(title: str | None) -> str:
    """面向聊天的标题：去掉导入路径、开头编号和「- 定稿」装饰。

    用户反馈 ``XJWY-GCYJ-05`` 对物业/工程同事是噪音，标题才是入口；编号只在
    引用详情/管理台出现。``能耗管理作业指导书 - 定稿`` → ``能耗管理作业指导书（定稿）``。
    """
    text = display_doc_title(title)
    if text == "未知文档":
        return "未命名文档"
    text = _TITLE_CODE_RE.sub("", text).strip()
    text = re.sub(r"\s*[-—－]\s*定稿$", "（定稿）", text).strip()
    return text or display_doc_title(title)


def display_doc_title(title: str | None) -> str:
    """展示用文档名：去掉导入路径前缀（``doc文档/XJWY-…`` → ``XJWY-…``）。"""
    t = (title or "").strip()
    if not t:
        return "未知文档"
    t = t.replace("\\", "/")
    if "/" in t:
        t = t.rsplit("/", 1)[-1].strip()
    return t or "未知文档"


def _short_title(title: str, *, max_len: int = 28) -> str:
    t = display_doc_title(title)
    if t == "未知文档":
        t = "未命名文档"
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


def load_seed_questions(repo_root: Path | None = None) -> list[dict[str, Any]]:
    """运营配置优先，缺省用内置种子（对齐黄金集问法）。"""
    candidates: list[Path] = []
    if repo_root is not None:
        candidates.append(Path(repo_root) / "config" / "discovery_questions.json")
    candidates.append(Path("config") / "discovery_questions.json")
    for path in candidates:
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                items = data.get("questions") if isinstance(data, dict) else data
                if isinstance(items, list) and items:
                    out: list[dict[str, Any]] = []
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        q = str(item.get("question") or "").strip()
                        if not q:
                            continue
                        kws = item.get("title_keywords") or []
                        out.append(
                            {
                                "question": q,
                                "title_keywords": [str(k) for k in kws if str(k).strip()],
                            }
                        )
                    if out:
                        return out
        except (OSError, ValueError, TypeError) as exc:
            log.warning("discovery_seed_load_failed", path=str(path), error=str(exc))
    return list(_DEFAULT_SEEDS)


def list_visible_published_docs(
    runtime: Any,
    perm: PermContext,
    *,
    limit: int = 20,
    admin_bypass: bool = False,
) -> list[dict[str, Any]]:
    """当前用户可见的已发布文档标题清单（与 ``list_documents`` 同行级 ACL）。"""
    limit = max(1, min(int(limit), 100))
    if not admin_bypass and not perm.visible_kb_ids:
        return []

    where = ["d.status = 'published'", "d.deleted_at IS NULL"]
    params: list[Any] = []
    if not admin_bypass:
        where.append("d.level <= ?")
        params.append(int(perm.clearance))
        placeholders = ",".join("?" for _ in perm.visible_kb_ids)
        where.append(f"d.kb_id IN ({placeholders})")
        params.extend(int(k) for k in perm.visible_kb_ids)

    fetch_cap = min(max(limit * 5, 100), 500)
    sql = (
        "SELECT d.doc_id, d.kb_id, k.code AS kb_code, d.title, d.level, d.status, "
        "d.visible_dept_ids, d.effective_date, d.expire_date, d.doc_version, "
        "(SELECT c.shard_id FROM ekbs_chunk c WHERE c.doc_id = d.doc_id LIMIT 1) AS shard_id "
        "FROM ekbs_document d LEFT JOIN ekbs_kb k ON k.kb_id = d.kb_id "
        f"WHERE {' AND '.join(where)} ORDER BY d.updated_at DESC, d.doc_id DESC LIMIT ?"
    )
    with runtime.db.sync_connect() as conn:
        raw_rows = [dict(r) for r in conn.execute(sql, (*params, fetch_cap)).fetchall()]
        doc_ids = [int(r["doc_id"]) for r in raw_rows]
        acl_map = runtime.gate.load_document_acl(doc_ids)
        visible: list[dict[str, Any]] = []
        for record in raw_rows:
            try:
                record["visible_dept_ids"] = json.loads(record.get("visible_dept_ids") or "[]")
            except ValueError:
                record["visible_dept_ids"] = []
            facts = DocumentFacts.from_row(record)
            decision = runtime.gate.can_see_document(
                perm,
                facts,
                acl_map.get(int(record["doc_id"]), []),
                for_retrieval=False,
                allow_deleted=False,
                admin_bypass=admin_bypass,
            )
            if not decision.allowed:
                continue
            visible.append(
                {
                    "doc_id": int(record["doc_id"]),
                    "title": str(record.get("title") or ""),
                    "kb_id": int(record["kb_id"]),
                    "kb_code": record.get("kb_code"),
                    "level": int(record["level"]),
                    "effective_date": record.get("effective_date"),
                }
            )
            if len(visible) >= limit:
                break
        from ekbs.personal import attach_personal_owners

        attach_personal_owners(conn, visible)
    return visible


def _seed_matches_titles(seed: dict[str, Any], titles: list[str]) -> bool:
    keywords = [str(k).strip().lower() for k in (seed.get("title_keywords") or []) if str(k).strip()]
    if not keywords:
        # 无关键词：仅在有可见文档时放行（避免空库泄漏通用题）
        return bool(titles)
    blob = " ".join(titles).lower()
    return any(kw in blob for kw in keywords)


def _docs_with_figures(
    runtime: Any,
    documents: list[dict[str, Any]],
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """在已可见文档里挑出带 figure 块的（拒答找图场景用）。"""
    if not documents:
        return []
    ids = [int(d["doc_id"]) for d in documents if d.get("doc_id") is not None]
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    try:
        with runtime.db.sync_connect() as conn:
            rows = conn.execute(
                f"SELECT doc_id, COUNT(*) AS n FROM ekbs_chunk "
                f"WHERE chunk_type = 'figure' AND doc_id IN ({placeholders}) "
                f"GROUP BY doc_id ORDER BY n DESC LIMIT ?",
                (*ids, int(limit)),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("docs_with_figures_failed", error=str(exc)[:120])
        return []
    by_id = {int(d["doc_id"]): d for d in documents}
    out: list[dict[str, Any]] = []
    for row in rows:
        doc = by_id.get(int(row["doc_id"]))
        if doc is not None:
            out.append(doc)
    return out


def _query_title_overlap(query: str, title: str) -> int:
    """粗粒度词面重合分：拒答提示用来把「二维码」推到熙物联，而不是中央空调。"""
    q = (query or "").strip().lower()
    t = (title or "").strip().lower()
    if not q or not t:
        return 0
    score = 0
    # 连续 2～4 字子串（中文问句无空格）
    for n in (4, 3, 2):
        for i in range(0, max(0, len(q) - n + 1)):
            gram = q[i : i + n]
            if gram.isalnum() is False and not any("\u4e00" <= ch <= "\u9fff" for ch in gram):
                continue
            if gram in t:
                score += n
    return score


def _add_suggestion(
    out: list[dict[str, Any]], seen: set[str], question: str, *,
    source: str, doc_id: int | None = None,
) -> None:
    q = question.strip()
    if not q or q in seen:
        return
    seen.add(q)
    item: dict[str, Any] = {"question": q, "source": source}
    if doc_id is not None:
        item["doc_id"] = doc_id
    out.append(item)


def _suggest_by_query_seeds(
    out: list[dict[str, Any]], seen: set[str], seeds: list[dict[str, Any]],
    titles: list[str], query_l: str, limit: int,
) -> None:
    """0) 与当前问题词面相关的种子优先（仍须标题 ACL）。"""
    for seed in seeds:
        if not _seed_matches_titles(seed, titles):
            continue
        kws = [str(k).strip().lower() for k in (seed.get("title_keywords") or []) if str(k).strip()]
        qtext = str(seed.get("question") or "").lower()
        if any(kw in query_l for kw in kws) or any(
            tok and tok in query_l for tok in qtext.replace("？", "").split() if len(tok) >= 2
        ):
            _add_suggestion(out, seen, str(seed["question"]), source="seed")
        if len(out) >= limit:
            return


def _suggest_for_refusal(
    out: list[dict[str, Any]], seen: set[str], runtime: Any,
    docs: list[dict[str, Any]], query_l: str, limit: int,
) -> None:
    """拒答且问题未命中任何种子：只推相关文档派生问，禁止甩默认合同题。"""
    want_figure = any(k in query_l for k in ("图", "二维码", "二维碼", "照片", "截图", "码"))
    ranked = sorted(
        docs,
        key=lambda d: _query_title_overlap(query_l, str(d.get("title") or "")),
        reverse=True,
    )
    for doc in ranked:
        if _query_title_overlap(query_l, str(doc.get("title") or "")) <= 0:
            break
        title = _short_title(str(doc.get("title") or ""))
        if want_figure:
            _add_suggestion(
                out, seen, f"把「{title}」里的图片发我",
                source="doc_title_figure", doc_id=int(doc["doc_id"]),
            )
        _add_suggestion(
            out, seen, f"「{title}」主要讲了什么？",
            source="doc_title", doc_id=int(doc["doc_id"]),
        )
        if len(out) >= limit:
            return
    # 「二维码」对不上标题时：改推当前可见、且库里真有 figure 块的文档
    if len(out) < limit and want_figure:
        for doc in _docs_with_figures(runtime, docs, limit=limit):
            title = _short_title(str(doc.get("title") or ""))
            _add_suggestion(
                out, seen, f"把「{title}」里的图片发我",
                source="doc_with_figure", doc_id=int(doc["doc_id"]),
            )
            if len(out) >= limit:
                break


def _suggest_by_seeds(
    out: list[dict[str, Any]], seen: set[str], seeds: list[dict[str, Any]],
    titles: list[str], limit: int,
) -> None:
    """1) 其余运营/黄金种子（过标题关键词 ACL）——帮助/导览用。"""
    for seed in seeds:
        if _seed_matches_titles(seed, titles):
            _add_suggestion(out, seen, str(seed["question"]), source="seed")
        if len(out) >= limit:
            return


def _suggest_by_titles(
    out: list[dict[str, Any]], seen: set[str], docs: list[dict[str, Any]], limit: int,
) -> None:
    """2) 从可见文档标题派生（始终 ACL 安全；展示名去路径前缀）。"""
    for doc in docs:
        title = _short_title(str(doc.get("title") or ""))
        _add_suggestion(
            out, seen, f"「{title}」主要讲了什么？",
            source="doc_title", doc_id=int(doc["doc_id"]),
        )
        if len(out) >= limit:
            break


def suggested_questions(
    runtime: Any,
    perm: PermContext,
    *,
    limit: int = 5,
    admin_bypass: bool = False,
    documents: list[dict[str, Any]] | None = None,
    query: str | None = None,
    for_refusal: bool = False,
    for_help: bool = False,
) -> list[dict[str, Any]]:
    """示例问：标题派生（ACL 安全）+ 运营种子（按标题关键词过滤）。

    ``query``：拒答场景传入原问题，优先推与问题词面相关的种子。
    ``for_refusal``：拒答专用——问题对不上任何种子时，**不要**退回默认合同题，
    改为推标题与问题有重合的文档派生问（避免「要二维码却引导中央空调」）。
    """
    limit = max(1, min(int(limit), 20))
    docs = documents if documents is not None else list_visible_published_docs(
        runtime, perm, limit=max(limit, 10), admin_bypass=admin_bypass
    )
    titles = [str(d.get("title") or "") for d in docs]
    query_l = (query or "").strip().lower()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    seeds = load_seed_questions(getattr(getattr(runtime, "settings", None), "repo_root", None))

    if query_l:
        _suggest_by_query_seeds(out, seen, seeds, titles, query_l, limit)
        if len(out) >= limit:
            return out[:limit]
    if for_refusal and query_l:
        _suggest_for_refusal(out, seen, runtime, docs, query_l, limit)
        return out[:limit]
    _suggest_by_seeds(out, seen, seeds, titles, limit)
    if len(out) >= limit:
        return out[:limit]
    if for_help:
        # P0：帮助只给人工/种子问法；不再退回「「标题」主要讲了什么？」的弱示例。
        return out[:limit]
    _suggest_by_titles(out, seen, docs, limit)
    return out[:limit]


def _longest_common_substring(left: str, right: str) -> int:
    if not left or not right:
        return 0
    best = 0
    prev = [0] * (len(right) + 1)
    for char_left in left:
        current = [0] * (len(right) + 1)
        for index, char_right in enumerate(right, start=1):
            if char_left == char_right:
                current[index] = prev[index - 1] + 1
                if current[index] > best:
                    best = current[index]
        prev = current
    return best


def _match_key(title: str) -> str:
    text = display_doc_title(title).lower()
    text = re.sub(r"\.[a-z0-9]{1,5}$", "", text)
    text = re.sub(r"v\d+$", "", text)
    return re.sub(r"[\s_\-—]+", "", text)


def title_match_score(hint: str, title: str) -> int:
    """标题吻合分。整段包含给 100 以上；否则用最长公共子串长度。"""
    needle = _match_key(hint)
    hay = _match_key(title)
    if len(needle) < 2 or not hay:
        return 0
    if needle in hay or hay in needle:
        return 100 + min(len(needle), len(hay))
    return _longest_common_substring(needle, hay)


def resolve_file_request(
    runtime: Any,
    perm: PermContext,
    text: str,
    *,
    admin_bypass: bool = False,
) -> dict[str, Any]:
    """按当前用户可见文档，判断能不能发某一份原文。

    ``action``：``send`` 唯一命中；``choose`` 多份接近；``none`` 对不上。
    不读文件字节，调用方再按 ``doc_id`` 回源。
    """
    hint = file_request_hint(text)
    documents = list_visible_published_docs(
        runtime, perm, limit=100, admin_bypass=admin_bypass
    )
    if not hint:
        return {"action": "none", "message": "请说明要哪一份文件。", "doc_id": None, "candidates": []}

    scored = [
        (title_match_score(hint, str(doc.get("title") or "")), doc)
        for doc in documents
    ]
    scored = [(score, doc) for score, doc in scored if score > 0]
    scored.sort(key=lambda item: (-item[0], int(item[1]["doc_id"])))

    def _names(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {"doc_id": int(doc["doc_id"]), "title": display_doc_title(str(doc.get("title") or ""))}
            for doc in rows
        ]

    if not scored or scored[0][0] < 4:
        lines = [f"在你能查询的文档里，没有找到和「{hint}」对应的文件。"]
        if documents:
            lines.append("你当前可以要这些原文：")
            for index, doc in enumerate(documents[:8], 1):
                lines.append(f"{index}. {display_doc_title(str(doc.get('title') or ''))}")
            lines.append("直接说「把某某文件发给我」。")
        else:
            lines.append("你当前没有可见的已发布文档。")
        return {
            "action": "none",
            "message": "\n".join(lines),
            "doc_id": None,
            "candidates": _names(documents[:8]),
        }

    strong = [doc for score, doc in scored if score >= 100]
    if len(strong) == 1:
        chosen = strong[0]
    elif len(strong) > 1:
        lines = ["有多份文档都对得上，请点名其中一份："]
        for index, doc in enumerate(strong[:8], 1):
            lines.append(f"{index}. {display_doc_title(str(doc.get('title') or ''))}")
        return {
            "action": "choose",
            "message": "\n".join(lines),
            "doc_id": None,
            "candidates": _names(strong[:8]),
        }
    else:
        best = scored[0][0]
        close = [doc for score, doc in scored if score >= 4 and score >= best - 1]
        if len(close) != 1:
            lines = [f"「{hint}」对应到不止一份文档，请点名其中一份："]
            for index, doc in enumerate(close[:8], 1):
                lines.append(f"{index}. {display_doc_title(str(doc.get('title') or ''))}")
            return {
                "action": "choose",
                "message": "\n".join(lines),
                "doc_id": None,
                "candidates": _names(close[:8]),
            }
        chosen = close[0]

    title = display_doc_title(str(chosen.get("title") or ""))
    return {
        "action": "send",
        "message": f"已按你的权限发送原文：{title}",
        "doc_id": int(chosen["doc_id"]),
        "candidates": [{"doc_id": int(chosen["doc_id"]), "title": title}],
    }


def format_help_text(
    documents: list[dict[str, Any]],
    suggestions: list[dict[str, Any]],
    *,
    group_chat: bool = False,
) -> str:
    """钉钉欢迎/帮助纯文本（可复制或原样发送示例问）。

    ``group_chat=True``：群聊只允许公开文档（个人库已在 API 侧从 perm 剪掉），
    文案也要明确“群聊/公开”，并去掉个人库上传/删除的引导，避免误导。
    """
    lines = [
        "你好，我是企业知识库助手。",
        "你可以直接问制度、合同、表格里的具体条款；也可以发送「帮助」随时回来看这份导览。",
        "",
    ]
    if group_chat:
        lines[1] = (
            "群聊中我只能基于公开文档回答；个人私有库/文件请私聊我处理。"
            "也可以发送「帮助」随时回来看这份导览。"
        )
    if documents:
        lines.append("当前群聊可查询的公开文档：" if group_chat else "你当前可见的文档：")
        shown = documents[:40]
        for i, doc in enumerate(shown, 1):
            lines.append(f"{i}. {_short_title(str(doc.get('title') or ''), max_len=40)}")
        if len(documents) > len(shown):
            lines.append(f"…共 {len(documents)} 份（仅展示前 {len(shown)}）")
    else:
        lines.append("你当前没有可见的已发布文档。若认为权限有误，请联系管理员。")

    if suggestions:
        lines.append("")
        lines.append("可以这样问我（直接发送整句即可）：")
        for i, item in enumerate(suggestions[:5], 1):
            lines.append(f"{i}. {item['question']}")

    lines.append("")
    lines.append("要某份原文，直接说「把某某文件发给我」，我会发原文件，而不是摘要。")
    if group_chat:
        lines.append("私有库文件与个人文件操作（我的文件/上传/删除）请在单聊里进行。")
    else:
        lines.append("在单聊里直接发文件，会进入只属于你的私有库。说「我的文件」查看，说「删除我的文件 标题」删除。")
    lines.append("提示：没有可靠依据时我会拒答，不会编造。")
    return "\n".join(lines)


_PERSONAL_LIST = {"我的文件", "我的文档", "我的私有库", "私有库", "个人库", "我的私人库"}
_PERSONAL_DELETE = re.compile(
    r"^(?:请)?(?:帮我)?(?:把|将)?(?:删除|删掉|移除)"
    r"(?:我的)?(?:私有库|私人库|个人库)?(?:里|中)?(?:的)?"
    r"(?:文件|文档)?"
    r"\s*[：:]?\s*(?P<title>.*)$"
)


def is_personal_list_command(text: str) -> bool:
    compact = re.sub(r"\s+", "", (text or "").strip())
    return compact in _PERSONAL_LIST


def personal_delete_title(text: str) -> str | None:
    """返回要删的标题。不是删除指令时返回 None；是指令但没写标题时返回空串。"""
    raw = (text or "").strip()
    if not raw or not re.search(r"删除|删掉|移除", raw):
        return None
    if not re.search(r"私有|私人|个人库|我的文件|我的文档", raw):
        return None
    match = _PERSONAL_DELETE.match(raw)
    if not match:
        return ""
    return match.group("title").strip()


def format_help_text_short(
    suggestions: list[dict[str, Any]],
    *,
    group_chat: bool = False,
) -> str:
    """P0 帮助：唯一入口；版式对齐问答回复（小节标题 + 有序列表）。"""
    lines = [
        "直接发送文字：",
        "1. 查询权限",
        "2. 问答示例 — 按你的权限生成示例问答",
        "3. 管理个人私有文件（仅单聊：直接发文件入库；删除说「删除我的文件 标题」）",
        "4. 发原文（直接说：发我工程部10月工作汇报ppt原件）",
        "",
        "也可以直接提问，例如：跑水事件环境部先做什么？",
    ]
    if group_chat:
        lines.append("")
        lines.append("群聊中我只能基于公开文档回答。")
    return "\n".join(lines)


def format_permission_text(documents: list[dict[str, Any]], clearance: int) -> str:
    """查询权限：只给文档名称/数量/密级，不返回正文或 ACL 细节。"""
    if not documents:
        return "当前没有可查文档。若认为权限有误，请联系管理员。"
    private_count = sum(
        1
        for doc in documents
        if str(doc.get("kb_code") or "").startswith("personal-")
    )
    header = f"可查文档 {len(documents)} 篇"
    if private_count:
        header += f"（私有 {private_count} 篇）"
    titles = "、".join(friendly_title(str(doc.get("title") or "")) for doc in documents)
    return f"{header}，密级 1~{int(clearance)}：\n{titles}"


def format_examples_text(suggestions: list[dict[str, Any]]) -> str:
    """问答示例：只给问题文字，不给答案/正文。"""
    questions = [
        str(item.get("question") or "").strip()
        for item in suggestions
        if item.get("question")
    ]
    questions = [q for q in questions if q][:3]
    if not questions:
        return "暂无问答示例，直接提问即可。"
    lines = ["问答示例（按你的权限生成）："]
    lines.extend(f"· {question}" for question in questions)
    return "\n".join(lines)


def format_private_manage_text() -> str:
    return (
        "管理个人私有文件（仅单聊）：\n"
        "· 直接发文件给我 → 入库到你的私有库\n"
        "· 我的文件 → 查看\n"
        "· 删除我的文件 标题 → 删除"
    )


def format_send_source_text() -> str:
    return "直接说：发我工程部10月工作汇报ppt原件"


def format_document_list_text(documents: list[dict[str, Any]]) -> str:
    """P0 文档清单：只列可读标题，隐藏编号；没有文档时给出权限提示。"""
    if not documents:
        return "当前没有可查的已发布文档。若认为权限有误，请联系管理员。"
    lines = [f"可查文档（{len(documents)}）："]
    for doc in documents:
        lines.append(f"· {friendly_title(str(doc.get('title') or ''))}")
    lines.append("")
    lines.append("直接问标题或具体条款即可。")
    return "\n".join(lines)


def format_refusal_hints(suggestions: list[dict[str, Any]]) -> str:
    """拒答时附在答案后的 2～3 条相关示例问。"""
    qs = [str(s.get("question") or "").strip() for s in suggestions if s.get("question")]
    qs = [q for q in qs if q][:3]
    if not qs:
        return ""
    lines = ["", "你可以试试问："]
    for i, q in enumerate(qs, 1):
        lines.append(f"{i}. {q}")
    return "\n".join(lines)


def build_catalog(
    runtime: Any,
    perm: PermContext,
    *,
    doc_limit: int = 20,
    suggestion_limit: int = 5,
    admin_bypass: bool = False,
    group_chat: bool = False,
    mode: str = "full",
) -> dict[str, Any]:
    """组装发现层响应体。

    ``mode``：
    * ``help``（P0 钉钉“帮助”）——只给 3 条业务示例 + 提示「回复文档」，不列清单；
    * ``docs``（P0 钉钉“文档”）——只列可读标题，隐藏编号；
    * ``full``（管理台/旧调用）——沿用原来的“清单+示例+私有库说明”长文。
    """
    documents = list_visible_published_docs(
        runtime, perm, limit=doc_limit, admin_bypass=admin_bypass
    )
    if mode in {"docs", "help", "permissions"}:
        suggestions: list[dict[str, Any]] = []
    else:
        suggestions = suggested_questions(
            runtime,
            perm,
            limit=suggestion_limit,
            admin_bypass=admin_bypass,
            documents=documents,
            for_help=(mode == "help" or mode == "examples"),
        )
    if mode == "help":
        help_text = format_help_text_short(suggestions, group_chat=group_chat)
    elif mode == "permissions":
        help_text = format_permission_text(documents, perm.clearance)
    elif mode == "examples":
        help_text = format_examples_text(suggestions)
    elif mode == "docs":
        help_text = format_document_list_text(documents)
    else:
        help_text = format_help_text(documents, suggestions, group_chat=group_chat)
    return {
        "documents": documents,
        "suggestions": suggestions,
        "help_text": help_text,
        "document_count": len(documents),
    }
