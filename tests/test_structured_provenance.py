"""结构化结果出处：提取挂来源块；运算挂依据集 + op。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from ekbs.permission.context import PermContext
from ekbs.retrieval.pipeline import RetrievalPipeline, RetrievedChunk, RetrievalResult
from ekbs.retrieval.structured_query import StructuredQueryHit


def _perm() -> PermContext:
    return PermContext(
        user_id=1,
        dingtalk_userid="u1",
        clearance=2,
        dept_chain=("d1",),
        visible_shard_ids=("kb_all_L1",),
        visible_kb_ids=(1,),
        policy_version=1,
    )


def _chunk(cid: int, doc_id: int = 235) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=cid,
        doc_id=doc_id,
        shard_id="kb_all_L1",
        content=f"row-{cid}",
        heading_path="",
        page_from=1,
        page_to=1,
        chunk_type="table",
        doc_title="t",
    )


def test_bind_aggregate_labels_evidence_not_fake_top3() -> None:
    pipe = object.__new__(RetrievalPipeline)
    pipe._post_check_citations = MagicMock(
        side_effect=lambda answer, chunks, perm, indices=None: [
            SimpleNamespace(cite_index=i, chunk_id=c.chunk_id)
            for i, c in enumerate(chunks, start=1)
            if indices is None or i in set(indices)
        ]
    )
    chunks = [_chunk(10), _chunk(11), _chunk(99)]
    retrieval = RetrievalResult(
        query="q",
        chunks=chunks,
        refused=False,
        timings_ms={},
    )
    hit = StructuredQueryHit(
        value=100.0,
        surface="100",
        op="sum",
        chunk_ids=(11, 10),
        doc_ids=(235,),
        rows_touched=2,
    )
    text, citations, evidence = pipe._bind_structured_provenance(
        "合计 100", hit, retrieval, _perm()
    )
    assert "模块sum" in text.replace(" ", "")
    assert "依据 2 条" in text
    assert "[1][2]" in text
    assert [c.chunk_id for c in evidence] == [11, 10]
    assert 99 not in [c.chunk_id for c in evidence]
    assert pipe._post_check_citations.called


def test_extract_provenance_kind_and_payload() -> None:
    hit = StructuredQueryHit(
        value=1343.0,
        surface="1343",
        op="extract",
        chunk_ids=(10, 11),
        doc_ids=(235,),
        rows_touched=2,
        period="2026-09",
        metric="用电量",
        entity_key="公共",
        detail=[{"项目": "活动用电", "用量": 1343}],
        extras={"source": "rerank_table_chunks"},
    )
    assert hit.provenance_kind == "extract"
    prov = hit.provenance()
    assert prov["kind"] == "extract"
    assert prov["input_chunk_ids"] == [10, 11]
    assert prov["input_doc_ids"] == [235]
    payload = hit.as_dict()
    assert payload["provenance"]["kind"] == "extract"
    assert payload["quote"] == "1343"


def test_aggregate_provenance_is_evidence_set_not_single_chunk_quote() -> None:
    hit = StructuredQueryHit(
        value=274856.8,
        surface="274856.8",
        op="sum",
        chunk_ids=(10, 11, 12),
        doc_ids=(235,),
        rows_touched=11,
        period="2026-09",
        metric="用电量",
        extras={"source": "rerank_table_chunks+sum"},
    )
    assert hit.provenance_kind == "aggregate"
    prov = hit.provenance()
    assert prov["kind"] == "aggregate"
    assert prov["op"] == "sum"
    assert prov["rows_touched"] == 11
    assert prov["input_chunk_ids"] == [10, 11, 12]
    payload = hit.as_dict()
    assert "模块 sum" in payload["quote"]
    assert "11" in payload["quote"]


def test_module_yoy_diff_is_aggregate() -> None:
    hit = StructuredQueryHit(
        value=20.0,
        surface="20.00%",
        op="diff",
        chunk_ids=(1, 2),
        doc_ids=(9,),
        rows_touched=2,
        extras={
            "source": "module_yoy",
            "current": 120.0,
            "baseline": 100.0,
            "baseline_period": "2025-09",
        },
    )
    assert hit.provenance_kind == "aggregate"
    assert hit.provenance()["baseline_period"] == "2025-09"
