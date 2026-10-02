"""用桌面那份 `hybrid_rag_eval.py` 在**真实语料**上跑评测。

## 这个运行器做了什么 / 没做什么

**没改**：桌面那份脚本一个字都没动。它是通过 ``importlib`` 原样加载的，
`HybridRetrieverEvaluator` 的 BM25、MinMax 归一化、``alpha`` 加权融合、Recall@K 计算
全部是脚本自己的代码。

**替换了一处**（必须说清）：脚本 `__init__` 里那句
``SentenceTransformer("BAAI/bge-small-zh")`` 需要 `torch`（~2GB 下载）。
本机 `.venv` 没有 torch，但**我们库里已经有同一批 chunk 的向量**（生产用的
`text-embedding-v4`，1024 维、L2 归一化，存在 `var/index/kb_all_L1.faiss`）。
所以注入一个**鸭子类型替身**：`encode(corpus)` 返回库里已有的向量，
`encode([query])` 用同一个生产模型现算（结果一致、可复现）。
→ 结论里比较的是「同一个脚本的融合逻辑」在「我们真实语料的真实向量」上的表现。

**另一处已知差异**（脚本自己的注释也承认）：它用 ``doc.split()`` 做分词，
中文没有空格 → 每篇文档会变成**一个 token**，BM25 实质失效。
所以跑两轮：`RUN A` 原样（预期 alpha 不起作用 = 退化成纯向量检索）；
`RUN B` 按它注释的建议改用 jieba 预分词（脚本代码仍不改，只是把喂进去的语料
按空格切成词；向量那一路仍用未分词的原文）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import sqlite3
import sys
import types
import time

import faiss
import jieba
import numpy as np

RAG = pathlib.Path("/Users/lh/Documents/工作学习/AI项目/RAG")
SCRIPT = pathlib.Path("/Users/lh/Desktop/python_混合检索评测/hybrid_rag_eval.py")
DB = RAG / "var/index/ekbs.db"
SHARD_FILE = RAG / "var/index/kb_all_L1.faiss"
OUT_JSON = RAG / "docs/eval/hybrid_eval_desktop_script.json"

sys.path.insert(0, str(RAG))
from ekbs.config import Secrets, load_settings  # noqa: E402
from ekbs.ingest.chunker import retrieval_text  # noqa: E402
from ekbs.provider.registry import build_embedding  # noqa: E402


# ---------------------------------------------------------------- 语料与向量


def load_corpus() -> tuple[list[int], list[str], np.ndarray]:
    """返回 (chunk_ids, 检索文本, 向量矩阵)。文本口径与生产一致 = retrieval_text。"""
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT chunk_id, heading_path, content, shard_id FROM ekbs_chunk "
        "ORDER BY chunk_id"
    ).fetchall()
    ids: list[int] = []
    texts: list[str] = []
    keep: list[int] = []
    index = faiss.read_index(str(SHARD_FILE))
    labels = faiss.vector_to_array(index.id_map)
    by_id = {int(lab): i for i, lab in enumerate(labels)}
    for row in rows:
        cid = int(row["chunk_id"])
        if cid not in by_id:  # 该块没有向量（例如父容器按设计不入向量）
            continue
        ids.append(cid)
        texts.append(retrieval_text(row["heading_path"], str(row["content"] or "")))
        keep.append(by_id[cid])
    # IndexIDMap2 的 reconstruct_batch 按「映射 id」查会报 key not found；
    # 底层索引按插入序存向量，与 id_map 的顺序一一对应 → 一次性全取再按位置挑。
    all_vectors = index.index.reconstruct_n(0, index.ntotal)
    del by_id  # 位置映射由 keep 承担
    vectors = np.asarray([all_vectors[i] for i in keep], dtype="float32")
    return ids, texts, vectors


def load_golden(name: str) -> list[dict]:
    data = json.loads((RAG / "docs/eval" / name).read_text(encoding="utf-8"))
    return data["cases"] if isinstance(data, dict) and "cases" in data else data


def relevant_indices(cases: list[dict], ids: list[int], texts: list[str]) -> tuple[list[dict], list[str]]:
    """把金标题集翻译成脚本要的 ``relevant_doc_indices``（按 must_contain 片段定位块）。"""
    con = sqlite3.connect(DB)
    pos_to_id = {cid: i for i, cid in enumerate(ids)}
    out: list[dict] = []
    unresolved: list[str] = []
    for case in cases:
        frag = case.get("must_contain")
        frags = [frag] if isinstance(frag, str) else list(frag or [])
        hits: list[int] = []
        for f in frags:
            if not str(f).strip():
                continue
            # 片段可能只出现在 **heading_path** 里（标题路径注入正是为此）——
            # 实测 `course-rag-multimodal` 的「RAG多模态数据处理」只在标题路径上。
            matched = [
                int(r[0])
                for r in con.execute(
                    "SELECT chunk_id FROM ekbs_chunk "
                    "WHERE content LIKE ? OR heading_path LIKE ?",
                    (f"%{f}%", f"%{f}%"),
                )
            ]
            hits.extend(pos_to_id[c] for c in matched if c in pos_to_id)
        if not hits and case.get("must_hit_doc"):
            # 文档级期望：相关集 = 该文档的全部可检索块（金标只要求"命中对的文档"）
            doc_title = str(case["must_hit_doc"])
            matched = [
                int(r[0])
                for r in con.execute(
                    "SELECT c.chunk_id FROM ekbs_chunk c JOIN ekbs_document d "
                    "ON d.doc_id = c.doc_id WHERE d.title LIKE ?",
                    (f"%{doc_title}%",),
                )
            ]
            hits.extend(pos_to_id[c] for c in matched if c in pos_to_id)
        hits = sorted(set(hits))
        if not hits:
            unresolved.append(case["id"])
            continue
        out.append({"id": case["id"], "query": case["question"], "relevant_doc_indices": hits})
    return out, unresolved


# ---------------------------------------------------------------- 替身模型


class PrecomputedEncoder:
    """鸭子类型替代 ``SentenceTransformer``：语料用库里已有向量，查询用生产模型现算。"""

    def __init__(self, corpus_texts: list[str], corpus_vectors: np.ndarray,
                 query_vectors: dict[str, np.ndarray]) -> None:
        self.corpus_texts = corpus_texts
        self.corpus_vectors = corpus_vectors
        self.query_vectors = query_vectors

    def encode(self, texts, normalize_embeddings: bool = True):
        if isinstance(texts, str):
            texts = [texts]
        texts = list(texts)
        if len(texts) == len(self.corpus_texts) and texts[:1] == self.corpus_texts[:1]:
            return self.corpus_vectors
        missing = [t for t in texts if t not in self.query_vectors]
        if missing:
            raise KeyError(f"替身没有这些查询的向量：{missing[:2]}")
        return np.asarray([self.query_vectors[t] for t in texts], dtype="float32")


class _EncoderHolder:
    """当前生效的替身编码器。脚本模块 import 一次就绑定了类名，所以用持有者以便两轮切换。"""

    encoder: "PrecomputedEncoder | None" = None


def install_stub() -> None:
    """注入 ``sentence_transformers`` 模块，让桌面脚本原样 import 成功。"""
    module = types.ModuleType("sentence_transformers")

    class SentenceTransformer:  # noqa: N801 —— 与真实库同名
        def __init__(self, model_name: str = "", *args, **kwargs) -> None:
            self.model_name = model_name

        def encode(self, texts, normalize_embeddings: bool = True):
            encoder = _EncoderHolder.encoder
            if encoder is None:
                raise RuntimeError("替身编码器未就绪")
            return encoder.encode(texts, normalize_embeddings)

    module.SentenceTransformer = SentenceTransformer
    sys.modules["sentence_transformers"] = module


def load_desktop_script():
    spec = importlib.util.spec_from_file_location("hybrid_rag_eval_desktop", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # 原样执行桌面脚本（__main__ 块不会跑）
    return module


# ---------------------------------------------------------------- 主流程


def main() -> int:
    queries_seen: dict[str, np.ndarray] = {}
    settings = load_settings()
    secrets = Secrets()  # 只读 os.environ（由 ~/.ekbs/secrets.env 注入）
    provider = build_embedding(settings, secrets)

    ids, corpus, vectors = load_corpus()
    original = load_golden("golden_corpus16.json")
    paraphrase = load_golden("golden_corpus16_paraphrase.json")
    negative = load_golden("golden_corpus16_negative.json")
    positives, unresolved = relevant_indices([*original, *paraphrase], ids, corpus)
    print(f"语料 {len(corpus)} 个可检索块；正例 {len(positives)}/{len(original)+len(paraphrase)}"
          f"（{len(unresolved)} 条金标片段在语料里定位不到）")
    if unresolved:
        print("  定位不到的用例:", unresolved)

    all_queries = [c["query"] for c in positives] + [c["question"] for c in negative]
    started = time.perf_counter()
    embedded = asyncio.run(provider.embed(all_queries))
    for text, vec in zip(all_queries, embedded):
        arr = np.asarray(vec, dtype="float32")
        queries_seen[text] = arr / max(float(np.linalg.norm(arr)), 1e-12)
    print(f"查询向量 {len(embedded)} 条，用时 {time.perf_counter() - started:.1f}s")

    install_stub()
    script = load_desktop_script()
    plain_query_vec = dict(zip(all_queries, (queries_seen[q] for q in all_queries)))
    seg_query_vec = {
        " ".join(jieba.cut(q)): queries_seen[q] for q in all_queries
    }
    rel_sizes = sorted(len(c["relevant_doc_indices"]) for c in positives)
    print(f"相关集大小分布：min={rel_sizes[0]} 中位={rel_sizes[len(rel_sizes)//2]} "
          f"max={rel_sizes[-1]}（>5 的占 {sum(1 for n in rel_sizes if n > 5)}/{len(rel_sizes)}）")

    results: dict = {
        "corpus_chunks": len(corpus),
        "positives": len(positives),
        "unresolved_cases": unresolved,
        "relevant_set_sizes": {
            "min": rel_sizes[0], "median": rel_sizes[len(rel_sizes) // 2], "max": rel_sizes[-1],
            "gt_k": sum(1 for n in rel_sizes if n > 5),
        },
        "runs": {},
    }
    alpha_grid = [0.1, 0.3, 0.5, 0.7, 0.9]

    for run_name, seg in (("A_as_is", False), ("B_jieba", True)):
        if seg:
            corpus_in = [" ".join(jieba.cut(t)) for t in corpus]
            cases_in = [{**c, "query": " ".join(jieba.cut(c["query"]))} for c in positives]
            query_map = seg_query_vec
        else:
            corpus_in, cases_in, query_map = corpus, positives, plain_query_vec
        _EncoderHolder.encoder = PrecomputedEncoder(corpus_in, vectors, query_map)
        evaluator = script.HybridRetrieverEvaluator(corpus_in)

        sweep, hit_at, per_case = {}, {}, {}
        for alpha in alpha_grid:
            m = evaluator.evaluate_recall_at_k(cases_in, k=5, alpha=alpha)
            sweep[str(alpha)] = {
                "recall_at_5": round(m["recall_at_k"], 4),
                "avg_latency_s": round(m["avg_latency_per_query"], 4),
                "queries": m["total_queries"],
            }
            print(f"  [{run_name}] alpha={alpha}: Recall@5={m['recall_at_k']:.4f} "
                  f"延迟={m['avg_latency_per_query']:.3f}s")
        # 逐题取 rank 最大的那次检索（k=10）算 best_rank —— 脚本本身不给 rank，
        # 但 MRR 需要它，而 MRR 是"rerank 有没有用"的关键对照指标（我加的，标注清楚）
        best_rank: dict[str, int | None] = {}
        for case in cases_in:
            res = evaluator.search(case["query"], alpha=0.5, k=10)
            rel = set(case["relevant_doc_indices"])
            rank = next((pos for pos, (idx, _) in enumerate(res, 1) if idx in rel), None)
            best_rank[case["id"]] = rank
        for k in (1, 3, 5, 10):
            hits = 0
            for case in cases_in:
                res = evaluator.search(case["query"], alpha=0.5, k=k)
                got = {i for i, _ in res}
                ok = bool(got & set(case["relevant_doc_indices"]))
                hits += ok
                per_case.setdefault(case["id"], {})[f"hit@{k}"] = ok
                per_case[case["id"]]["best_rank"] = best_rank[case["id"]]
            hit_at[k] = round(hits / len(cases_in), 4) if cases_in else 0.0
        print(f"  [{run_name}] 补充指标（我加的，脚本本身没有）："
              + "  ".join(f"Hit@{k}={v:.1%}" for k, v in hit_at.items()))
        neg_scores = []
        for case in negative:
            q = " ".join(jieba.cut(case["question"])) if seg else case["question"]
            top = evaluator.search(q, alpha=0.5, k=1)
            neg_scores.append(
                {"id": case["id"], "top1_score": round(float(top[0][1]), 4) if top else None}
            )
        mrr_extra = round(
            sum(1.0 / r for r in best_rank.values() if r) / len(best_rank), 4
        ) if best_rank else 0.0
        results["runs"][run_name] = {
            "alpha_sweep": sweep,
            "hit_at_k_alpha_0.5_extra": hit_at,
            "mrr_alpha_0.5_extra": mrr_extra,
            "per_case_hit_extra": per_case,
            "negative_top1_scores": neg_scores,
        }
        print(f"  [{run_name}] 补充 MRR（我加的）= {mrr_extra:.3f}")

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已写入 {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
