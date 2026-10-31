"""可选的 Contextual Retrieval：入库时给每块补一句「本段在文档中的位置/主题」。

默认关闭（每块一次 LLM 调用，全量语料成本高）；开启后正文前缀 ``【上下文】…``，
FTS / 嵌入 / 精排吃的是同一份文本，不存在"只喂了其中一侧"的口径分裂。
"""

from __future__ import annotations

from ekbs.ingest.pipeline import IngestPipeline


def _pipeline(runtime) -> IngestPipeline:
    return IngestPipeline(
        db=runtime.db,
        settings=runtime.settings,
        providers=runtime.providers,
        shards=runtime.shards,
        fts=runtime.fts,
        figure_index=runtime.figure_index,
    )


def _contents(runtime, doc_id: int) -> list[str]:
    with runtime.db.sync_connect() as conn:
        return [
            str(row["content"])
            for row in conn.execute(
                "SELECT content FROM ekbs_chunk WHERE doc_id = ? ORDER BY seq", (doc_id,)
            )
        ]


async def _ingest(runtime, kb_id: int):
    return await _pipeline(runtime).ingest_markdown(
        title="普通制度",
        markdown="# 制度\n\n## 1. 目的\n\n正文内容说明。\n",
        kb_id=kb_id,
        level=1,
        visible_dept_ids=[],
        real_corpus=False,
    )


async def test_contextual_retrieval_off_by_default(seeded) -> None:
    runtime, summary = seeded

    result = await _ingest(runtime, int(summary["kb_id"]))

    contents = _contents(runtime, result.doc_id)
    assert contents
    assert not any(content.startswith("【上下文】") for content in contents)


async def test_contextual_retrieval_prefixes_chunks_when_enabled(seeded) -> None:
    runtime, summary = seeded
    runtime.settings.ingest = {"contextual_retrieval": True}

    result = await _ingest(runtime, int(summary["kb_id"]))

    contents = _contents(runtime, result.doc_id)
    assert contents
    assert all(content.startswith("【上下文】") for content in contents)
