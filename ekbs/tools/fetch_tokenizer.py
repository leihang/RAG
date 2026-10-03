"""取本地 token 计数的 Qwen 词表（`tokenizer.json`）—— **可选的固定词表路径**。

> 2026-10-03 起：默认口径已改用**官方 `dashscope` SDK 自带的 `qwen.tiktoken`**
> （离线、零下载、与 SDK 同版本；实测与这里的 HF 词表**计数逐条相同**）。
> 所以本脚本**不再是部署必需**，只在你想把口径固定到 HF 词表时使用：
> 跑一次本脚本，然后把 `EKBS_TOKENIZER_PATH` 指向 `var/models/qwen-tokenizer.json`。

## 为什么需要这个脚本

`02` TC-INDEX-004 要求本地 token 计数与云端 `usage.total_tokens` 偏差 ≤5%，而本地计数是
**代理**：代理的精度取决于"词表是否与线上嵌入模型同源"。线上嵌入模型是 Qwen 系
（`config/application.cloud.yaml` 的 `models.embed.model`），所以本地也要用 Qwen 词表 ——
用 OpenAI 的 `cl100k_base` 会系统性偏低（200 段真实文本实测 −35%）。

## 为什么是"下载"而不是"装依赖"

词表是一个约 11 MB 的**数据文件**，不是代码；放进 git 不合适，所以：

* 本脚本把它**下载到 `var/models/qwen-tokenizer.json`**（`var/` 已在 `.gitignore`）；
* `ekbs/ingest/chunker.count_tokens()` 启动时按
  `EKBS_TOKENIZER_PATH` → `<repo>/var/models/qwen-tokenizer.json` → `~/.ekbs/` 探测；
* **取不到不会报错**（回退 tiktoken 并告警）—— 分块链路不该被一个数据文件卡住，
  但 `/health/deps` 的 `token_counter` 字段会显式显示当前口径，避免"悄悄降级"。

用法：
    .venv/bin/python -m ekbs.tools.fetch_tokenizer            # 缺才下
    .venv/bin/python -m ekbs.tools.fetch_tokenizer --force    # 强制重下
    .venv/bin/python -m ekbs.tools.fetch_tokenizer --dest ~/.ekbs/qwen-tokenizer.json
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import httpx

#: Qwen 官方公开词表（与 `Qwen3-Embedding` 系列同源；本仓库实测三个 Qwen 候选文件内容一致）
DEFAULT_URL = "https://huggingface.co/Qwen/Qwen3-Embedding-0.6B/resolve/main/tokenizer.json"
DEFAULT_DEST = pathlib.Path(__file__).resolve().parents[2] / "var" / "models" / "qwen-tokenizer.json"


def fetch(url: str, dest: pathlib.Path, *, force: bool = False) -> int:
    if dest.is_file() and not force:
        print(f"已存在，跳过：{dest}（{dest.stat().st_size} 字节）　--force 可强制重下")
        return 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"下载 {url}\n  → {dest}")
    try:
        resp = httpx.get(url, timeout=300, follow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 下载失败：{exc}")
        print("   （若是网络受限，可手动把 Qwen 的 tokenizer.json 拷到该路径，或用 --dest 指定别处）")
        return 1
    dest.write_bytes(resp.content)
    # 立即自检：能加载且能编码，避免"下到一个坏文件"却到入库时才发现
    try:
        from tokenizers import Tokenizer

        tok = Tokenizer.from_file(str(dest))
        sample = tok.encode("为了保障工程部各班工作交接全面。", add_special_tokens=False).ids
        print(f"✅ 已写入 {dest}（{dest.stat().st_size} 字节）；自检：示例句 {len(sample)} token")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 文件已写入但加载失败：{exc}（请检查是否装 tokenizers，或文件是否完整）")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载 Qwen 词表供本地 token 计数使用")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--dest", type=pathlib.Path, default=DEFAULT_DEST)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    code = fetch(args.url, args.dest.expanduser(), force=args.force)
    if code == 0:
        # 装完顺手把当前口径打出来，省得再去查
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
        from ekbs.ingest.chunker import active_token_counter  # noqa: PLC0415

        print(f"当前生效口径：{active_token_counter()}")
    return code


if __name__ == "__main__":
    sys.exit(main())
