"""用 RAGAS TestsetGenerator 从真实语料生成中文测试集（人工复核前）。

输出：
- var/eval/real/testset_raw.jsonl  机器可读（后续评测用）
- var/eval/real/testset_review.xlsx 人工复核表

用法（eval venv）：
    set -a; . ~/.ekbs/secrets.env; set +a
    var/eval/venv/bin/python eval/ragas_testset.py --size 20 --docs 30 --seed 42
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "var" / "eval" / "real"
DB = REPO / "var" / "index" / "ekbs.db"


def _connect():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def _sample_docs(limit_docs: int, seed: int) -> list[dict]:
    """分层抽样：所有 L3 小库 + 其余按分片配额；每篇取代表性块。"""
    rng = random.Random(seed)
    con = _connect()
    try:
        rows = con.execute(
            """
            SELECT d.doc_id, d.title, d.level,
                   (SELECT c.shard_id FROM ekbs_chunk c WHERE c.doc_id=d.doc_id LIMIT 1) AS shard_id,
                   COUNT(c.chunk_id) AS n_chunks
            FROM ekbs_document d JOIN ekbs_chunk c ON c.doc_id=d.doc_id
            WHERE d.status='published'
            GROUP BY d.doc_id
            """
        ).fetchall()
    finally:
        con.close()
    docs = [dict(r) for r in rows]
    by_shard: dict[str, list[dict]] = {}
    for d in docs:
        by_shard.setdefault(str(d["shard_id"] or ""), []).append(d)
    picked: list[dict] = []
    # 小分片全取；大分片按数量配额
    for shard, items in by_shard.items():
        quota = max(1, round(limit_docs * len(items) / max(1, len(docs))))
        rng.shuffle(items)
        picked.extend(items[:quota])
    picked = picked[:limit_docs]
    for d in picked:
        d["chunks"] = []
    ids = [int(d["doc_id"]) for d in picked]
    if not ids:
        return []
    con = _connect()
    try:
        placeholders = ",".join("?" for _ in ids)
        for row in con.execute(
            f"SELECT chunk_id, doc_id, content, heading_path, chunk_type FROM ekbs_chunk "
            f"WHERE doc_id IN ({placeholders}) ORDER BY doc_id, seq", ids
        ):
            for d in picked:
                if int(d["doc_id"]) == int(row["doc_id"]):
                    d["chunks"].append(dict(row))
                    break
    finally:
        con.close()
    return picked


def main() -> int:  # noqa: C901 - 一次性评测脚本（argparse + 生成循环）
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=20, help="生成题数")
    parser.add_argument("--docs", type=int, default=30, help="抽样文档数")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-chunks-per-doc", type=int, default=6)
    parser.add_argument("--out", default=str(OUT_DIR / "testset_raw.jsonl"))
    parser.add_argument("--xlsx", default=str(OUT_DIR / "testset_review.xlsx"))
    parser.add_argument("--generator-model", default="deepseek-chat")
    parser.add_argument("--generator-base-url", default="https://api.deepseek.com/v1")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dashscope = os.environ.get("DASHSCOPE_API_KEY")
    deepseek = os.environ.get("DEEPSEEK_API_KEY")
    if not dashscope or not deepseek:
        print("缺少 DASHSCOPE_API_KEY / DEEPSEEK_API_KEY（先 source ~/.ekbs/secrets.env）")
        return 2

    docs = _sample_docs(args.docs, args.seed)
    print(f"抽样 {len(docs)} 篇，块数 {sum(len(d['chunks']) for d in docs)}")

    from langchain_core.documents import Document as LCDocument
    from langchain_openai import OpenAIEmbeddings
    from openai import OpenAI
    from ragas import RunConfig
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import llm_factory
    from ragas.testset import TestsetGenerator
    from ragas.testset.transforms.engine import Parallel
    from ragas.testset.transforms.extractors import EmbeddingExtractor, SummaryExtractor
    from ragas.testset.transforms.extractors.llm_based import NERExtractor, ThemesExtractor
    from ragas.testset.transforms.relationship_builders import CosineSimilarityBuilder, OverlapScoreBuilder

    llm = llm_factory(
        args.generator_model,
        provider="openai",
        client=OpenAI(api_key=deepseek, base_url=args.generator_base_url, timeout=180, max_retries=3),
    )
    embeddings = LangchainEmbeddingsWrapper(
        OpenAIEmbeddings(
            model="qwen3.7-text-embedding-flash",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            api_key=dashscope,
            dimensions=1024,
            tiktoken_enabled=False,
            check_embedding_ctx_length=False,
        )
    )
    generator = TestsetGenerator(
        llm=llm,
        embedding_model=embeddings,
        llm_context="你是企业知识库测试专家。请只根据给定中文文档生成中文问题与参考答案；"
                    "问题要具体、可核对，答案必须能在原文中找到依据。",
    )

    # RAGAS 要求单篇 >100 tokens；把同一文档的连续块拼成 320 字左右的窗口，
    # 窗口 metadata 记录它包含的 chunk_ids，生成后仍能映射回 gold 分块。
    lc_docs: list[LCDocument] = []
    window_index: dict[str, dict] = {}
    chunk_text_index: dict[str, dict] = {}
    all_chunks: list[dict] = []
    for d in docs:
        chunks = d["chunks"][: args.max_chunks_per_doc]
        buf: list[dict] = []
        size = 0

        def flush():
            nonlocal buf, size
            if not buf:
                return
            parts = []
            for c in buf:
                heading = str(c.get("heading_path") or "").strip()
                parts.append((f"## {heading}\n" if heading else "") + str(c.get("content") or "").strip())
            text = "\n\n".join(p for p in parts if p)
            if len(text) >= 180:
                lc_docs.append(LCDocument(page_content=text, metadata={
                    "doc_id": int(d["doc_id"]), "chunk_ids": [int(c["chunk_id"]) for c in buf],
                    "title": str(d["title"]),
                }))
                window_index[text] = {
                    "doc_id": int(d["doc_id"]), "title": str(d["title"]),
                    "chunk_ids": [int(c["chunk_id"]) for c in buf],
                }
            buf = []
            size = 0

        for c in chunks:
            content = str(c.get("content") or "")
            if not content.strip():
                continue
            chunk_text_index[content.strip()] = {
                "chunk_id": int(c["chunk_id"]), "doc_id": int(d["doc_id"]), "title": str(d["title"]),
            }
            all_chunks.append({
                "chunk_id": int(c["chunk_id"]), "doc_id": int(d["doc_id"]),
                "title": str(d["title"]), "content": content.strip(),
            })
            buf.append(c)
            size += len(content)
            if size >= 320:
                flush()
        flush()
    print(f"送入 RAGAS 的文档窗口：{len(lc_docs)}（来自 {len(docs)} 篇）")

    # 不用 default_transforms：它会触发 HeadlinesExtractor/HeadlineSplitter，
    # 中文表格/长文档上偶发 "headlines not found" 直接中断。这里只保留生成题所需的
    # summary/entities/themes/embedding/关系，避免 splitter。
    summary = SummaryExtractor(llm=llm)
    ner = NERExtractor(llm=llm)
    themes = ThemesExtractor(llm=llm)
    emb = EmbeddingExtractor(
        embedding_model=embeddings, property_name="summary_embedding", embed_property_name="summary"
    )
    cosine = CosineSimilarityBuilder(
        property_name="summary_embedding", new_property_name="summary_similarity", threshold=0.5
    )
    overlap = OverlapScoreBuilder(property_name="entities", threshold=0.01)
    transforms = [summary, Parallel(emb, themes, ner), Parallel(cosine, overlap)]

    started = time.time()
    testset = generator.generate_with_langchain_docs(
        lc_docs,
        testset_size=args.size,
        transforms=transforms,
        run_config=RunConfig(timeout=240, max_workers=2),
        raise_exceptions=False,
    )
    df = testset.to_pandas()
    print(f"生成 {len(df)} 条，用时 {time.time()-started:.1f}s，列：{list(df.columns)}")

    rows: list[dict] = []
    for i, row in enumerate(df.to_dict(orient="records"), 1):
        ctx = row.get("reference_contexts") or []
        if isinstance(ctx, str):
            ctx = [ctx]
        exact: list[dict] = []
        ctx_texts = [str(x).strip() for x in ctx]
        for text in ctx_texts:
            hit = window_index.get(text)
            if hit:
                for cid in hit["chunk_ids"]:
                    item = {"chunk_id": cid, "doc_id": hit["doc_id"]}
                    if item not in exact:
                        exact.append(item)
                continue
            # RAGAS 会改写/拼接头上下文：用 rapidfuzz 找最相似的块（中文长文本）
            try:
                from rapidfuzz import fuzz
            except Exception:
                fuzz = None
            scored: list[tuple[float, dict]] = []
            if fuzz is not None and text:
                for c in all_chunks:
                    score = fuzz.partial_ratio(text[:4000], c["content"][:4000])
                    if score >= 65:
                        scored.append((float(score), c))
            scored.sort(key=lambda x: x[0], reverse=True)
            for score, c in scored[:2]:
                item = {"chunk_id": c["chunk_id"], "doc_id": c["doc_id"]}
                if item not in exact:
                    exact.append(item)
        rows.append({
            "id": f"real-{i:04d}",
            "question": str(row.get("user_input") or ""),
            "reference": str(row.get("reference") or ""),
            "reference_contexts": [str(x) for x in ctx],
            "gold_chunk_ids": [x["chunk_id"] for x in exact],
            "gold_doc_ids": sorted({x["doc_id"] for x in exact}),
            "gold_doc_titles": [],
            "synthesizer": str(row.get("synthesizer_name") or ""),
            "review_status": "待复核",
            "review_notes": "",
            "edited_question": "",
            "edited_answer": "",
        })

    # 补 gold 文档标题，方便人工核对
    title_map: dict[int, str] = {}
    con = _connect()
    try:
        ids = sorted({did for r in rows for did in r["gold_doc_ids"]})
        if ids:
            ph = ",".join("?" for _ in ids)
            title_map = {int(r["doc_id"]): str(r["title"]) for r in con.execute(
                f"SELECT doc_id,title FROM ekbs_document WHERE doc_id IN ({ph})", ids
            )}
    finally:
        con.close()
    for r in rows:
        r["gold_doc_titles"] = [title_map.get(int(d), str(d)) for d in r["gold_doc_ids"]]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    print(f"写出 {out}")

    # 人工复核表（openpyxl）
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "测试集复核"
    headers = ["ID", "合成类型", "问题", "参考答案", "问题（人工修改）", "答案（人工修改）",
               "gold_doc_ids", "gold文档", "gold_chunk_ids", "审核状态", "审核意见"]
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="D9E1F2")
        c.alignment = Alignment(horizontal="center", vertical="center")
    for r in rows:
        ws.append([r["id"], r["synthesizer"], r["question"], r["reference"],
                   r["edited_question"], r["edited_answer"],
                   ",".join(map(str, r["gold_doc_ids"])),
                   "；".join(r.get("gold_doc_titles") or []),
                   ",".join(map(str, r["gold_chunk_ids"])),
                   r["review_status"], r["review_notes"]])
    widths = [10, 16, 45, 55, 35, 35, 16, 40, 20, 12, 35]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:K{ws.max_row}"
    xlsx = Path(args.xlsx)
    wb.save(xlsx)
    print(f"写出 {xlsx}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
