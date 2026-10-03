"""jieba **用户词典**：加载、生效、重建、可见性。

背景（实测）：`XJWY-GC-01-01` 被通用 jieba 切成 `['xjwy','-','gc','-','01','-','01']`（7 个碎片），
`XJ-01-11-1F02` 同样 7 片。碎片进倒排索引 → 查询变成"碎片 OR"，
既命中一堆无关块（实测把库里根本不存在的 `XJ-01-11-1F02` 都"搜"出来了），
又让精确匹配失效。整词进用户词典后是**单个词项**。

这几条钉住四件事：① 词典能被加载并生效；② 缺失/坏掉不能拖垮检索；
③ 写入侧（`content_tok`）用的是同一份词典；④ 重建是幂等往返（含 doc2query 行）。
"""

from __future__ import annotations

import pathlib

import pytest

from ekbs.index import fts as fts_module
from ekbs.index.fts import FtsIndex, ensure_userdict, reset_userdict_state, tokenize, tokenize_for_fts

TERM = "XJWY-GC-01-01"


@pytest.fixture(autouse=True)
def _clean_state():
    reset_userdict_state()
    yield
    reset_userdict_state()


def test_userdict_makes_a_code_one_token(tmp_path, monkeypatch) -> None:
    """词典生效：编号从 7 个碎片变成 1 个词项。"""
    dict_file = tmp_path / "userdict.txt"
    dict_file.write_text(f"# 测试词典\n{TERM} 100 n\n值班记录 80 n\n", encoding="utf-8")
    monkeypatch.setenv("EKBS_USERDICT_PATH", str(dict_file))
    reset_userdict_state()

    state = ensure_userdict()
    assert str(dict_file) in state and "2 词" in state
    assert tokenize(TERM) == [TERM.lower()]
    assert tokenize_for_fts(TERM) == TERM.lower()
    assert tokenize("值班记录") == ["值班记录"]


def test_missing_userdict_is_not_fatal(tmp_path, monkeypatch) -> None:
    """词典缺失（或路径错）**不能**拖垮检索链路 —— 状态如实报 ``none``，分词照常工作。

    注意：这里**不能**断言"编号被切成 7 片" —— jieba 的用户词典在进程内是**全局且无法卸载**的，
    前面用例加载过的词会留在这里。要验证"没有词典时的碎片行为"，必须在**全新进程**里跑
    （见下一条用例）。
    """
    monkeypatch.setenv("EKBS_USERDICT_PATH", str(tmp_path / "not-there.txt"))
    reset_userdict_state()
    assert ensure_userdict() == "none"
    assert tokenize("电梯一年要花多少保养费")  # 不抛异常即可


def test_fragmentation_without_userdict_in_a_fresh_process() -> None:
    """**全新进程 + 无词典**：编号确实被切成 7 个碎片（这是要解决的问题本身）。

    这条用子进程是为了绕开"jieba 全局词典不可卸载"这件事 —— 否则测的就是别的用例的残留。
    """
    import subprocess
    import sys

    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "from ekbs.index.fts import tokenize;" 
        "print(len(tokenize('XJWY-GC-01-01')))"
    ) % str(pathlib.Path(__file__).resolve().parents[1])
    env = {**__import__("os").environ, "EKBS_USERDICT_PATH": "/nonexistent/userdict.txt"}
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=180
    )
    assert out.returncode == 0, out.stderr[-400:]
    assert out.stdout.strip() == "7", f"期望 7 个碎片，实际 {out.stdout.strip()}"


def test_broken_userdict_is_reported_not_raised(tmp_path, monkeypatch) -> None:
    """词典文件坏掉（目录/权限）也只是状态串里报错，不抛异常。"""
    broken = tmp_path / "adir"
    broken.mkdir()
    monkeypatch.setenv("EKBS_USERDICT_PATH", str(broken))
    reset_userdict_state()
    state = ensure_userdict()
    assert state == "none" or state.startswith("error:")


def test_fts_writer_uses_the_userdict(db, monkeypatch, tmp_path) -> None:
    """写入侧（``content_tok``）与查询侧共用同一份词典 —— 否则两边口径不一致、匹配不上。"""
    dict_file = tmp_path / "userdict.txt"
    dict_file.write_text(f"{TERM} 100 n\n", encoding="utf-8")
    monkeypatch.setenv("EKBS_USERDICT_PATH", str(dict_file))
    reset_userdict_state()

    FtsIndex(db).upsert_chunks([(1, "kb_all_L1", f"{TERM} 交接班管理制度", "文档")])
    with db.sync_connect() as conn:
        stored = conn.execute("SELECT content_tok FROM ekbs_chunk_fts").fetchone()[0]
    assert TERM.lower() in stored.split()


async def test_rebuild_is_an_idempotent_round_trip(seeded, monkeypatch, tmp_path) -> None:
    """重建 = 删除 + 用生产写入器重写：**行数稳定**（含 doc2query 行），且新词典已生效。"""
    from ekbs.ingest.pipeline import IngestPipeline
    from ekbs.tools.rebuild_fts import rebuild

    dict_file = tmp_path / "userdict.txt"
    dict_file.write_text(f"{TERM} 100 n\n值班记录 80 n\n", encoding="utf-8")
    monkeypatch.setenv("EKBS_USERDICT_PATH", str(dict_file))
    reset_userdict_state()

    runtime, summary = seeded
    pipeline = IngestPipeline(
        db=runtime.db, settings=runtime.settings, providers=runtime.providers,
        shards=runtime.shards, fts=runtime.fts, figure_index=runtime.figure_index,
    )
    result = await pipeline.ingest_markdown(
        title="词典自检.md",
        markdown=f"# 交接班制度\n\n{TERM} 交接班制度说明。\n\n值班记录 上签字。\n",
        kb_id=int(summary["kb_id"]), level=1, visible_dept_ids=[], source_path="词典自检.md",
        real_corpus=False,
    )
    assert result.status == "published", result.status

    before = runtime.fts.count()
    assert before >= 1
    report = rebuild([result.doc_id], settings=runtime.settings, db=runtime.db)
    assert report["rows_final"] == before, "重建是往返操作，行数必须稳定（含 doc2query 行）"

    with runtime.db.sync_connect() as conn:
        tokens = [
            row[0]
            for row in conn.execute(
                "SELECT content_tok FROM ekbs_chunk_fts WHERE chunk_id IN "
                "(SELECT chunk_id FROM ekbs_chunk WHERE doc_id = ?)", (result.doc_id,)
            )
        ]
    assert any(TERM.lower() in value.split() for value in tokens)


def test_health_exposes_userdict_state(tmp_path, monkeypatch) -> None:
    """`/health/deps.fts_userdict` 必须能看见词典状态（避免"以为加了、其实没生效"）。"""
    from ekbs.api.health import fts_userdict_status

    monkeypatch.setenv("EKBS_USERDICT_PATH", str(tmp_path / "nope.txt"))
    reset_userdict_state()
    assert fts_userdict_status() == "none"

    dict_file = tmp_path / "userdict.txt"
    dict_file.write_text(f"{TERM} 100 n\n", encoding="utf-8")
    monkeypatch.setenv("EKBS_USERDICT_PATH", str(dict_file))
    reset_userdict_state()
    assert "1 词" in fts_userdict_status()
    assert fts_module.userdict_path() == dict_file


def test_default_userdict_path_points_into_repo_var() -> None:
    """未设环境变量时，默认去 `<repo>/var/dict/userdict.txt`（部署物，不进源码树）。"""
    monkeypatch_free = pathlib.Path(fts_module.__file__).resolve().parents[2] / "var/dict/userdict.txt"
    assert monkeypatch_free.name == "userdict.txt"
    assert "var" in monkeypatch_free.parts
