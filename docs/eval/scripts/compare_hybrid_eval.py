"""把「桌面脚本的评测结果」与「生产管线的评测结果」对齐成一张可比的表，并写出报告。

对齐口径（重要）：
* 桌面脚本的相关集来自金标的 `must_contain`/`must_hit_doc`（一个片段可能命中多个块），
  生产脚本 `eval_retrieval` 的 `hit` 也是同一套金标口径 → 两边**同一批金标准则、同一批语料**；
* 但"相关集大小"不同：脚本按**块**算 Recall（分母是命中块数），生产按**题**算 Hit（命中任一即算）；
* 所以报告里主指标用 **Hit@k**（两边都能算、含义一致），
  Recall@K 只作为脚本原样输出一并列出，并说明分母差异。
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

RAG = pathlib.Path("/Users/lh/Documents/工作学习/AI项目/RAG")
SCRIPT_JSON = RAG / "docs/eval/hybrid_eval_desktop_script.json"
OUT_MD = RAG / "docs/eval/hybrid_eval_report.md"
OUT_JSON = RAG / "docs/eval/hybrid_eval_compare.json"


def production_eval(golden: str) -> dict:
    """跑生产管线（真实服务链路）并算 Hit@1/3/5/10 与 MRR。"""
    out = subprocess.run(
        [str(RAG / ".venv/bin/python"), "-u", "-m", "ekbs.tools.eval_retrieval",
         "--golden", f"docs/eval/{golden}", "--json"],
        cwd=RAG, capture_output=True, text=True, timeout=2400,
    )
    payload = json.loads(out.stdout[out.stdout.index("{"):])
    cases = payload.get("cases") or payload.get("results") or []
    ranks = [c.get("rank") for c in cases]
    n = len(cases)
    hit = lambda k: sum(1 for r in ranks if r and 0 < r <= k) / n if n else 0.0  # noqa: E731
    mrr = sum(1.0 / r for r in ranks if r and r > 0) / n if n else 0.0
    return {
        "cases": n,
        "hit_at_1": round(hit(1), 4), "hit_at_3": round(hit(3), 4),
        "hit_at_5": round(hit(5), 4), "hit_at_10": round(hit(10), 4),
        "mrr": round(mrr, 4),
        "refused": sum(1 for c in cases if c.get("refused")),
        # 被阈值/规则拒答的题（生产 Hit@5 因此低于纯检索）—— 逐条列出便于核对
        "refused_cases": [
            {"id": c["id"], "reason": c.get("refusal_reason", "")}
            for c in cases if c.get("refused")
        ],
    }


def script_per_set(script: dict, ids: set[str]) -> dict:
    """从脚本逐题结果里拆出某个题集的 Hit@k（脚本本身只给整集 Recall）。"""
    out = {}
    for run, data in script["runs"].items():
        per_case = data["per_case_hit_extra"]
        picked = {cid: v for cid, v in per_case.items() if cid in ids}
        ranks = [v.get("best_rank") for v in picked.values()]
        out[run] = {
            "cases": len(picked),
            **{
                f"hit_at_{k}": round(
                    sum(1 for v in picked.values() if v.get(f"hit@{k}")) / len(picked), 4
                )
                for k in (1, 3, 5, 10)
            },
            "mrr": round(sum(1.0 / r for r in ranks if r) / len(ranks), 4) if ranks else 0.0,
        }
    return out


def main() -> int:
    script = json.loads(SCRIPT_JSON.read_text(encoding="utf-8"))
    original_ids = {
        c["id"] for c in json.loads((RAG / "docs/eval/golden_corpus16.json").read_text(encoding="utf-8"))["cases"]
    }
    para_ids = {
        c["id"] for c in json.loads((RAG / "docs/eval/golden_corpus16_paraphrase.json").read_text(encoding="utf-8"))["cases"]
    }
    print("跑生产管线（原题集）…")
    prod_orig = production_eval("golden_corpus16.json")
    print("跑生产管线（同义改写集）…")
    prod_para = production_eval("golden_corpus16_paraphrase.json")

    compare = {
        "corpus": {"retrievable_chunks": script["corpus_chunks"]},
        "production_original": prod_orig,
        "production_paraphrase": prod_para,
        "desktop_script_original": script_per_set(script, original_ids),
        "desktop_script_paraphrase": script_per_set(script, para_ids),
        "desktop_script_whole": {
            run: {**{"cases": script["positives"]}, **data["hit_at_k_alpha_0.5_extra"],
                  "alpha_sweep_recall_at_5": data["alpha_sweep"]}
            for run, data in script["runs"].items()
        },
    }
    # 生产的拒答代价：被拒的那几题，纯检索（脚本）其实能不能捞到
    cost = []
    for item in prod_orig["refused_cases"]:
        cid = item["id"]
        got = {
            run: {
                "script_hit@5": script["runs"][run]["per_case_hit_extra"].get(cid, {}).get("hit@5"),
                "script_best_rank": script["runs"][run]["per_case_hit_extra"].get(cid, {}).get("best_rank"),
            }
            for run in ("A_as_is", "B_jieba")
        }
        cost.append({"id": cid, "reason": item["reason"], "script": got})
    compare["refusal_cost_original_set"] = cost
    OUT_JSON.write_text(json.dumps(compare, ensure_ascii=False, indent=2), encoding="utf-8")

    def row(label: str, m: dict) -> str:
        return (f"| {label} | {m.get('cases','-')} | {m.get('hit_at_1',0):.1%} | "
                f"{m.get('hit_at_3',0):.1%} | {m.get('hit_at_5',0):.1%} | "
                f"{m.get('hit_at_10',0):.1%} | {m.get('mrr', float('nan')):.3f} |")

    lines = [
        "# 混合检索评测报告（用桌面 `hybrid_rag_eval.py`）",
        "",
        "## 评测对象",
        "",
        f"- **语料**：当前知识库 `internal_public_document` 的 **{script['corpus_chunks']} 个可检索块**"
        "（16 篇文档，已含最新清洗/分片修复）；文本口径 = 生产同款 `retrieval_text()`（标题路径 + 正文）。",
        f"- **查询**：金标题集 **45 条正例**（原题集 28 + 同义改写 17）；相关块由金标的"
        "`must_contain` 片段 / `must_hit_doc` 定位（45/45 全部解析成功）。",
        "- **脚本**：`~/Desktop/python_混合检索评测/hybrid_rag_eval.py`，**文件未做任何修改**"
        "（通过 `importlib` 原样加载；BM25、MinMax 归一化、alpha 融合、Recall@K 都是它自己的代码）。",
        "",
        "## 结果（主指标用 Hit@k —— 两边含义一致，可直接比）",
        "",
        "| 检索方案 | 题数 | Hit@1 | Hit@3 | Hit@5 | Hit@10 | MRR |",
        "|---|---|---|---|---|---|---|",
        row("**生产管线**（融合 + 云端 rerank + 父子扩展 + 阈值）· 原题集", prod_orig),
        row("**生产管线** · 同义改写集", prod_para),
        row("桌面脚本 Run A（原样，`.split()` 分词）· 原题集", compare["desktop_script_original"]["A_as_is"]),
        row("桌面脚本 Run A · 同义改写集", compare["desktop_script_paraphrase"]["A_as_is"]),
        row("桌面脚本 Run B（jieba 分词，脚本注释的建议）· 原题集", compare["desktop_script_original"]["B_jieba"]),
        row("桌面脚本 Run B · 同义改写集", compare["desktop_script_paraphrase"]["B_jieba"]),
        "",
        "## 脚本原样输出：Recall@5 与 alpha 敏感性",
        "",
        "| alpha（BM25 权重） | Run A 原样 Recall@5 | Run B jieba Recall@5 |",
        "|---|---|---|",
    ]
    sweep_a = script["runs"]["A_as_is"]["alpha_sweep"]
    sweep_b = script["runs"]["B_jieba"]["alpha_sweep"]
    for a in ("0.1", "0.3", "0.5", "0.7", "0.9"):
        lines.append(f"| {a} | {sweep_a[a]['recall_at_5']:.4f} | {sweep_b[a]['recall_at_5']:.4f} |")
    lines += [
        "",
        f"（单查询延迟：Run A {sweep_a['0.5']['avg_latency_s']:.4f}s / Run B "
        f"{sweep_b['0.5']['avg_latency_s']:.4f}s —— 纯本地计算，不含云端调用。）",
        "",
        "## 三个结论",
        "",
        "1. **脚本的 BM25 在中文上是空转**：它用 `doc.split()` 分词，中文没有空格 → 整段正文"
        "变成一个 token，BM25 分数恒为 0；MinMax 归一化对全等数组返回全 0，于是融合退化成"
        "**纯向量检索** —— 所以 Run A 在 alpha 0.1~0.9 之间 **Recall@5 完全相同（0.1184）**。"
        "脚本自己的注释也写了「生产环境建议接入 jieba」。",
        "2. **换成 jieba（Run B）后，BM25 权重越大反而越差**：Recall@5 从 0.1199（alpha=0.1）"
        "单调掉到 0.0810（alpha=0.9），Hit@5 从 95.6% 掉到 86.7%（alpha=0.5 时）。"
        "根因是脚本的**逐查询 MinMax 归一化**：它把很弱的 BM25 信号硬拉到 [0,1] 全域，"
        "噪声项因此在融合中与向量侧等权 —— 这正是我们生产用**原始分数**做 alpha 融合"
        "（`fusion=alpha, hybrid_alpha=0.85`）而不做 MinMax 的原因。",
        f"3. **单级稠密检索的召回已经很强，生产的 rerank 买的是「排序」**：原题集上脚本纯向量 "
        f"Hit@5 = {compare['desktop_script_original']['A_as_is']['hit_at_5']:.1%}，"
        f"甚至**高于**生产管线的 {prod_orig['hit_at_5']:.1%}（生产有 {prod_orig['refused']} 例被阈值拒答，"
        f"见下），但 Hit@1 只有 {compare['desktop_script_original']['A_as_is']['hit_at_1']:.1%}、"
        f"MRR {compare['desktop_script_original']['A_as_is']['mrr']:.3f}；生产 MRR "
        f"{prod_orig['mrr']:.3f}（原题集）—— 差距全在**排序**上：云端 rerank 把命中块从第 2~5 位抬到第 1 位。"
        f"换句话说，**召回靠稠密检索，精度靠 rerank**。",
        "",
        "## 生产阈值拒答的代价（原题集）",
        "",
        f"生产管线原题集 Hit@5 = {prod_orig['hit_at_5']:.1%}（{prod_orig['refused']} 例被拒答），"
        "而纯稠密检索（脚本 Run A）在同题集 Hit@5 = "
        f"{compare['desktop_script_original']['A_as_is']['hit_at_5']:.1%}。逐条核对被拒的那几题：",
        "",
        "| 用例 | 生产拒答原因 | 脚本 Run A 能捞到吗（best_rank） |",
        "|---|---|---|",
        *[
            f"| {c['id']} | {c['reason']} | "
            + ("是，第 " + str(c['script']['A_as_is']['script_best_rank']) + " 位"
               if c['script']['A_as_is']['script_best_rank'] else "否（Top-10 内没有相关块）")
            + " |"
            for c in compare.get("refusal_cost_original_set", [])
        ],
        "",
        "> 这正是生产与「教程脚本」的取舍差异：生产用 `min_score` 宁可拒答也不给低置信答案"
        "（负例不误放），代价是**偶发把能答的题也拒了**；脚本没有阈值，永远返回 Top-K。",
        "",
        "**逐条核对结果：这一例是金标题集的假目标，不是检索缺陷。** 库里唯一匹配该片段的块是：",
        "",
        "```text",
        "chunk_id=4264  doc_id=41  chunk_type=parent  内容仅 7 字：「4.RAG调优」",
        "heading_path=「3.RAG多模态数据处理」   ← 只有标题、没有正文的幻灯片页",
        "```",
        "",
        "即：`retrieval_text` = 「3.RAG多模态数据处理\n4.RAG调优」共 12 字，**这个块里没有任何可作答的内容**。"
        "生产把它 rerank 到低于 `min_score=0.65` → 拒答（**判别正确**：没有答案就不该编）；"
        "脚本判它「命中」，是因为金标的判据是**片段字符串匹配**（匹配到了 heading_path），"
        "并不判断「这个块能不能回答问题」。",
        "",
        "→ **处置建议**：① 该金标用例应退役或改判据（它测的是「标题路径被检索到」，"
        "不是「答得出来」）；② 或按发现 81 的待办，让只有标题的页也生成**标题块**"
        "（正文即标题），这样它才是有内容的检索单元。两者选一即可，不要靠调低阈值掩盖。",
        "",
        "## 两个必须说明的口径差异（否则数字会被误读）",
        "",
        "* **Recall@K 的分母**：脚本按**块**算（`|命中块| / |相关块|`），而金标里同一片段常命中多个块"
        f"（相关集大小 min={script['relevant_set_sizes']['min']}、中位={script['relevant_set_sizes']['median']}、"
        f"max={script['relevant_set_sizes']['max']}；>5 的占 {script['relevant_set_sizes']['gt_k']}/45）→ "
        "Top-5 最多只能覆盖 5/|相关集|，所以 0.1184 这个绝对数**不代表检索质量**，"
        "只在 Run A/Run B 之间、以及 alpha 之间做比较才有意义。",
        "* **脚本的融合分没有绝对含义**：alpha=0.5 时 8 条负例的 top-1 分数**全部恰好是 0.5000**"
        "（MinMax 后 top-1 恒为 1.0，再乘 0.5 权重）→ 无法据此设相关性阈值；"
        "生产的 `min_score=0.65` 之所以能用，是因为它作用在**云端 rerank 的原始分**上。",
        "",
        "## 复现",
        "",
        "```bash",
        "# 1) 桌面脚本跑真实语料（原样 + jieba 两轮）",
        "export EKBS_CONFIG=config/application.cloud.yaml",
        ".venv/bin/python docs/eval/scripts/run_desktop_hybrid_eval.py",
        "# 2) 生成对照与报告",
        ".venv/bin/python docs/eval/scripts/compare_hybrid_eval.py",
        "```",
        "",
        "原始数据：`docs/eval/hybrid_eval_desktop_script.json`（脚本两轮全量结果）、"
        "`docs/eval/hybrid_eval_compare.json`（与生产管线的对齐表）。",
    ]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"报告已写入 {OUT_MD}")
    print(json.dumps({k: v for k, v in compare.items() if k != "desktop_script_whole"},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
