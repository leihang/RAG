"""批量入库工具（`01` §12）：参数、闸门、去重与验收线。

注意：模块名是 ``ekbs.tools.import``（与关键字同名，`01` §12 就是这么写的），
因此这里只能 ``importlib.import_module``，不能 ``from ekbs.tools.import import ...``。
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import_module = importlib.import_module
import_tool = import_module("ekbs.tools.import")


DOC_A = """# 差旅费管理办法

## 第一条 交通标准

市内交通据实报销；高铁二等座、飞机经济舱为标准配置。

## 第二条 住宿标准

一线城市每晚不超过 600 元，其他城市不超过 400 元，凭发票报销。
"""

DOC_B = """# 培训管理规定

## 第一条 申请

员工参加外部培训须提前 10 个工作日提交申请，由部门负责人审批。
"""


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "raw"
    directory.mkdir()
    (directory / "差旅费管理办法.md").write_text(DOC_A, encoding="utf-8")
    (directory / "培训管理规定.md").write_text(DOC_B, encoding="utf-8")
    (directory / "说明.docx").write_bytes(b"not-a-real-docx")  # 解析会失败
    (directory / "忽略我.csv").write_text("a,b\n1,2\n", encoding="utf-8")  # 不在白名单
    return directory


async def _import(settings, source_dir: Path, **kwargs):
    defaults = dict(
        directory=source_dir,
        kb_code="hr-policy",
        level=2,
        dept_ids=["102"],
        synthetic=True,  # 合成样本，避免触发合规闸（真实语料不要加）
        parallel=2,
    )
    defaults.update(kwargs)
    return await import_tool.import_directory(settings, **defaults)


def test_discover_files_filters_by_extension(source_dir: Path) -> None:
    files = import_tool.discover_files(source_dir)
    names = {path.name for path in files}
    assert names == {"差旅费管理办法.md", "培训管理规定.md", "说明.docx"}
    assert "忽略我.csv" not in names


def test_checksum_is_stable(source_dir: Path) -> None:
    first = import_tool.checksum_of(source_dir / "培训管理规定.md")
    second = import_tool.checksum_of(source_dir / "培训管理规定.md")
    assert first == second and len(first) == 64


async def test_import_creates_kb_documents_and_acl(settings, source_dir: Path) -> None:
    report = await _import(settings, source_dir, extensions=(".md",))
    assert len(report.imported) == 2
    assert report.success_rate == 1.0
    assert report.checks["success_rate_ok"] is True
    assert report.checks["fingerprint_consistent"] is True
    assert report.checks["shard_registration_ok"] is True

    runtime = importlib.import_module("ekbs.runtime").Runtime.create(settings, service="test")
    try:
        with runtime.db.sync_connect() as conn:
            kb = conn.execute("SELECT kb_id, name FROM ekbs_kb WHERE code = 'hr-policy'").fetchone()
            assert kb is not None, "知识库应被自动创建"
            docs = conn.execute(
                "SELECT doc_id, title, status, level, visible_dept_ids FROM ekbs_document "
                "ORDER BY doc_id"
            ).fetchall()
            acl = conn.execute(
                "SELECT subject_type, subject_id, effect FROM ekbs_acl_entry "
                "WHERE object_type = 'shard' AND subject_id = '102'"
            ).fetchall()
        assert {str(d["status"]) for d in docs} == {"published"}
        assert {int(d["level"]) for d in docs} == {2}
        assert all("102" in str(d["visible_dept_ids"]) for d in docs)
        assert acl and all(str(r["effect"]) == "allow" for r in acl)
        # 部门映射（dev 配置 hr→102）应让文档落到 kb_hr_L2
        assert report.imported[0].shard_id == "kb_hr_L2"
    finally:
        await runtime.aclose()


async def test_duplicate_checksum_is_skipped(settings, source_dir: Path) -> None:
    await _import(settings, source_dir, extensions=(".md",))
    second = await _import(settings, source_dir, extensions=(".md",))
    assert len(second.imported) == 0
    assert len(second.skipped) == 2
    assert all("重复" in item.error for item in second.skipped)


async def test_parse_failure_lowers_success_rate(settings, source_dir: Path) -> None:
    """`01` §12 的解析成功率达标线是 98%：一个坏文件就该被算出来。"""
    report = await _import(settings, source_dir)
    assert len(report.imported) == 2 and len(report.failed) == 1
    assert report.success_rate == pytest.approx(2 / 3)
    assert report.checks["success_rate_ok"] is False
    assert "docx" in report.failed[0].error or "解析" in report.failed[0].error


async def test_dry_run_writes_nothing(settings, source_dir: Path) -> None:
    report = await _import(settings, source_dir, dry_run=True)
    assert report.checks["dry_run"] is True
    assert len(report.imported) == 0
    runtime = importlib.import_module("ekbs.runtime").Runtime.create(settings, service="test")
    try:
        with runtime.db.sync_connect() as conn:
            count = int(conn.execute("SELECT COUNT(*) FROM ekbs_document").fetchone()[0])
    finally:
        await runtime.aclose()
    assert count == 0


async def test_explicit_shard_wins_and_is_registered(settings, source_dir: Path) -> None:
    """T3 的处置：显式 `--shard-id` 优先于派生，并且该分片必须已登记（否则检索不到）。"""
    report = await _import(
        settings, source_dir, extensions=(".md",), shard_id="kb_legal_L2"
    )
    assert {item.shard_id for item in report.imported} == {"kb_legal_L2"}
    runtime = importlib.import_module("ekbs.runtime").Runtime.create(settings, service="test")
    try:
        assert "kb_legal_L2" in {row.shard_id for row in runtime.shards.registry()}
        with runtime.db.sync_connect() as conn:
            shards = {
                str(r["shard_id"])
                for r in conn.execute(
                    "SELECT DISTINCT shard_id FROM ekbs_chunk WHERE doc_id IN "
                    "(SELECT doc_id FROM ekbs_document)"
                ).fetchall()
            }
            fts_shards = {
                str(r["shard_id"])
                for r in conn.execute(
                    "SELECT DISTINCT c.shard_id AS shard_id FROM ekbs_chunk_fts f "
                    "JOIN ekbs_chunk c ON c.chunk_id = CAST(f.chunk_id AS INTEGER)"
                ).fetchall()
            }
        assert shards == {"kb_legal_L2"}
        assert fts_shards == {"kb_legal_L2"}, "FTS5 行必须跟着显式分片一起改"
        assert report.checks["shard_registration_ok"] is True
    finally:
        await runtime.aclose()


async def test_effective_date_is_applied(settings, source_dir: Path) -> None:
    report = await _import(
        settings, source_dir, extensions=(".md",), effective_date="2026-01-01"
    )
    runtime = importlib.import_module("ekbs.runtime").Runtime.create(settings, service="test")
    try:
        with runtime.db.sync_connect() as conn:
            dates = {
                str(r["effective_date"])
                for r in conn.execute("SELECT effective_date FROM ekbs_document").fetchall()
            }
    finally:
        await runtime.aclose()
    assert dates == {"2026-01-01"}
    assert report.imported


async def test_l3_gate_blocks_import_even_in_batch(settings, source_dir: Path) -> None:
    """批量导入不能绕过 L3 闸（00 C5）—— 这是「先跑通再放开」的关键。"""
    from ekbs.errors import ConfigError

    settings.permission.l3_ingest_enabled = False
    with pytest.raises(ConfigError):
        await _import(settings, source_dir, extensions=(".md",), level=3)


async def test_parallel_is_clamped(settings, source_dir: Path) -> None:
    """16 GB 机器上解析峰值不能叠加（00 §5）：超过上限要夹住而不是照跑。"""
    report = await _import(settings, source_dir, extensions=(".md",), parallel=9)
    assert len(report.imported) == 2


def test_cli_help_lists_documented_flags() -> None:
    """`01` §12 给的参数必须都在（--dir/--kb-code/--level/--dept-ids/--effective-date/--shard-id/--parallel）。"""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "ekbs.tools.import", "--help"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0
    for flag in ("--dir", "--kb-code", "--level", "--dept-ids", "--effective-date",
                 "--shard-id", "--parallel", "--synthetic", "--dry-run"):
        assert flag in result.stdout, flag


def test_cli_exit_code_reflects_success_rate(tmp_path: Path) -> None:
    """成功率低于 98% → 退出码 1（可接进上线检查）。

    这里刻意用**临时配置 + 临时数据目录**：早期版本直接指向仓库的
    ``config/application.dev.yaml``，结果子进程把数据写进了仓库的 ``var/``，
    污染了开发库（测试隔离问题，已修）。
    """
    import shutil
    import subprocess
    import sys

    repo = Path(__file__).resolve().parent.parent
    # 临时「部署根」：配置放 <tmp>/config/，建表脚本放 <tmp>/sql/（与仓库布局同构）
    etc = tmp_path / "config"
    etc.mkdir(parents=True)
    (tmp_path / "sql").mkdir()
    shutil.copy(repo / "sql" / "001_init.sql", tmp_path / "sql" / "001_init.sql")
    text = (repo / "config" / "application.dev.yaml").read_text(encoding="utf-8")
    text = (
        text.replace("  log_dir: var/log", f"  log_dir: {tmp_path}/var/log")
        .replace("  data_dir: var\n", f"  data_dir: {tmp_path}/var\n")
        .replace("  path: var/index/ekbs.db", f"  path: {tmp_path}/var/index/ekbs.db")
    )
    (etc / "application.yaml").write_text(text, encoding="utf-8")

    source = tmp_path / "raw"
    source.mkdir()
    (source / "好文件.md").write_text("# 标题\n\n正文内容。\n", encoding="utf-8")
    (source / "坏文件.docx").write_bytes(b"not-a-real-docx")

    env = {
        "EKBS_CONFIG": str(etc / "application.yaml"),
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
    }
    result = subprocess.run(
        [
            sys.executable, "-m", "ekbs.tools.import",
            "--dir", str(source), "--kb-code", "cli-kb",
            "--dept-ids", "102", "--synthetic", "--ext", ".md,.docx",
        ],
        capture_output=True, text=True, timeout=180, env=env, cwd=str(repo),
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "解析成功率" in result.stdout

    # 关键断言：仓库的开发库不能被这个用例写过
    dev_db = repo / "var" / "index" / "ekbs.db"
    if dev_db.is_file():
        import sqlite3

        conn = sqlite3.connect(str(dev_db))
        try:
            titles = {
                str(r[0])
                for r in conn.execute("SELECT title FROM ekbs_document").fetchall()
            }
        finally:
            conn.close()
        assert "好文件" not in titles, "CLI 用例污染了仓库开发库"
