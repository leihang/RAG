"""TC-INDEX-004 校准：本地 token 计数 vs 云端 `usage.total_tokens`。

规格原文（`02` §4.3）：

    取 200 段真实文本：本地 `tiktoken` 计数 vs 云端 embedding 返回的 `usage.total_tokens`；
    两者偏差 ≤5%（超出则调整本地计数口径后重测）。**这条是所有尺寸判定的前置条件。**

## 为什么必须有这个工具

`token_count` 决定分块尺寸、质量指标（p50/p90）、成本估算，但它是**代理**：
代理精度取决于"本地词表是否与线上嵌入模型同源"。线上是 Qwen 系嵌入模型，若本地用
OpenAI 的 `cl100k_base`，200 段真实文本实测偏低 −35%（中文散文 +9%、数字表格行 −42%）。
换 Qwen 词表后收敛到 −11%。**换嵌入模型后必须重跑本工具**（锚点是 `ekbs_shard.embedding_fingerprint`）。

## 用法

    export EKBS_CONFIG=config/application.cloud.yaml   # 需要 DASHSCOPE_API_KEY
    .venv/bin/python -m ekbs.tools.token_calibration                    # 真实 chunk（默认）
    .venv/bin/python -m ekbs.tools.token_calibration --segments 200 --fail-over 5
    .venv/bin/python -m ekbs.tools.token_calibration --source files     # 语料不够时改抽源文件
    .venv/bin/python -m ekbs.tools.token_calibration --no-cloud         # 只看本地口径，不花钱

会出网、会计费（按批调用，200 段约 10 次请求，费用极低）。
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys

import httpx

#: 中文占比分桶：偏差与文本类型强相关（数字/表格行与中文散文能差 50 个百分点），
#: 只看合计会掩盖这一点，所以必须分组报告。
GROUP_CJK = 0.6
GROUP_LOW = 0.2
DEFAULT_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"


def _load_embed_model() -> str:
    from ekbs.config import load_settings

    return str(load_settings().models.embed.model)


def _current_fingerprint(settings) -> str:
    """当前嵌入模型指纹（校准必须绑定它：换模型旧系数自动失效）。"""
    try:
        from ekbs.provider.base import model_ref

        return model_ref(
            settings.models.embed.provider, settings.models.embed.model,
            dim=settings.models.embed.dim, version=settings.models.embed.version,
        ).fingerprint
    except Exception:  # noqa: BLE001
        return ""


def _cjk_ratio(text: str) -> float:
    return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff") / max(1, len(text))


def sample_from_chunks(limit: int) -> list[str]:
    """真实 chunk 正文（规格口径：200 段**真实文本**）。"""
    from ekbs.config import load_settings

    db = pathlib.Path(load_settings().data_dir) / "index" / "ekbs.db"
    con = sqlite3.connect(db)
    rows = [
        str(r[0])
        for r in con.execute(
            "SELECT content FROM ekbs_chunk WHERE TRIM(content) != '' ORDER BY chunk_id"
        )
    ]
    if len(rows) > limit:
        step = len(rows) / limit
        rows = [rows[int(i * step)] for i in range(limit)]
    return rows


def _sample_docx_paragraphs(base: pathlib.Path) -> list[str]:
    segs: list[str] = []
    for path in sorted(base.rglob("*.docx")):
        try:
            import docx

            for para in docx.Document(str(path)).paragraphs:
                if len(para.text.strip()) >= 20:
                    segs.append(para.text.strip())
        except Exception:  # noqa: BLE001
            continue
    return segs


def _sample_xlsx_rows(base: pathlib.Path) -> list[str]:
    segs: list[str] = []
    for path in sorted(base.rglob("*.xlsx")):
        try:
            from openpyxl import load_workbook

            wb = load_workbook(str(path), data_only=True)
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    line = " | ".join("" if c is None else str(c) for c in row)
                    if len(line.strip(" |")) >= 20:
                        segs.append(line.strip(" |"))
        except Exception:  # noqa: BLE001
            continue
    return segs


def _sample_pdf_paragraphs(base: pathlib.Path) -> list[str]:
    """用仓库内 mineru-venv 的 pypdfium2 抽 PDF 文本层；venv 缺失则跳过。"""
    pdfs = sorted(base.rglob("*.pdf"))
    if not pdfs:
        return []
    reader = (
        "import json,sys,pypdfium2 as pdfium;"
        "d=pdfium.PdfDocument(sys.argv[1]);"
        "print(json.dumps([d[i].get_textpage().get_text_range() for i in range(len(d))]))"
    )
    py = pathlib.Path(__file__).resolve().parents[2] / "var" / "mineru-venv/bin/python"
    segs: list[str] = []
    for path in pdfs[:3]:
        if not py.is_file():
            break
        out = subprocess.run(
            [str(py), "-c", reader, str(path)], capture_output=True, text=True, timeout=600
        )
        if out.returncode != 0:
            continue
        for page_text in json.loads(out.stdout or "[]"):
            for para in re.split(r"\n\s*\n", str(page_text)):
                if len(para.strip()) >= 20:
                    segs.append(para.strip())
    return segs


def sample_from_files(limit: int, source_dirs: list[pathlib.Path]) -> list[str]:
    """语料不足时的替代样本：从源文件抽段落（docx / xlsx / 有文本层 PDF）。"""
    segs: list[str] = []
    for base in source_dirs:
        if not base.is_dir():
            continue
        segs.extend(_sample_docx_paragraphs(base))
        segs.extend(_sample_xlsx_rows(base))
        segs.extend(_sample_pdf_paragraphs(base))
    segs = [s[:2000] for s in segs]
    if len(segs) > limit:
        step = len(segs) / limit
        segs = [segs[int(i * step)] for i in range(limit)]
    return segs


class QuotaExceeded(RuntimeError):
    """云端额度不足（不是口径问题，需要人去控制台处理）。"""


def cloud_total(texts: list[str], *, model: str, endpoint: str) -> int:
    resp = httpx.post(
        endpoint,
        headers={"Authorization": f"Bearer {os.environ['DASHSCOPE_API_KEY']}"},
        json={"model": model, "input": texts, "dimensions": 1024},
        timeout=180,
    )
    data = resp.json()
    if "usage" not in data:
        message = str((data.get("error") or {}).get("message") or data)[:220]
        # 配额用尽是最常见的一种（实测踩到：一次入库 18.8 万 token 就把日额度打满）。
        # 要点：**这不是代码/口径问题**，且它会同时让新文档的入库嵌入失败 —— 必须说清楚。
        if "quota" in message.lower() or "limit" in message.lower():
            raise QuotaExceeded(
                "云端额度不足，无法取真值：" + message
                + "\n   → 先去 DashScope 控制台确认套餐/额度；在恢复之前，"
                  "新文档的入库嵌入也会失败（校准记录会缺这一篇）"
            )
        raise RuntimeError(f"云端未返回 usage：{message}")
    return int(data["usage"]["total_tokens"])


def local_counters(with_calibration: bool = False):
    """返回 {口径名: 计数函数}。第一个是**当前生效口径**。

    ``with_calibration=True`` 时追加一列"校准后"（本地 × 云端真值聚合出的系数）——
    这是"准确度测试"要看的那一列：校准的意义就是把它的偏差压进阈值。
    """
    chunker = importlib.import_module("ekbs.ingest.chunker")
    counters: dict[str, object] = {}
    active = chunker.active_token_counter()
    counters[f"当前生效（{active.split(':')[0]}）"] = chunker.count_tokens
    if with_calibration:
        from ekbs.config import load_settings

        from ekbs.ingest import token_calib

        settings = load_settings()
        calibration = token_calib.load(
            settings.data_dir, _current_fingerprint(settings), chunker.active_token_counter()
        )
        if calibration:
            factor = calibration.factor
            counters[f"校准后（×{factor:.3f}）"] = (
                lambda t, _f=factor: int(round(chunker.count_tokens(t) * _f))
            )
    try:  # 对照项：老口径（帮助判断"换了词表改善多少"）
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        counters["tiktoken cl100k_base"] = lambda t: len(enc.encode(t))
    except Exception:  # noqa: BLE001
        pass
    try:  # 对照项：中文词法分词（证明"词≠token"）
        import jieba

        counters["jieba 词数"] = lambda t: len([w for w in jieba.lcut(t) if w.strip()])
    except Exception:  # noqa: BLE001
        pass
    return counters


def report(segs: list[str], counters, *, model: str, endpoint: str, batch: int,
           fail_over: float, use_cloud: bool) -> int:
    groups = {
        f"中文散文（CJK>{GROUP_CJK:.0%}）": [s for s in segs if _cjk_ratio(s) > GROUP_CJK],
        f"数字/表格行（CJK<{GROUP_LOW:.0%}）": [s for s in segs if _cjk_ratio(s) < GROUP_LOW],
        "混合（其余）": [s for s in segs if GROUP_LOW <= _cjk_ratio(s) <= GROUP_CJK],
        "**全部**": segs,
    }
    cloud_by_group: dict[str, int] = {}
    if use_cloud:
        for name, group in groups.items():
            total = 0
            for i in range(0, len(group), batch):
                total += cloud_total(group[i:i + batch], model=model, endpoint=endpoint)
            cloud_by_group[name] = total
    print(f"\n样本 {len(segs)} 段（模型 {model}）")
    header = f"  {'分组':<24}{'段数':>5}" + (f"{'云端':>8}" if use_cloud else "")
    for label in counters:
        header += f"{label:>22}"
    print(header)
    worst = 0.0
    active_ok = True
    for name, group in groups.items():
        if not group:
            continue
        line = f"  {name:<24}{len(group):>5}"
        if use_cloud:
            line += f"{cloud_by_group[name]:>8}"
        for label, fn in counters.items():
            value = sum(fn(s) for s in group)  # type: ignore[operator]
            if use_cloud and cloud_by_group[name]:
                dev = (value - cloud_by_group[name]) / cloud_by_group[name] * 100
                mark = "✅" if abs(dev) <= fail_over else "❌"
                line += f"{f'{dev:+.0f}% {mark}':>22}"
                if name == "**全部**" and label.startswith("当前生效"):
                    worst = dev
                    active_ok = abs(dev) <= fail_over
            else:
                line += f"{value:>22}"
        print(line)
    if not use_cloud:
        return 0
    print(
        f"\n  当前生效口径的合计偏差 {worst:+.1f}%（阈值 ±{fail_over:.0f}%）"
        f" → {'✅ 达标' if active_ok else '❌ 超标（先调整计数口径后重测，见 02 TC-INDEX-004）'}"
    )
    return 0 if active_ok else 1


def from_records(*, min_samples: int) -> int:
    """用入库时记下的云端真值聚合校准系数（不出网）。"""
    from ekbs.config import load_settings

    from ekbs.ingest import token_calib

    settings = load_settings()
    fingerprint = _current_fingerprint(settings)
    path = token_calib.records_path(settings.data_dir)
    if not path.is_file():
        print(f"还没有校准记录（{path}）—— 先导入至少一篇文档，入库时会自动记录")
        return 1
    entries = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    chunker = importlib.import_module("ekbs.ingest.chunker")   # 与 from_files 同样的晚绑定
    counter = chunker.active_token_counter()
    matched = [
        e for e in entries
        if (not fingerprint or e.get("fingerprint") == fingerprint)
        and (not e.get("counter") or e.get("counter") == counter)
    ]
    print(
        f"\n记录 {len(entries)} 条；匹配当前「模型指纹 + 计数口径」{len(matched)} 条"
        f"（fingerprint={fingerprint[:24]}…，counter={counter}）"
    )
    if len(matched) < len(entries):
        legacy = sum(1 for e in entries if not e.get("counter"))
        print(
            f"  说明：被排除 {len(entries) - len(matched)} 条"
            + (f"（其中 {legacy} 条是升级前写的、没有计数口径字段 —— 2026-10-03 起计入绑定）"
               if legacy else "（计数口径或模型指纹不符）")
            + "；入库 ≥3 篇后重跑本工具即可重建系数。"
        )
    print(f"  {'doc':>5}{'云端真值':>10}{'本地估算':>10}{'比例':>8}")
    for e in matched:
        print(f"  {e['doc_id']:>5}{e['cloud_tokens']:>10}{e['local_tokens']:>10}{e['ratio']:>8.3f}")
    calibration = token_calib.summarize(
        settings.data_dir, fingerprint, counter=counter, min_samples=min_samples
    )
    if calibration is None:
        print(f"\n样本不足 {min_samples} 条（或数据不全）→ 暂不写系数（避免单篇把全局口径带偏）")
        return 0
    written = token_calib.write_factor(settings.data_dir, calibration)
    print(
        f"\n  逐篇比例中位数 {calibration.median_ratio:.4f}（"
        f"加权系数 {calibration.factor:.4f} 会被最大那篇主导，两者都列出来供你判断）"
    )
    over = 0
    for e in matched:
        after = (e["local_tokens"] * calibration.factor - e["cloud_tokens"]) / e["cloud_tokens"] * 100
        over += abs(after) > 5
    print(f"  套用加权系数后：{len(matched) - over}/{len(matched)} 篇在 ±5% 内（{over} 篇仍超）")
    print(
        f"\n  聚合：云端 {calibration.cloud_tokens} / 本地 {calibration.local_tokens}"
        f" → 系数 {calibration.factor:.4f}（校准前偏差 {calibration.deviation_pct:+.1f}%）"
        f"\n  已写入 {written}（绑定指纹，换模型自动失效）"
    )
    print(
        "  复核方式：校准后偏差应≈0%（系数就是按它算的）；真正要看的是"
        "\n    python -m ekbs.tools.token_calibration --source files --segments 200"
        "  ← 用**独立样本**验证校准是否泛化"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TC-INDEX-004 本地 token 计数校准")
    ap.add_argument("--segments", type=int, default=200, help="抽样段数（规格：200）")
    ap.add_argument("--source", choices=("chunks", "files"), default="chunks",
                    help="chunks=真实分块正文（规格口径）；files=从源文件抽段落（语料不足时）")
    ap.add_argument("--dir", action="append", default=None, help="--source files 时扫描的目录")
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--fail-over", type=float, default=5.0, help="偏差阈值（默认 5%%）")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--no-cloud", action="store_true", help="只打印本地计数，不出网")
    ap.add_argument(
        "--from-records", action="store_true",
        help="不出网：直接用入库时记下的云端真值（token_calibration.jsonl）聚合系数并落盘",
    )
    args = ap.parse_args(argv)

    if args.from_records:
        return from_records(min_samples=args.segments if args.segments < 3 else 3)

    if args.source == "chunks":
        segs = sample_from_chunks(args.segments)
        if len(segs) < args.segments:
            print(f"⚠️ 语料只有 {len(segs)} 段（规格要 {args.segments} 段）—— 全用上，"
                  f"结论仅供参考；要更接近规格口径请先多导几篇文档，或改用 --source files")
    else:
        dirs = [pathlib.Path(d).expanduser() for d in (args.dir or ["~/Desktop"])]
        segs = sample_from_files(args.segments, dirs)
    if not segs:
        print("没有取到样本")
        return 1
    if not args.no_cloud and not os.environ.get("DASHSCOPE_API_KEY"):
        print("缺少 DASHSCOPE_API_KEY（或加 --no-cloud）")
        return 1
    try:
        return _report_entry(segs, args)
    except QuotaExceeded as exc:
        print(f"\n❌ {exc}")
        return 2


def _report_entry(segs, args) -> int:
    return report(
        segs, local_counters(with_calibration=not args.no_cloud),
        model=_load_embed_model() if not args.no_cloud else "n/a",
        endpoint=args.endpoint, batch=args.batch, fail_over=args.fail_over,
        use_cloud=not args.no_cloud,
    )


if __name__ == "__main__":
    sys.exit(main())
