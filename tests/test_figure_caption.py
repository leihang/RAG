"""图片描述（`06` §4.11）：视觉模型给图写检索文本 + **内容哈希缓存**。

为什么必须有缓存：每张图一次视觉模型调用（**计费 + 图片出网**）。图片文件名本身就是
内容哈希（`img-<sha8>.png`），所以"重新解析一次 = 再花一遍钱"是纯粹可以避免的浪费 ——
用户会反复 re-parse（我自己就为了改分块口径 re-parse 了好几次）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ekbs.ingest.figure_caption import (
    CAPTION_CACHE_VERSION,
    CAPTION_PROMPT,
    caption_images,
    read_cached_caption,
    write_cached_caption,
)


class FakeVision:
    """假的视觉 provider：记录调用次数，按图片名回一句可断言的话。"""

    class _Ref:
        model = "fake-vl"

    def __init__(self) -> None:
        self.ref = self._Ref()
        self.calls: list[str] = []

    async def generate(self, messages):
        content = messages[0]["content"]
        data_url = content[1]["image_url"]["url"]
        self.calls.append(data_url[:24])
        return type("R", (), {"text": f"描述-{len(self.calls)}"})()


def _img(tmp_path: Path, name: str = "img-abc12345.png") -> Path:
    p = tmp_path / "images" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    return p


async def test_caption_generates_once_and_reuses_cache(tmp_path: Path) -> None:
    vision = FakeVision()
    image = _img(tmp_path)
    data_dir = tmp_path / "var"

    first = await caption_images(data_dir=data_dir, vision=vision, images=[(image.name, image)])
    assert first == {image.name: "描述-1"}
    assert len(vision.calls) == 1
    assert read_cached_caption(data_dir, image.name) == "描述-1"

    # 第二次（例如重新解析）：命中缓存，**不再调用**
    second = await caption_images(data_dir=data_dir, vision=vision, images=[(image.name, image)])
    assert second == {image.name: "描述-1"}
    assert len(vision.calls) == 1, "命中缓存却再次调用了视觉模型（会重复计费）"


async def test_caption_runs_with_bounded_concurrency(tmp_path: Path) -> None:
    """未缓存图应按 concurrency 并行，且不超过上限。"""
    import asyncio

    class SlowVision(FakeVision):
        def __init__(self) -> None:
            super().__init__()
            self.in_flight = 0
            self.max_in_flight = 0

        async def generate(self, messages):
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                await asyncio.sleep(0.05)
                return await super().generate(messages)
            finally:
                self.in_flight -= 1

    vision = SlowVision()
    paths = []
    for i in range(6):
        p = _img(tmp_path, f"img-{i:08d}.png")
        paths.append((p.name, p))

    got = await caption_images(
        data_dir=tmp_path / "var",
        vision=vision,
        images=paths,
        concurrency=3,
    )
    assert len(got) == 6
    assert len(vision.calls) == 6
    assert vision.max_in_flight <= 3
    assert vision.max_in_flight >= 2, "并发未生效（仍像串行）"


async def test_caption_failure_degrades_without_raising(tmp_path: Path) -> None:
    class Boom(FakeVision):
        async def generate(self, messages):
            raise RuntimeError("模型 503")

    vision = Boom()
    image = _img(tmp_path, "img-boom0001.png")
    got = await caption_images(data_dir=tmp_path / "var", vision=vision, images=[(image.name, image)])
    assert got == {}  # 交给调用方退回 Alt Text / 页标题
    assert read_cached_caption(tmp_path / "var", image.name) is None


async def test_caption_skips_unreadable_and_oversize(tmp_path: Path, monkeypatch) -> None:
    import ekbs.ingest.figure_caption as mod

    vision = FakeVision()
    missing = tmp_path / "images" / "img-gone0000.png"  # 不存在
    monkeypatch.setattr(mod, "MAX_IMAGE_BYTES", 1)
    big = _img(tmp_path, "img-big00000.png")

    got = await caption_images(
        data_dir=tmp_path / "var", vision=vision, images=[(missing.name, missing), (big.name, big)]
    )
    assert got == {}
    assert vision.calls == []


def test_cache_file_is_json_with_model_and_time(tmp_path: Path) -> None:
    data_dir = tmp_path / "var"
    write_cached_caption(data_dir, "img-x.png", "一句话描述", model="qwen-vl-plus")
    payload = json.loads((data_dir / "cache" / "figure-captions" / "img-x.json").read_text("utf-8"))
    assert payload["caption"] == "一句话描述"
    assert payload["model"] == "qwen-vl-plus"
    assert payload["prompt_version"] == CAPTION_CACHE_VERSION
    assert isinstance(payload["at"], int)


def test_corrupt_cache_is_ignored_not_fatal(tmp_path: Path) -> None:
    data_dir = tmp_path / "var"
    path = data_dir / "cache" / "figure-captions" / "img-bad.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    assert read_cached_caption(data_dir, "img-bad.png") is None


def test_prompt_asks_for_searchable_numbers_and_visible_text() -> None:
    """提示词必须同时要求：抄录图中文字 + 图表数字/趋势（截图类否则只剩空描述）。"""
    assert "抄录" in CAPTION_PROMPT or "可读文字" in CAPTION_PROMPT
    assert "数字" in CAPTION_PROMPT and "趋势" in CAPTION_PROMPT


def test_old_caption_cache_without_prompt_version_is_ignored(tmp_path: Path) -> None:
    data_dir = tmp_path / "var"
    write_cached_caption(data_dir, "img-old.png", "旧一句话", model="qwen-vl-plus")
    # 手工写成 v1 缓存（无 prompt_version）
    path = data_dir / "cache" / "figure-captions" / "img-old.json"
    path.write_text(
        json.dumps({"caption": "旧一句话", "model": "qwen-vl-plus", "at": 1}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert read_cached_caption(data_dir, "img-old.png") is None
    write_cached_caption(data_dir, "img-new.png", "新检索文本", model="qwen-vl-plus")
    assert read_cached_caption(data_dir, "img-new.png") == "新检索文本"
    payload = json.loads(
        (data_dir / "cache" / "figure-captions" / "img-new.json").read_text(encoding="utf-8")
    )
    assert payload["prompt_version"] == "v2-extract-text"
