"""给文档里的图片生成一句描述（`06` §4.11：图内文字/图意进索引）。

## 为什么需要它

图片抽出来、落盘、建 `figure` 块，都只解决了"存下来"；**图片块要有文字才进索引**
（`chunker.chunk_markdown` 的口径），否则搜什么都搜不到它 —— 实测用户那份 38 页 PPT
里 92 张图，只有 6 张有 Alt Text（还是 `5506745资源 1@2x` 这种素材名），其余 86 张
在界面上、在检索里都等于不存在。图片处理于是变成摆设。

纯 OCR（MinerU）解决不了：它对独立图片走版面分析流水线，回来的是**这张图自己的引用**
而不是文字（实测，见 README 第 66 条）。而视觉模型能读出**图意**：

* 图表 → 「2024–2026 用电量 5460941/5535105/5960922 度，逐年上升，公司承担部分先升后降」
* 现场照片 → 「德力西 DT862 型三相四线有功电能表，读数 10544.9 kWh」

## 两条硬约束

1. **计费 + 出网**：每张图一次视觉模型调用，且图片离开内网。所以默认关闭
   （`parse.figure_caption=false`），打开前还会过合规闸（`corpus_egress_ack`）。
2. **同文档重解析不重复计费**：描述按图片内容哈希缓存在
   ``<data_dir>/cache/figure-captions/``。**文档删除时缓存随文档一起清**
   （见 ``docs._purge_document_assets``）—— 图注是文档内容，不为跨文档省
   一点点 vision 费而残留。重解析同一篇未删除文档时仍可命中缓存。

失败一律降级：某张图调用失败就返回空，由调用方退回"Alt Text / 页面标题"那条路径，
不影响整篇入库。
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any, Sequence

from ekbs.logging import get_logger

log = get_logger(component="ingest.figure_caption")

#: 缓存版本：改提示词后必须 bump，否则旧描述会命中缓存、重解析白跑。
CAPTION_CACHE_VERSION = "v2-extract-text"

#: 提示词：检索文本优先「抄录图中可读文字」，图表再补数字/趋势。
#: v1 只要求「一句话描述」，截图类图会变成「展示四个网站页面…」而丢掉页面上的字。
CAPTION_PROMPT = (
    "这张图将用于全文检索。请用中文输出可检索文本，规则：\n"
    "1. 优先完整抄录图中所有可读文字（标题、正文、列表、按钮、表格单元格、水印、角标），"
    "按阅读顺序组织，保留关键专有名词与数字；\n"
    "2. 若是图表/示意图：在抄录可见标注之外，补充关键数字、单位、年份与趋势；\n"
    "3. 若是现场照片：写清设施/场景，并抄录可见的型号、读数、铭牌文字；\n"
    "4. 图中确实无可读文字时写「无可读文字」并加一句场景描述；不要编造看不清的内容；\n"
    "5. 只输出检索文本本身，不要客套话，不要用 Markdown。"
)

#: 单张图的图片体积上限（超过就不送）：避免把超大图整张塞进请求。
MAX_IMAGE_BYTES = 8 * 1024 * 1024


def caption_cache_dir(data_dir: Path) -> Path:
    """描述缓存目录（同文档重解析复用；文档删除时由删除链路清掉）。"""
    return Path(data_dir) / "cache" / "figure-captions"


def _cache_path(data_dir: Path, image_name: str) -> Path:
    """缓存文件名 = 图片文件名去掉扩展名（也就是内容哈希 `img-<sha8>`）。"""
    stem = Path(image_name).stem
    return caption_cache_dir(data_dir) / f"{stem}.json"


def read_cached_caption(data_dir: Path, image_name: str) -> str | None:
    path = _cache_path(data_dir, image_name)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # 缓存损坏就当没有，不要让它挡住入库
        return None
    # 旧版提示词产物一律作废（否则重解析仍复用「一句话描述」）
    if str(payload.get("prompt_version") or "") != CAPTION_CACHE_VERSION:
        return None
    text = str(payload.get("caption") or "").strip()
    return text or None


def write_cached_caption(data_dir: Path, image_name: str, caption: str, *, model: str) -> None:
    path = _cache_path(data_dir, image_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "caption": caption,
                "model": model,
                "prompt_version": CAPTION_CACHE_VERSION,
                "at": int(time.time()),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _image_data_url(image: Path) -> str | None:
    try:
        if image.stat().st_size > MAX_IMAGE_BYTES:
            return None
        raw = image.read_bytes()
    except OSError:
        return None
    suffix = image.suffix.lstrip(".").lower() or "png"
    mime = "image/jpeg" if suffix in {"jpg", "jpeg"} else f"image/{suffix}"
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


async def caption_images(
    *,
    data_dir: Path,
    vision: Any,
    images: Sequence[tuple[str, Path]],
    concurrency: int = 1,
) -> dict[str, str]:
    """给一批图片生成描述，返回 ``{图片名: 描述}``。

    ``vision`` 是 OpenAI 兼容的对话 provider（`Providers.vision`）。
    命中缓存的不再调用；失败的跳过（不抛异常）。
    ``concurrency`` 控制未缓存图的并发调用数（信号量；≤0 时按 1）。
    """
    out: dict[str, str] = {}
    model = str(getattr(getattr(vision, "ref", None), "model", "") or "vision")
    pending: list[tuple[str, Path]] = []
    for name, path in images:
        cached = read_cached_caption(data_dir, name)
        if cached:
            out[name] = cached
        else:
            pending.append((name, path))

    if not pending:
        if out:
            log.info(
                "figure_captions_done",
                generated=0,
                reused=len(out),
                total=len(images),
            )
        return out

    limit = max(1, int(concurrency or 1))
    sem = asyncio.Semaphore(limit)
    lock = asyncio.Lock()
    called = 0

    async def _one(name: str, path: Path) -> None:
        nonlocal called
        data_url = _image_data_url(path)
        if data_url is None:
            log.warning("figure_caption_skipped", image=name, reason="image_unreadable_or_too_large")
            return
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": CAPTION_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ]
        async with sem:
            try:
                result = await vision.generate(messages)
                text = str(getattr(result, "text", "") or "").strip()
            except Exception as exc:  # 单张失败不影响入库
                log.warning("figure_caption_failed", image=name, error=str(exc)[:120])
                return
        if not text:
            log.warning("figure_caption_empty", image=name)
            return
        async with lock:
            called += 1
            out[name] = text
        write_cached_caption(data_dir, name, text, model=model)

    await asyncio.gather(*(_one(name, path) for name, path in pending))
    if called or out:
        log.info(
            "figure_captions_done",
            generated=called,
            reused=len(out) - called,
            total=len(images),
        )
    return out
