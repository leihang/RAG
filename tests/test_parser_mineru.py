"""MinerU 解析适配的**离线**契约测试（`01` §4 的 M1、`docs/spec/20` 的 T8）。

真跑 MinerU 要 2–3 GB 模型（首次约 2 分钟/页），不适合进测试；这里用一个**假的 mineru
可执行文件**产出与实测**同构**的产物，锁住三件事：

1. 产物文件名与目录层级（`<out>/<stem>/auto/<stem>_content_list.json`）—— 实测确认；
2. 字段映射：`page_idx` **0 基**要 +1、`text_level` → Markdown 标题、table/image/equation 各自的取法；
3. 三类失败路径都要**明确报错**，绝不静默返回“解析成功但内容为空”。
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from ekbs.ingest.parser import ParserUnavailable, parse_file

FAKE_MINERU = """#!/bin/sh
# 假 MinerU：只认 `-p <src> -o <out>`，按 <out>/<stem>/auto/ 落产物
while [ $# -gt 0 ]; do
  case "$1" in
    -p) SRC="$2"; shift 2 ;;
    -o) OUT="$2"; shift 2 ;;
    *) shift ;;
  esac
done
STEM="$(basename "$SRC")"; STEM="${STEM%.*}"
mkdir -p "$OUT/$STEM/auto"
cp "$FAKE_PAYLOAD" "$OUT/$STEM/auto/${STEM}_content_list.json"
printf '# 从 markdown 回落\\n' > "$OUT/$STEM/auto/${STEM}.md"
exit 0
"""

FAILING_MINERU = """#!/bin/sh
echo "boom: model download failed" >&2
exit 2
"""


def _fake_binary(tmp_path: Path, body: str, *, name: str = "mineru") -> Path:
    path = tmp_path / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _payload(tmp_path: Path, blocks: list[dict]) -> Path:
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps(blocks, ensure_ascii=False))
    return payload


def _blocks() -> list[dict]:
    """与实测产物同构：两页、含标题/正文/表格/图片/公式。"""
    return [
        {"type": "text", "text": "员工年假管理制度", "text_level": 1, "page_idx": 0},
        {"type": "text", "text": "第一条 年假天数：满 1 年 5 天，满 3 年 10 天。", "page_idx": 0},
        {
            "type": "table",
            "table_caption": ["表 1 年假天数"],
            "table_body": "<table><tr><td>司龄</td><td>天数</td></tr></table>",
            "page_idx": 1,
        },
        {"type": "image", "img_path": "images/abc.jpg", "img_caption": ["组织架构"], "page_idx": 1},
        {"type": "equation", "text": "E = mc^2", "page_idx": 1},
    ]


@pytest.fixture()
def fake_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def _install(blocks: list[dict] | None = None, *, body: str = FAKE_MINERU) -> Path:
        binary = _fake_binary(tmp_path, body)
        payload = _payload(tmp_path, blocks if blocks is not None else _blocks())
        monkeypatch.setenv("EKBS_MINERU_BIN", str(binary))
        monkeypatch.setenv("FAKE_PAYLOAD", str(payload))
        return binary

    return _install


def test_mineru_blocks_map_to_markdown_and_pages(tmp_path: Path, fake_env) -> None:
    """核心映射：`text_level`→标题、表格取 `table_body`、图取 `img_path`、`page_idx` 0 基 → 页码 +1。"""
    fake_env()
    source = tmp_path / "制度.pdf"
    source.write_bytes(b"%PDF-1.4 fake")

    parsed = parse_file(source)

    assert parsed.engine == "mineru"
    assert parsed.markdown.splitlines()[0] == "# 员工年假管理制度", "text_level=1 应转成一级标题"
    assert "第一条 年假天数" in parsed.markdown
    assert "<table>" in parsed.markdown, "表格应取 table_body"
    assert "表 1 年假天数" in parsed.markdown, "表格标题不能丢"
    assert "![组织架构](images/abc.jpg)" in parsed.markdown
    assert "$$ E = mc^2 $$" in parsed.markdown

    # page_idx 是 0 基：实测确认，必须 +1 才是给人看的页码
    assert parsed.page_count == 2
    pages = [item[1] for item in parsed.page_map]
    assert pages == [1, 2], f"页码映射应按页面首块位置生成：{parsed.page_map}"
    assert parsed.page_map[0][0] == 0, "第一页首块偏移应为 0"


def test_mineru_missing_binary_raises_with_actionable_hint(tmp_path: Path, monkeypatch) -> None:
    """没装 MinerU 必须**明确报错**并给出可执行的安装命令（含 extras 这个坑）。"""
    monkeypatch.setenv("EKBS_MINERU_BIN", str(tmp_path / "nope" / "mineru"))
    (tmp_path / "x.pdf").write_bytes(b"%PDF")
    with pytest.raises(ParserUnavailable) as excinfo:
        parse_file(tmp_path / "x.pdf")
    message = str(excinfo.value)
    assert "mineru[core]" in message, "必须提示带 extras，否则装完 CLI 直接崩（实测）"
    assert "modelscope" in message, "国内需换模型源（实测 HuggingFace 不可达）"


def test_mineru_is_found_next_to_interpreter(tmp_path: Path, monkeypatch) -> None:
    """**生产路径回归**：launchd 的 PATH 很窄，必须能从「与解释器同 venv」找到 mineru。

    实测背景：`/opt/ekbs/venv/bin/python -m ekbs.worker` 由 launchd 拉起时 PATH 只有
    `/usr/bin:/bin:/usr/sbin:/sbin`，`shutil.which("mineru")` 找不到 venv 里的可执行文件，
    会误报「MinerU 未安装」。
    """
    from ekbs.ingest.parser import _mineru_bin

    fake_python_dir = tmp_path / "venv" / "bin"
    fake_python_dir.mkdir(parents=True)
    binary = fake_python_dir / "mineru"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)

    monkeypatch.delenv("EKBS_MINERU_BIN", raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_python_dir / "python"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")   # 模拟 launchd 的窄 PATH
    assert _mineru_bin() == str(binary)

    # 显式指定优先于同 venv
    other = _fake_binary(tmp_path, "#!/bin/sh\nexit 0\n", name="mineru-other")
    monkeypatch.setenv("EKBS_MINERU_BIN", str(other))
    assert _mineru_bin() == str(other)


def test_mineru_nonzero_exit_is_not_silently_swallowed(tmp_path: Path, fake_env) -> None:
    fake_env(body=FAILING_MINERU)
    source = tmp_path / "报错.pdf"
    source.write_bytes(b"%PDF")
    with pytest.raises(ParserUnavailable) as excinfo:
        parse_file(source)
    assert "退出码 2" in str(excinfo.value)
    assert "boom" in str(excinfo.value), "应把子进程的错误输出带出来，便于排查"


def test_mineru_falls_back_to_markdown_when_content_list_missing(
    tmp_path: Path, fake_env, monkeypatch
) -> None:
    """产物缺 `_content_list.json` 时回落到 `.md`，但**不伪造页码**（page_map 留空）。"""
    fake_env(body=FAKE_MINERU.replace(
        'cp "$FAKE_PAYLOAD" "$OUT/$STEM/auto/${STEM}_content_list.json"', ":"
    ))
    source = tmp_path / "仅md.pdf"
    source.write_bytes(b"%PDF")
    parsed = parse_file(source)
    assert parsed.engine == "mineru"
    assert "从 markdown 回落" in parsed.markdown
    assert parsed.page_map == [], "拿不到页码时必须留空，不能编造"


def test_mineru_empty_output_raises(tmp_path: Path, fake_env) -> None:
    """既无 content_list 也无 .md → 必须报错，绝不能“成功但内容为空”。"""
    fake_env(body=FAKE_MINERU.replace(
        'cp "$FAKE_PAYLOAD" "$OUT/$STEM/auto/${STEM}_content_list.json"', ":"
    ).replace("printf '# 从 markdown 回落\\n' > \"$OUT/$STEM/auto/${STEM}.md\"", ":"))
    source = tmp_path / "空.pdf"
    source.write_bytes(b"%PDF")
    with pytest.raises(ParserUnavailable):
        parse_file(source)
