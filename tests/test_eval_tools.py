"""检索评测工具的口径测试（`eval_retrieval` / `eval_switch_sweep`）。

只测**纯逻辑**（打分与聚合），不联网、不建 runtime —— 评测工具的价值恰恰在于口径稳定，
口径错了整批数字都是假的。这里钉住三条最容易错的口径：

1. 命中判定（文档标题 / 原文片段 / 负例）；
2. **负例必须用 `top_score`**：拒答时 `chunks` 会被清空，用 `chunks[0].rerank_score`
   聚合会静默只统计"没被拒的那几条"（我第一版就是这么错的，8 题只算了 1 题，
   均值还因此恒定不变，看起来像开关完全无效）；
3. 开关变体的相对基线差值（正=该开关有用、负=该开关拖后腿）。
"""

from __future__ import annotations

from types import SimpleNamespace

from ekbs.tools.eval_retrieval import _match_rank, load_golden
from ekbs.tools.eval_switch_sweep import NegCase, VariantResult, default_variants


def _chunk(title: str, content: str) -> SimpleNamespace:
    return SimpleNamespace(doc_title=title, content=content, chunk_id=1, doc_id=1)


def test_match_rank_by_doc_and_fragment() -> None:
    chunks = [_chunk("员工手册", "年假 5 天"), _chunk("年假管理制度", "入职满 1 年享受 5 天")]
    rank, how = _match_rank(chunks, must_hit_doc="年假管理制度", must_contain="入职满 1 年", k=5)
    assert rank == 2 and how == "doc+contain"


def test_match_rank_doc_found_but_fragment_missing_is_a_miss() -> None:
    """文档对了、片段不在其中 → 必须判 MISS（不能算命中，否则评测会虚高）。"""
    chunks = [_chunk("员工手册", "年假 5 天"), _chunk("年假管理制度", "入职满 1 年享受 5 天")]
    rank, how = _match_rank(chunks, must_hit_doc="年假管理制度", must_contain="不存在的片段", k=5)
    assert rank is None and how == "doc_only_miss_contain"


def test_match_rank_fragment_only_ignores_doc_title() -> None:
    chunks = [_chunk("别的文档", "工程总价 26000.00 元")]
    rank, how = _match_rank(chunks, must_hit_doc=None, must_contain="26000.00", k=5)
    assert rank == 1 and how == "contain"


def test_negative_cases_use_top_score_not_chunks() -> None:
    """负例聚合必须覆盖**全部**负例，包括被拒答（chunks 被清空）的那些。"""
    result = VariantResult(name="baseline", note="")
    result.neg_cases = [
        NegCase(id="neg-a", question="q1", score=0.29, refused=True),
        NegCase(id="neg-b", question="q2", score=0.33, refused=True),
        NegCase(id="neg-c", question="q3", score=0.61, refused=False, top_chunk=2898),
    ]
    assert result.neg_refused == 2, "拒答数应覆盖全部负例"
    assert len(result.neg_scores) == 3, "分数应覆盖全部负例（含被拒的）"
    assert abs(result.neg_top1_mean - 0.41) < 1e-9
    assert result.neg_top1_max == 0.61
    leaked = result.neg_leaked
    assert [c.id for c in leaked] == ["neg-c"], "漏放的负例必须能被点名"


def test_negative_leak_is_what_matters_not_hit_at_k() -> None:
    """正例 Hit@k 对"把无关内容顶进 Top-k"完全不敏感 —— 所以负例要单独看。"""
    good = VariantResult(name="a", note="")
    good.neg_cases = [NegCase(id="n", question="q", score=0.30, refused=True)]
    bad = VariantResult(name="b", note="")
    bad.neg_cases = [NegCase(id="n", question="q", score=0.66, refused=False)]
    assert not good.neg_leaked and len(bad.neg_leaked) == 1


def test_default_variants_close_one_switch_at_a_time() -> None:
    """变体设计：一次只关一个（否则分不清是谁的贡献）。"""
    variants = {v.name: v for v in default_variants()}
    assert "baseline" in variants and "no_figure" in variants
    assert variants["no_figure"].overrides.figure_multimodal is False
    assert variants["no_multiquery"].overrides.multi_query == 1
    assert variants["dense_only"].overrides.hybrid_alpha == 1.0
    assert variants["sparse_only"].overrides.hybrid_alpha == 0.0
    # 加权融合的中间档也在表里：用同义改写题集量出"RRF 负优化"后才加的
    # （README §四 第 75 条），选型要在两套题上都看，免得只对一套过拟合。
    assert variants["alpha_85"].overrides.fusion == "alpha"
    assert variants["alpha_85"].overrides.hybrid_alpha == 0.85


def test_golden_files_are_wellformed_and_locatable() -> None:
    """题集本身要能被工具读入，且每条都有定位方式（否则「命中」无从判定）。"""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "docs" / "eval"
    golden = root / "golden_corpus16.json"
    if not golden.is_file():
        return  # 题集是运行期资产，缺失时跳过（不把测试绑死在这份语料上）
    cases = load_golden(golden)
    assert cases, "正例题集不能为空"
    for case in cases:
        assert case.get("question"), f"{case.get('id')} 缺 question"
        assert case.get("must_hit_doc") or case.get("must_contain"), (
            f"{case.get('id')} 既没给 must_hit_doc 也没给 must_contain，命中无从判定"
        )
    negatives = load_golden(root / "golden_corpus16_negative.json")
    assert negatives and all(c.get("question") for c in negatives)


def test_figure_index_reloads_external_changes(tmp_path) -> None:
    """图索引必须感知**其它进程**对文件的改动，否则会静默累积孤儿向量。

    实测事故（2026-10-02）：api 删文档把图向量摘掉并原子替换文件，worker 仍用启动时
    那份内存索引，随后入库时在陈旧副本上追加 → `figures.faiss` 里有 402 个向量、
    而库里只有 201 个图片块（201 个孤儿）。文本分片早有这套指纹机制，图索引当时没有。
    """
    import numpy as np

    from ekbs.index.figure_faiss import FigureIndex

    path = tmp_path / "figures.faiss"
    dim = 8

    writer = FigureIndex(path, dim)
    writer.upsert(np.eye(dim, dtype="float32")[:2], [1, 2])
    assert path.is_file()

    reader = FigureIndex(path, dim)          # 模拟另一个进程的内存副本
    assert reader.vector_count == 2

    writer.remove([1])                       # 外部进程摘掉一个
    assert reader.vector_count == 1, "应感知外部变更并重载，而不是继续用陈旧副本"

    # 追加也不能把外部删掉的 id 带回来
    reader.upsert(np.eye(dim, dtype="float32")[:1], [3])
    reloaded = FigureIndex(path, dim)
    assert reloaded.vector_count == 2
    assert sorted(label for label, _ in reloaded.search(np.eye(dim, dtype="float32")[0], 10)) == [2, 3]
