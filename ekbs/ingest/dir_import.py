"""服务端目录导入：**扫描预览 + 登记待解析**（管理台「导入」页的后端）。

为什么不在网页里上传几百份文件
------------------------------
真实语料是「一个目录几百份制度 / 一个代码仓库几千个文件」，浏览器上传既不现实
（要过浏览器、要分片、要等），也不必要 —— **文件本来就已经在服务器上**。
所以这条路径只做两件事：扫描、登记；真正的解析与入库交给常驻的 ``ekbs-worker``
（文档置 ``status='pending'`` 即入队），于是进度、重跑、取消直接复用已有的
「解析任务」页，不必另造一套任务体系。

安全边界（**只有一条，必须守住**）
--------------------------------
网页端**只能**访问 ``<data_dir>/raw`` 及其子目录。用户传进来的相对路径一律
``resolve()`` 后校验是否落在 raw 根之内，符号链接也不例外 —— 否则这个接口等于
「把服务器上任意文件读进向量库」的能力开放给了任何有 ``doc:document:create``
的账号。中文目录名天然支持（路径按 UTF-8 处理，不做任何转码）。

与管理台 `import` 页、CLI ``ekbs.tools.import`` 的关系：

============================  ==========================  ============================
场景                          入口                        解析由谁做
============================  ==========================  ============================
服务器目录、批量               管理台「导入 → 目录导入」     worker（pending 队列）
服务器目录、批量（脚本/CI）     ``ekbs.tools.import``       CLI 进程内联（同步等待）
单个/少量文件                  管理台「导入 → 上传文件」     worker（pending 队列）
============================  ==========================  ============================

三者共用同一份扩展名白名单（:data:`ekbs.ingest.parser.SUFFIX_ENGINES`）与同一套去重口径
（内容 checksum），不会出现「CLI 能导、界面不能导」的两张皮。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ekbs.config import Settings
from ekbs.db import Database
from ekbs.errors import ConfigError
from ekbs.ingest.parser import SUFFIX_ENGINES, normalize_suffix
from ekbs.logging import get_logger

log = get_logger(component="ingest.dir_import")

#: 扫描预览里最多回多少个文件（避免一个几万文件的仓库把响应撑爆；计数仍是全量）
PREVIEW_LIMIT = 200

#: 一次登记的上限。超过就分批来 —— 16 GB 机器上解析串行，几千份排队只会压住队列，
#: 让操作者看不出进度。
ENQUEUE_LIMIT = 2000


class ImportPathError(ValueError):
    """路径不在 raw 根之内（含符号链接逃逸）。"""


def raw_root(settings: Settings) -> Path:
    """允许导入的根目录：``<data_dir>/raw``（生产为 ``/data/ekbs/raw``）。"""
    return settings.data_paths()["raw"]


def resolve_within_raw(settings: Settings, rel: str | Path = "") -> Path:
    """把用户给的相对路径解析成 raw 下的真实路径；越界一律拒绝。

    三种越界都要挡住：``..`` 穿越、绝对路径、**软链接指向 raw 之外**。
    ``Path.resolve()`` 会把符号链接一并展开，所以最后比较真实前缀即可。
    """
    root = raw_root(settings).resolve()
    candidate = Path(rel)
    if candidate.is_absolute():
        raise ImportPathError("只接受 raw 目录下的相对路径（不支持绝对路径）")
    target = (root / candidate).resolve()
    if target != root and root not in target.parents:
        raise ImportPathError("路径超出 raw 目录范围（不允许 .. 或指向外部的软链接）")
    return target


def assert_path_within_raw(settings: Settings, path: str | Path) -> Path:
    """断言 ``path`` 落在 raw 根之内，返回 resolve 后的绝对路径。

    与 :func:`resolve_within_raw` 的区别：这里**同时接受**相对路径与已经落在
    raw 下的绝对路径（上传/complete、CLI 登记常用绝对路径）。任何逃逸
    （``..``、raw 外绝对路径、指向外部的软链接）一律拒绝。

    用途：``POST /docs/documents`` 的 ``source_path``、以及任何把调用方路径
    交给 worker ``parse_file`` 的入口——否则等于开放「读任意本机文件进 KB」。
    """
    root = raw_root(settings).resolve()
    root.mkdir(parents=True, exist_ok=True)
    candidate = Path(path)
    if candidate.is_absolute():
        target = candidate.resolve()
    else:
        target = (root / candidate).resolve()
    if target != root and root not in target.parents:
        raise ImportPathError("路径超出 raw 目录范围（不允许 .. 或指向外部的软链接）")
    return target


def to_rel(settings: Settings, path: Path) -> str:
    """把 raw 下的真实路径还原成网页端使用的相对路径（空串表示 raw 根）。"""
    root = raw_root(settings).resolve()
    resolved = path.resolve()
    if resolved == root:
        return ""
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:  # pragma: no cover - 由 resolve_within_raw 先挡住
        raise ImportPathError(f"路径不在 raw 目录内：{path}") from exc


@dataclass
class DirEntry:
    """目录浏览用的一条子目录。"""

    name: str
    rel_path: str
    has_children: bool
    #: 该目录（**含子目录**）里白名单内的文件数，便于挑目录时不至于导进空文件夹
    importable_files: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "rel_path": self.rel_path,
            "has_children": self.has_children,
            "importable_files": self.importable_files,
        }


#: 扫描结果里一个文件的归类
ACTION_NEW = "new"                 #: 可导入
ACTION_DUPLICATE = "duplicate"     #: 内容已在库（按 checksum 去重）
ACTION_TOO_LARGE = "too_large"     #: 超过 parse.max_file_mb
ACTION_UNSUPPORTED = "unsupported"  #: 后缀不在 SUFFIX_ENGINES（正常扫描不会出现，保险起见）


@dataclass
class ImportCandidate:
    rel_path: str
    size_bytes: int
    action: str = ACTION_NEW
    reason: str = ""
    checksum: str = ""
    doc_id: int | None = None

    @property
    def importable(self) -> bool:
        return self.action == ACTION_NEW

    def as_dict(self) -> dict[str, Any]:
        return {
            "rel_path": self.rel_path,
            "size_bytes": self.size_bytes,
            "action": self.action,
            "reason": self.reason,
            "doc_id": self.doc_id,
            "importable": self.importable,
        }


@dataclass
class ScanResult:
    rel_dir: str
    total_files: int = 0
    candidates: list[ImportCandidate] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    warn: list[str] = field(default_factory=list)

    @property
    def importable(self) -> int:
        return int(self.counts.get(ACTION_NEW, 0))

    def importable_paths(self) -> list[str]:
        """**全量**可导入的相对路径（不受预览截断影响）。

        「预览只回 200 条」是为了响应体积，但执行导入时必须拿全量 ——
        早先版本直接拿预览列表去导，于是几千个文件的目录会**静默只导前 200 个**，
        而界面显示"已完成"，这是最危险的一类 bug。
        """
        return [c.rel_path for c in self.candidates if c.importable]

    def as_dict(self, *, preview_limit: int = PREVIEW_LIMIT) -> dict[str, Any]:
        preview = self.candidates[:preview_limit]
        return {
            "rel_dir": self.rel_dir,
            "total_files": self.total_files,
            "importable": self.importable,
            "counts": dict(self.counts),
            "blockers": list(self.blockers),
            "warn": list(self.warn),
            "preview": [c.as_dict() for c in preview],
            "preview_truncated": len(self.candidates) > len(preview),
            "preview_limit": preview_limit,
            "extensions": len(SUFFIX_ENGINES),
        }


def _importable_extensions() -> set[str]:
    return {suffix.lower() for suffix in SUFFIX_ENGINES}


def discover_files(
    directory: Path,
    *,
    extensions: Sequence[str] | None = None,
    recursive: bool = True,
    limit: int | None = None,
) -> list[Path]:
    """按扩展名白名单列出目录下的文件（CLI 与管理台**共用**这一份发现逻辑）。

    默认白名单直接取 :data:`SUFFIX_ENGINES` —— 早先 CLI 自己维护一份、
    配置里又有另一份，结果 ``.pdf`` 只在其中一份里，不加 ``--ext .pdf`` 时
    PDF **一个都发现不了**，输出只有「发现文件 0」，连"跳过"都没有。
    """
    if not directory.is_dir():
        raise FileNotFoundError(f"目录不存在：{directory}")
    exts = {e.lower() for e in (extensions or SUFFIX_ENGINES)}
    pattern = "**/*" if recursive else "*"
    files = [
        path
        for path in sorted(directory.glob(pattern))
        if path.is_file() and path.suffix.lower() in exts
    ]
    return files[: int(limit)] if limit else files


def _dir_file_count(directory: Path, extensions: set[str]) -> int:
    try:
        return sum(
            1
            for path in directory.rglob("*")
            if path.is_file() and path.suffix.lower() in extensions
        )
    except OSError:  # pragma: no cover - 权限/坏软链接
        return 0


def list_children(settings: Settings, rel: str = "") -> dict[str, Any]:
    """列出 raw 下某目录的**子目录**（供界面逐层进入；不列文件，文件走扫描）。"""
    target = resolve_within_raw(settings, rel)
    if not target.exists():
        raise FileNotFoundError(f"目录不存在：{rel or 'raw'}")
    if not target.is_dir():
        raise NotADirectoryError(f"不是目录：{rel}")
    extensions = _importable_extensions()
    entries: list[DirEntry] = []
    for child in sorted(target.iterdir(), key=lambda p: p.name):
        if child.name.startswith("."):
            continue
        # 符号链接指向 raw 之外的一律不展示（展示即等于暗示可用）
        try:
            child.resolve().relative_to(raw_root(settings).resolve())
        except ValueError:
            continue
        if not child.is_dir():
            continue
        entries.append(
            DirEntry(
                name=child.name,
                rel_path=to_rel(settings, child),
                has_children=any(p.is_dir() for p in child.iterdir()),
                importable_files=_dir_file_count(child, extensions),
            )
        )
    return {
        "rel_path": to_rel(settings, target),
        "parent": to_rel(settings, target.parent) if target != raw_root(settings).resolve() else None,
        "dirs": [e.as_dict() for e in entries],
        "root": str(raw_root(settings)),
    }


def list_descendant_dirs(settings: Settings, rel: str = "") -> list[str]:
    """列出 ``rel`` 下**全部子孙目录**的相对路径（相对 ``rel`` 本身，不含 ``.``）。

    供上传页「保存到文件夹」下拉使用：一次拿齐，不必逐层点。
    """
    target = resolve_within_raw(settings, rel)
    if not target.exists():
        raise FileNotFoundError(f"目录不存在：{rel or 'raw'}")
    if not target.is_dir():
        raise NotADirectoryError(f"不是目录：{rel}")
    root = raw_root(settings).resolve()
    base = target.resolve()
    out: list[str] = []
    try:
        candidates = sorted(base.rglob("*"), key=lambda p: str(p).lower())
    except OSError:  # pragma: no cover
        return out
    for path in candidates:
        if path.name.startswith(".") or not path.is_dir():
            continue
        try:
            path.resolve().relative_to(root)
        except ValueError:
            continue
        rel_under = path.relative_to(base).as_posix()
        if rel_under and rel_under != ".":
            out.append(rel_under)
    return out


def scan_directory(
    settings: Settings,
    db: Database,
    *,
    rel_dir: str = "",
    level: int = 2,
    dept_ids: Sequence[str] = (),
    recursive: bool = True,
    limit: int | None = None,
    extensions: Sequence[str] | None = None,
) -> ScanResult:
    """扫描目录并给每个文件归类；**不写库**。

    闸门（合规 / L3）不在这里抛异常，而是放进 ``blockers`` —— 界面要能把
    「为什么现在不能导」显示清楚，而不是让用户看到一句 500 或者 200 条单文件失败。
    """
    target = resolve_within_raw(settings, rel_dir)
    if not target.is_dir():
        raise NotADirectoryError(f"不是目录：{rel_dir or 'raw'}")

    exts = {e.lower() for e in (extensions or SUFFIX_ENGINES)}
    files = discover_files(target, extensions=exts, recursive=recursive, limit=limit)

    result = ScanResult(rel_dir=to_rel(settings, target), total_files=len(files))
    for action in (ACTION_NEW, ACTION_DUPLICATE, ACTION_TOO_LARGE, ACTION_UNSUPPORTED):
        result.counts[action] = 0

    if not files:
        result.warn.append(
            "该目录下没有白名单内的文件（支持 "
            f"{len(SUFFIX_ENGINES)} 种后缀，含 51 种代码后缀）"
        )

    max_mb = float(getattr(settings.parse, "max_file_mb", 0) or 0)
    seen_in_batch: dict[str, str] = {}
    for path in files:
        size = path.stat().st_size
        candidate = ImportCandidate(rel_path=to_rel(settings, path), size_bytes=size)
        if max_mb and size > max_mb * 1024 * 1024:
            candidate.action = ACTION_TOO_LARGE
            candidate.reason = f"超过 parse.max_file_mb={max_mb:g} MB"
        else:
            checksum = checksum_of(path)
            candidate.checksum = checksum
            if checksum in seen_in_batch:
                candidate.action = ACTION_DUPLICATE
                candidate.reason = f"与本批 {seen_in_batch[checksum]} 内容相同"
            else:
                seen_in_batch[checksum] = candidate.rel_path
                existing = existing_doc_by_checksum(db, checksum)
                if existing is not None:
                    candidate.action = ACTION_DUPLICATE
                    candidate.doc_id = existing
                    candidate.reason = f"内容已在库（doc_id={existing}）"
        result.counts[candidate.action] += 1
        result.candidates.append(candidate)

    result.blockers = check_blockers(settings, level=level, dept_ids=dept_ids)
    log.info(
        "dir_scan_done",
        rel_dir=result.rel_dir,
        total=result.total_files,
        importable=result.importable,
        blockers=len(result.blockers),
    )
    return result


def check_blockers(
    settings: Settings, *, level: int, dept_ids: Sequence[str] = ()
) -> list[str]:
    """把「现在不能导入」的原因逐条列出来（与 CLI 的闸门同一套判据）。

    为什么列而不抛：合规闸未开时，操作者最需要知道的是**该去哪里开**，
    而不是一句 ``ConfigError``。CLI 是整批快速失败，界面则应当把原因摆在按钮旁边。
    """
    blockers: list[str] = []
    try:
        settings.assert_compliance_gate(real_corpus=True)
    except ConfigError as exc:
        blockers.append(f"合规闸未开：{exc}")
    try:
        settings.assert_l3_ingest_allowed(level)
    except ConfigError as exc:
        blockers.append(f"L3/L4 入库闸未开：{exc}")
    # 分片归属：L2/L3 必须给部门、L4 需 l4_enabled —— 与入库路径同一份判定。
    # 不前置的话，这些文档会被登记成功、再由 worker 一条条失败（界面只显示"上传成功"）。
    try:
        from ekbs.index.shard import resolve_shard_for_document

        resolve_shard_for_document(
            level=int(level), visible_dept_ids=list(dept_ids), settings=settings
        )
    except ConfigError as exc:
        blockers.append(f"密级/部门不满足入库条件：{exc}")
    return blockers


def enqueue_files(
    settings: Settings,
    db: Database,
    *,
    rel_paths: Sequence[str],
    kb_id: int,
    level: int,
    dept_ids: Sequence[str] = (),
    created_by: int | None = None,
    doc_version: str = "v1",
) -> dict[str, Any]:
    """把选中的文件登记成 ``status='pending'`` 文档，交给 worker 解析。

    与 CLI 的差别只有一处：**这里不解析**。CLI 是同步导入（跑完就知道结果），
    管理台是异步入队（进度去「解析任务」页看）—— 网页请求不该挂几十分钟。

    返回 ``{"enqueued": [...], "skipped": [...], "failed": [...]}``，
    逐条给出 doc_id 或跳过/失败原因。
    """
    import json

    blockers = check_blockers(settings, level=level, dept_ids=dept_ids)
    if blockers:
        raise ConfigError("；".join(blockers))
    if len(rel_paths) > ENQUEUE_LIMIT:
        # **明确拒绝而不是静默截断**：导入这种动作一旦少导了一半而不说，
        # 操作者会拿着"已完成"的提示去用缺了一半的知识库。
        raise ConfigError(
            f"一次最多登记 {ENQUEUE_LIMIT} 份（本次 {len(rel_paths)} 份）；"
            "请缩小目录范围、分批导入，或改用 CLI（ekbs.tools.import）"
        )

    enqueued: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    seen: set[str] = set()

    for rel in rel_paths:
        try:
            path = resolve_within_raw(settings, rel)
        except ImportPathError as exc:
            failed.append({"rel_path": rel, "error": str(exc)})
            continue
        if not path.is_file():
            failed.append({"rel_path": rel, "error": "文件不存在"})
            continue
        suffix = normalize_suffix(path.name)
        if f".{suffix}" not in SUFFIX_ENGINES:
            failed.append({"rel_path": rel, "error": f"不支持的格式：{suffix}"})
            continue

        checksum = checksum_of(path)
        if checksum in seen:
            skipped.append({"rel_path": rel, "reason": "本批内重复"})
            continue
        seen.add(checksum)
        existing = existing_doc_by_checksum(db, checksum)
        if existing is not None:
            skipped.append(
                {"rel_path": rel, "doc_id": existing, "reason": "内容已在库（checksum 去重）"}
            )
            continue

        title = path.stem
        with db.sync_connect() as conn:
            cursor = conn.execute(
                "INSERT INTO ekbs_document(kb_id, title, source_path, level, visible_dept_ids, "
                "status, checksum, size_bytes, doc_version, created_by) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                (
                    kb_id,
                    title,
                    str(path),
                    level,
                    json.dumps(list(dept_ids), ensure_ascii=False),
                    checksum,
                    path.stat().st_size,
                    doc_version,
                    created_by,
                ),
            )
            doc_id = int(cursor.lastrowid or 0)
            conn.commit()
        enqueued.append({"rel_path": rel, "doc_id": doc_id, "size_bytes": path.stat().st_size})

    log.info(
        "dir_enqueue_done",
        enqueued=len(enqueued), skipped=len(skipped), failed=len(failed),
        kb_id=kb_id, level=level,
    )
    return {"enqueued": enqueued, "skipped": skipped, "failed": failed}


def checksum_of(path: Path) -> str:
    """SHA-256 内容哈希（与 CLI、上传秒传同一口径）。"""
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def existing_doc_by_checksum(db: Database, checksum: str) -> int | None:
    """内容是否已在库（**全局去重**，不看 kb/密级 —— 与 CLI 一致）。

    全局去重是有意的：同一份文件在不同部门重复入库会得到两份向量与两份审计，
    需要多部门可见请用 ``dept_ids`` 授权，而不是重复导入。
    """
    with db.sync_connect() as conn:
        row = conn.execute(
            "SELECT doc_id FROM ekbs_document WHERE checksum = ? AND status != 'failed' "
            "ORDER BY doc_id LIMIT 1",
            (checksum,),
        ).fetchone()
    return int(row["doc_id"]) if row else None
