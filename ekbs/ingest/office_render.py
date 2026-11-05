"""原始 Office → PDF 渲染（跨平台，供复核页「原件」展示）。

浏览器不能原生渲染 xlsx/pptx/docx；Word 用 docx-preview、PDF/图片用原始字节。
Excel 的图表/样式无法靠 SheetJS 还原，这里用 LibreOffice headless 读**原始文件**
导出 PDF——只做格式转换，不在后端重画单元格。

生产要求：目标机安装 LibreOffice（Linux: ``libreoffice``/``soffice``；macOS:
``/Applications/LibreOffice.app/Contents/MacOS/soffice``）。可用 ``EKBS_SOFFICE_BIN``
指定绝对路径；没装时抛 ``OfficeRenderError``，API 返回 503，前端降级为下载原件。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from ekbs.logging import get_logger

log = get_logger(component="ingest.office_render")

_RENDER_LOCK = threading.Lock()
_TIMEOUT_SECONDS = 240


class OfficeRenderError(RuntimeError):
    """LibreOffice 不可用 / 转换失败。"""


def _libreoffice_binary() -> str | None:
    override = (os.getenv("EKBS_SOFFICE_BIN") or "").strip()
    if override:
        return override if Path(override).is_file() else None
    candidates = [
        shutil.which("soffice"),
        shutil.which("libreoffice"),
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "/usr/bin/soffice",
        "/usr/lib/libreoffice/program/soffice",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def _convert_to_pdf(source: Path, output_dir: Path, *, timeout: int = _TIMEOUT_SECONDS) -> Path:
    binary = _libreoffice_binary()
    if not binary:
        raise OfficeRenderError(
            "未安装 LibreOffice（或 EKBS_SOFFICE_BIN 未指向 soffice）；"
            "无法把原件渲染成 PDF"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    # 独立 UserInstallation：避免 service 进程并发时 LibreOffice profile 锁冲突
    profile_dir = Path(tempfile.mkdtemp(prefix="ekbs-lo-profile-"))
    command = [
        binary,
        "--headless",
        "--norestore",
        "--convert-to",
        "pdf",
        "--outdir",
        str(output_dir),
        f"-env:UserInstallation=file://{profile_dir}",
        str(source),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise OfficeRenderError(f"LibreOffice 转换失败：{exc}") from exc
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)
    candidate = output_dir / f"{source.stem}.pdf"
    if completed.returncode != 0 or not candidate.is_file():
        tail = ((completed.stderr or "") + (completed.stdout or "")).strip()[-500:]
        raise OfficeRenderError(f"LibreOffice 转换失败（exit {completed.returncode}）：{tail}")
    if candidate.read_bytes()[:5] != b"%PDF-":
        raise OfficeRenderError("LibreOffice 输出不是有效 PDF")
    return candidate


def render_original_pdf(source: Path, cache_dir: Path, *, doc_id: int) -> Path:
    """把原始文件渲染成缓存 PDF；源文件 mtime/size 变化时重建。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{doc_id}.pdf"
    stamp = cache_dir / f"{doc_id}.json"
    source_stat = source.stat()
    expected = {"mtime_ns": source_stat.st_mtime_ns, "size": source_stat.st_size}
    try:
        if target.is_file() and json.loads(stamp.read_text(encoding="utf-8")) == expected:
            return target
    except (OSError, ValueError):
        pass

    with _RENDER_LOCK:
        # 等锁后重查，避免并发重复转换
        try:
            if target.is_file() and json.loads(stamp.read_text(encoding="utf-8")) == expected:
                return target
        except (OSError, ValueError):
            pass
        with tempfile.TemporaryDirectory(prefix="ekbs-render-") as tmp:
            rendered = _convert_to_pdf(source, Path(tmp))
            tmp_target = target.with_suffix(".pdf.tmp")
            shutil.copyfile(rendered, tmp_target)
            tmp_target.replace(target)
        stamp.write_text(json.dumps(expected), encoding="utf-8")
    log.info("office_original_rendered", doc_id=doc_id, pdf=str(target))
    return target
