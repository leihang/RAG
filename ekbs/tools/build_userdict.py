"""挖掘 jieba **用户词典**候选（只读，不改任何索引）。

## 为什么需要用户词典

BM25/FTS 的词项来自 jieba。通用词典不认识我们语料里的**编号、设备名、制度术语**，
于是把它们切碎，倒排索引里就变成一堆无意义的碎片：

    实测：`XJWY-GC-01-01` → ['xjwy', '-', 'gc', '-', '01', '-', '01']（7 个词项）
          `维保费`、`交接班`、`能耗分析` 等整词也可能被拆开

把整词加进用户词典后，它成为**单个词项** → BM25 精确匹配变准。
**注意：不动向量**，只需重建 FTS 预分词索引（`content_tok`）。

## 抽取规则（四类证据）

* **A 编号/代码**：`XJWY-GC-01-01`、`XIWY-QR-GC-002`、合同号等
* **B 书名号**：`《维修/设备组交接班记录》`
* **C 括号内术语**：`（投屏展示）`
* **D 新词发现（n-gram + 邻接熵）**：高频且左右邻字多样的片段，且**当前被 jieba 切开**
  —— 这类是"语料里真实存在、但通用词典不认识"的词（如 `维保费`、`交接班`）

## 用法

    .venv/bin/python -m ekbs.tools.build_userdict                    # 从库 + 桌面 Office 文件
    .venv/bin/python -m ekbs.tools.build_userdict --source db        # 只用已入库文本
    .venv/bin/python -m ekbs.tools.build_userdict --min-count 3 --out var/dict/candidates.txt

产物是**候选**（含频次与证据），需要人工过一遍再转成正式词典 —— 见
`var/dict/userdict.txt` 的用法说明。词典加错词（跨词边界的串）会**降低**质量，所以不自动化上线。
"""

from __future__ import annotations

import argparse
import collections
import pathlib
import re
import sqlite3
import sys

RAG = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAG))

from ekbs.index.fts import tokenize  # noqa: E402

#: 编号/代码：字母+数字+分隔符（如 XJWY-GC-01-01 / XIWY-QR-GC-002 / GB/T 10001）
CODE_RE = re.compile(r"[A-Za-z]{2,}(?:[-/][A-Za-z0-9]+){1,}")
BOOK_RE = re.compile(r"《([^》]{2,30})》")
PAREN_RE = re.compile(r"[（(]([^）)]{2,20})[）)]")
#: 中文候选片段（n-gram 的原料）
CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
#: **虚词/功能字**：候选若以它开头或结尾，几乎一定是"跨词边界碎片"（实测抽到 `的电`、`设备的`、
#: `了电`、`上的`）—— 这类词加进词典会**降低** BM25 精度，必须过滤。
FUNCTION_CHARS = set("的了在和与及为是上下内外中各种其之把被就让向从对到得着过吗呢啊或等能被所把"
                     "一二三四五六七八九十这那有些个之也还很更最不没未无要会想很再又已经于对")
#: 新词候选的邻接熵下限（nats）。太低会把"恰好相邻"的碎片当成词：实测阈值 1.0 时
#: 前 32 条里 60% 是噪声；1.5 能滤掉绝大多数虚假搭配。
DEFAULT_MIN_ENTROPY = 1.5


def corpus_texts(sources: list[str], db: pathlib.Path | None) -> list[tuple[str, str]]:
    """返回 ``[(来源, 文本)]``。"""
    out: list[tuple[str, str]] = []
    if "db" in sources and db and db.is_file():
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        for row in con.execute(
            "SELECT d.title AS title, c.heading_path AS heading_path, c.content AS content "
            "FROM ekbs_chunk c JOIN ekbs_document d ON d.doc_id = c.doc_id "
            "WHERE d.status = 'published'"
        ):
            out.append((f"db/{row['title'][:16]}", f"{row['title']}\n{row['heading_path']}\n{row['content']}"))
    if "desk" in sources:
        # Office 文件直接解析（PDF 走 MinerU 较慢，需显式 --with-pdf）
        for base in ("doc文档", "xlsx表格", "PPT-混合"):
            directory = pathlib.Path.home() / "Desktop" / base
            if not directory.is_dir():
                continue
            for path in sorted(directory.iterdir()):
                if path.suffix.lower() not in (".docx", ".pptx", ".xlsx", ".doc"):
                    continue
                try:
                    from ekbs.ingest.parser import parse_file

                    parsed = parse_file(path)
                except Exception:  # noqa: BLE001
                    continue
                text = "\n".join([parsed.markdown] + [getattr(t, "body", "") or "" for t in parsed.tables])
                out.append((f"file/{path.name[:20]}", text))
    return out


def code_candidates(texts: list[tuple[str, str]]) -> dict[str, dict]:
    """A 类：编号/代码。"""
    found: dict[str, dict] = {}
    for source, text in texts:
        for match in CODE_RE.finditer(text):
            word = match.group(0)
            entry = found.setdefault(word.lower(), {"word": word, "count": 0, "kinds": set(), "sources": set()})
            entry["count"] += 1
            entry["kinds"].add("A 编号")
            entry["sources"].add(source)
    return found


def _mostly_cjk(word: str, *, ratio: float = 0.6) -> bool:
    """以中文为主 —— 挡住 `+0.03万元`、`125部`、`DIV/0` 这类非术语串。"""
    if not word:
        return False
    cjk = sum(1 for ch in word if "\u4e00" <= ch <= "\u9fff")
    return cjk / len(word) >= ratio


def quoted_candidates(texts: list[tuple[str, str]]) -> dict[str, dict]:
    """B/C 类：书名号内、括号内术语。"""
    found: dict[str, dict] = {}
    for source, text in texts:
        for regex, kind in ((BOOK_RE, "B 书名号"), (PAREN_RE, "C 括号内")):
            for match in regex.finditer(text):
                word = match.group(1).strip()
                if len(word) < 2 or not _mostly_cjk(word) or word[0] in "+-0123456789":
                    continue
                entry = found.setdefault(word, {"word": word, "count": 0, "kinds": set(), "sources": set()})
                entry["count"] += 1
                entry["kinds"].add(kind)
                entry["sources"].add(source)
    return found


def _boundary_ok(word: str) -> bool:
    """候选首尾不能是虚词/功能字（挡住 `的电`、`设备的`、`了电` 这类碎片）。"""
    return word[0] not in FUNCTION_CHARS and word[-1] not in FUNCTION_CHARS


def new_word_candidates(
    texts: list[tuple[str, str]],
    *,
    min_count: int,
    max_len: int = 8,
    min_entropy: float = DEFAULT_MIN_ENTROPY,
) -> dict[str, dict]:
    """D 类：n-gram 新词发现（频次 + 左右邻字熵），且**当前被 jieba 切开**。

    为什么要求"被切开"：已经是一个整词的没必要加；我们要找的是**通用词典不认识、
    却在语料里反复出现**的片段（编号、设备名、制度术语）。
    """
    import math

    grams: collections.Counter[str] = collections.Counter()
    left: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    right: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for _, text in texts:
        for run in CJK_RUN_RE.findall(text):
            for size in range(2, max_len + 1):
                for start in range(len(run) - size + 1):
                    gram = run[start : start + size]
                    grams[gram] += 1
                    if start > 0:
                        left[gram][run[start - 1]] += 1
                    if start + size < len(run):
                        right[gram][run[start + size]] += 1

    def entropy(counter: collections.Counter[str]) -> float:
        total = sum(counter.values())
        if total <= 1:
            return 0.0
        return -sum((n / total) * math.log(n / total) for n in counter.values())

    #: 语料里出现过的整词（jieba 已认可的）
    known = {word for _, text in texts for word in tokenize(text) if len(word) >= 2}
    found: dict[str, dict] = {}
    for gram, count in grams.items():
        if count < min_count or gram in known:
            continue
        if entropy(left[gram]) < min_entropy or entropy(right[gram]) < min_entropy:
            continue
        if not _boundary_ok(gram):
            continue
        # 内部含虚词却又不以虚词起止（如 `期消防`）→ 也要求熵更硬
        if any(ch in FUNCTION_CHARS for ch in gram) and entropy(left[gram]) < min_entropy * 1.5:
            continue
        # 避免"包含关系"刷屏：若更长/更短的候选都存在，保留频次最高的那个由使用方裁剪
        found[gram] = {"word": gram, "count": count, "kinds": {"D 新词"}, "sources": set()}
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="挖掘 jieba 用户词典候选（只读）")
    ap.add_argument("--source", action="append", default=None, choices=["db", "desk"],
                    help="db=已入库文本；desk=桌面 Office 文件（可多次）")
    ap.add_argument("--min-count", type=int, default=3, help="新词候选最低出现次数")
    ap.add_argument("--limit", type=int, default=60, help="最多输出多少条候选")
    ap.add_argument("--out", default="var/dict/userdict_candidates.txt")
    args = ap.parse_args()

    sources = args.source or ["db", "desk"]
    texts = corpus_texts(sources, RAG / "var/index/ekbs.db")
    if not texts:
        print("没有可用的语料文本（先入库，或用 --source desk）")
        return 1
    characters = sum(len(text) for _, text in texts)

    merged: dict[str, dict] = {}
    for table in (code_candidates(texts), quoted_candidates(texts),
                  new_word_candidates(texts, min_count=args.min_count)):
        for key, entry in table.items():
            if key in merged:
                merged[key]["count"] += entry["count"]
                merged[key]["kinds"] |= entry["kinds"]
                merged[key]["sources"] |= entry["sources"]
            else:
                merged[key] = entry

    def rank(entry: dict) -> tuple:
        kinds = entry["kinds"]
        # A/B 类规则证据最强；C/D 类需要人工过目
        high = bool(kinds & {"A 编号", "B 书名号"})
        return (0 if high else 1, -entry["count"], entry["word"])

    rows = sorted(merged.values(), key=rank)[: args.limit]
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# jieba 用户词典候选（自动生成，**需人工过一遍**）",
        f"# 来源: {'+'.join(sources)}　语料字符数: {characters}　生成条目: {len(rows)}",
        "# 格式（正式词典 var/dict/userdict.txt）：每行 `词 词频 [词性]`，UTF-8，`#` 开头为注释",
        "# ⚠️ 加错词（跨词边界的串）会**降低**检索质量，所以不能自动上线",
        "",
    ]
    noisy = sum(1 for e in rows if not _boundary_ok(e["word"]))
    print(f"  语料：{len(texts)} 段文本 / {characters} 字符；候选 {len(rows)} 条"
          f"（虚词边界残留 {noisy} 条）\n")
    print(f"  {'推荐':<4} {'候选词':<24} {'频次':>4}  {'类型':<10} 当前 jieba 切法")
    for entry in rows:
        pieces = tokenize(entry["word"])
        mark = "✅已是整词" if len(pieces) == 1 else f"{len(pieces)} 片"
        kinds = "/".join(sorted(entry["kinds"]))
        star = "★" if entry["kinds"] & {"A 编号", "B 书名号"} else "·"
        print(f"  {star:<4} {entry['word'][:22]:<24} {entry['count']:>4}  {kinds:<10} {pieces} → {mark}")
        lines.append(f"{entry['word']} {entry['count']}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n  候选已写出：{out}")
    print("  下一步：人工过一遍 → 转成 var/dict/userdict.txt → 重建 FTS（**不动向量**）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
