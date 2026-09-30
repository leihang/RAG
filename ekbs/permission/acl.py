"""ACL 判定与 **C5 闸门** —— 进 LLM 前的最后一道文档级权限检查（`00` 约束 C5）。

为什么分片过滤不够：分片粒度只能表达「(部门 × 密级)」，无法表达**同部门同密级下的单文档 deny**。
「无权分片不参与计算」≠「片内没有 deny 条目」。因此 C5 是红线而非优化项
（`03` §5.2、`00` C5）：未落地 C5 前 ``permission.l3_ingest_enabled`` 必须为 false。

判定顺序严格按 `docs/spec/10` §7（状态/时效 → 密级 → kb → deny → allow → L4 → 部门），
并补入 **K6 修正**：跨部门文档在 `06` §2.2 是靠 **shard 级 allow** 授权的，若 C5 只认
document 级 ACL，就会出现「能召回、过不了闸门」的错杀，因此第 6.5 步接受
「该文档所属 shard 在本用户 ``acl_allow_shards`` 中」为放行依据。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from ekbs.config import Settings
from ekbs.db import Database
from ekbs.logging import get_logger
from ekbs.permission.context import PermContext

log = get_logger(component="acl")

#: 读判定可接受的权限点（`03` §6.3：`write` / `audit` 不授予读）
_READ_PERMISSIONS = ("read", "manage")


@dataclass(frozen=True)
class DocumentFacts:
    """C5 需要的文档事实（不含正文 —— 闸门只看权限属性）。"""

    doc_id: int
    kb_id: int
    title: str
    level: int
    status: str
    visible_dept_ids: tuple[str, ...]
    effective_date: str | None
    expire_date: str | None
    doc_version: str = "v1"
    shard_id: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> "DocumentFacts":
        raw_depts = row["visible_dept_ids"] or "[]"
        try:
            depts = tuple(str(d) for d in json.loads(raw_depts))
        except (TypeError, ValueError):
            depts = ()
        keys = row.keys()
        return cls(
            doc_id=int(row["doc_id"]),
            kb_id=int(row["kb_id"]),
            title=str(row["title"]),
            level=int(row["level"]),
            status=str(row["status"]),
            visible_dept_ids=depts,
            effective_date=row["effective_date"],
            expire_date=row["expire_date"],
            doc_version=str(row["doc_version"]) if "doc_version" in keys else "v1",
            shard_id=str(row["shard_id"]) if "shard_id" in keys and row["shard_id"] else None,
        )


@dataclass(frozen=True)
class AclDecision:
    allowed: bool
    reason: str

    def __bool__(self) -> bool:  # 便于 `if decision:`
        return self.allowed


@dataclass(frozen=True)
class AclEntry:
    object_type: str
    object_id: str
    subject_type: str
    subject_id: str
    permission: str
    effect: str


class DocumentGate:
    """C5：``can_read_document`` 的实现 + 事实加载 + 安全事件留痕。"""

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    # ------------------------------------------------------------------
    # 数据加载
    # ------------------------------------------------------------------

    def load_facts(self, doc_ids: Sequence[int]) -> dict[int, DocumentFacts]:
        """批量取文档事实（一次 SQL，避免 C5 在候选循环里 N+1）。"""
        if not doc_ids:
            return {}
        placeholders = ",".join("?" for _ in doc_ids)
        sql = (
            "SELECT d.doc_id, d.kb_id, d.title, d.level, d.status, d.visible_dept_ids, "
            "       d.effective_date, d.expire_date, d.doc_version, "
            "       (SELECT c.shard_id FROM ekbs_chunk c WHERE c.doc_id = d.doc_id "
            "        ORDER BY c.chunk_id LIMIT 1) AS shard_id "
            f"FROM ekbs_document d WHERE d.doc_id IN ({placeholders})"
        )
        with self.db.sync_connect() as conn:
            rows = conn.execute(sql, tuple(int(d) for d in doc_ids)).fetchall()
        return {int(r["doc_id"]): DocumentFacts.from_row(r) for r in rows}

    def load_document_acl(self, doc_ids: Sequence[int]) -> dict[int, list[AclEntry]]:
        """批量取文档级 ACL（含 ``folder`` 逐层累积语义的近似：本版只取 document 级）。"""
        if not doc_ids:
            return {}
        placeholders = ",".join("?" for _ in doc_ids)
        sql = (
            "SELECT object_id, object_type, subject_type, subject_id, permission, effect "
            "FROM ekbs_acl_entry WHERE object_type = 'document' "
            f"AND object_id IN ({placeholders}) "
            "AND (expire_at IS NULL OR expire_at > datetime('now'))"
        )
        result: dict[int, list[AclEntry]] = {int(d): [] for d in doc_ids}
        with self.db.sync_connect() as conn:
            rows = conn.execute(sql, tuple(str(int(d)) for d in doc_ids)).fetchall()
        for row in rows:
            entry = AclEntry(
                object_type=str(row["object_type"]),
                object_id=str(row["object_id"]),
                subject_type=str(row["subject_type"]),
                subject_id=str(row["subject_id"]),
                permission=str(row["permission"]),
                effect=str(row["effect"]),
            )
            try:
                doc_id = int(entry.object_id)
            except ValueError:
                continue
            result.setdefault(doc_id, []).append(entry)
        return result

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------

    @staticmethod
    def _entry_matches(entry: AclEntry, perm: PermContext) -> bool:
        if entry.permission not in _READ_PERMISSIONS:
            return False
        if (entry.subject_type, entry.subject_id) in perm.subjects:
            return True
        # dept 主体的值域在 K1 下未定稿：文本 dingtalk id 与整数 dept_id 都接受
        return entry.subject_type == "dept" and entry.subject_id in perm.dept_subjects

    def can_read_document(
        self,
        perm: PermContext,
        doc: DocumentFacts | None,
        entries: Iterable[AclEntry] | None = None,
        *,
        today: str | None = None,
    ) -> AclDecision:
        """`docs/spec/10` §7 的精确判定顺序（不可调整顺序：先 deny 再 allow 才能保证 deny 优先）。"""
        if doc is None:
            return AclDecision(False, "doc_not_found")
        # 1) 状态与时效
        if doc.status != "published":
            return AclDecision(False, "status")
        today = today or _today()
        if doc.effective_date and str(doc.effective_date) > today:
            return AclDecision(False, "not_effective")
        if doc.expire_date and str(doc.expire_date) <= today:
            return AclDecision(False, "expired")
        # 2) 密级（纵向）
        if doc.level > perm.clearance:
            return AclDecision(False, "level")
        # 3) 知识库可见性。
        # ⚠️ 这里**必须**是严格成员判断，不能写成 `if perm.visible_kb_ids and ...`：
        #    「可见知识库集合为空」意味着该用户看不到任何知识库，而不是「无限制」。
        #    （实测踩过：kb 级 deny 收敛掉全部可见 kb 后，空集被当成放行，L1 文档照样被读出。）
        #    K3 冲突提示：这等于让「kb 级 ACL」压过「L1 全员可见」（00 §3.2 规则 2），
        #    是有意的取舍 —— 显式 deny 应当生效；如需反过来，请先改 00/03 的口径。
        if doc.kb_id not in perm.visible_kb_ids:
            return AclDecision(False, "kb")
        # 4) 文档级 ACL：deny 优先
        entry_list = list(entries or [])
        for entry in entry_list:
            if entry.effect == "deny" and self._entry_matches(entry, perm):
                return AclDecision(False, "acl_deny")
        for entry in entry_list:
            if entry.effect == "allow" and self._entry_matches(entry, perm):
                return AclDecision(True, "acl_allow")
        # 4.5) K6 修正：跨部门文档靠 shard 级 allow 授权（06 §2.2）
        if doc.shard_id and doc.shard_id in perm.acl_allow_shards:
            return AclDecision(True, "shard_acl_allow")
        # 5) L4 白名单：没有显式 allow 就拒绝
        if doc.level == 4:
            return AclDecision(False, "l4_whitelist")
        # 6) 部门 / 公开
        if doc.level == 1:
            return AclDecision(True, "public")
        if not doc.visible_dept_ids:
            return AclDecision(True, "internal_open")
        if set(doc.visible_dept_ids) & set(perm.dept_chain):
            return AclDecision(True, "dept")
        # 兜底：文档可见部门用的是整数 dept_id 写法时也能命中
        if set(doc.visible_dept_ids) & set(perm.dept_subjects):
            return AclDecision(True, "dept")
        return AclDecision(False, "dept")

    def filter_chunks(
        self,
        perm: PermContext,
        chunks: Sequence[tuple[int, int]],
    ) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
        """对 ``[(chunk_id, doc_id), …]`` 执行 C5，返回 ``(放行, 丢弃)``。

        丢弃者**不得**进入 LLM 上下文、答案缓存或日志正文（`03` §5.2.1）。
        """
        doc_ids = sorted({doc_id for _, doc_id in chunks})
        facts = self.load_facts(doc_ids)
        entries = self.load_document_acl(doc_ids)
        kept: list[tuple[int, int]] = []
        dropped: list[tuple[int, int]] = []
        for chunk_id, doc_id in chunks:
            decision = self.can_read_document(perm, facts.get(doc_id), entries.get(doc_id, []))
            if decision.allowed:
                kept.append((chunk_id, doc_id))
            else:
                dropped.append((chunk_id, doc_id))
                from ekbs.observability.metrics import C5_DENIED, inc

                inc(C5_DENIED, 1, reason=decision.reason)
                log.info(
                    "c5_denied",
                    user_id=perm.user_id,
                    doc_id=doc_id,
                    chunk_id=chunk_id,
                    reason=decision.reason,
                )
        return kept, dropped

    def unauthorized_titles(self, perm: PermContext) -> list[str]:
        """当前用户**看不到**的已发布文档标题（L4 输出侧检测的输入，`04` §6.4）。

        判定与检索侧同口径：密级高于 clearance，或文档所属分片不在可见分片集合里。
        这类标题一旦出现在答案里，就说明模型把不该说的说了出来（或幻觉出了别的文档名）。

        ⚠️ 复杂度 O(已发布文档数)：试点规模（≤10 万页 / 千级文档）可接受；
        上量后应改为「答案文本 → 候选标题」的反向索引，而不是全量扫描。
        """
        sql = (
            "SELECT d.title, d.level, "
            "       (SELECT c.shard_id FROM ekbs_chunk c WHERE c.doc_id = d.doc_id "
            "        ORDER BY c.chunk_id LIMIT 1) AS shard_id "
            "FROM ekbs_document d WHERE d.status = 'published'"
        )
        with self.db.sync_connect() as conn:
            rows = conn.execute(sql).fetchall()
        visible = set(perm.visible_shard_ids)
        titles: list[str] = []
        for row in rows:
            title = str(row["title"] or "").strip()
            if not title:
                continue
            if int(row["level"]) > perm.clearance:
                titles.append(title)
            elif row["shard_id"] and str(row["shard_id"]) not in visible:
                titles.append(title)
        return titles

    # ------------------------------------------------------------------
    # 留痕
    # ------------------------------------------------------------------

    def record_security_event(
        self,
        event_type: str,
        *,
        user_id: int | None,
        severity: str,
        detail: dict[str, Any],
    ) -> None:
        """写 ``ekbs_security_event``（append-only，触发器保证只写不改）。"""
        with self.db.sync_connect() as conn:
            conn.execute(
                "INSERT INTO ekbs_security_event(event_type, user_id, severity, detail) "
                "VALUES (?, ?, ?, ?)",
                (event_type, user_id, severity, json.dumps(detail, ensure_ascii=False)),
            )
            conn.commit()


def _today() -> str:
    from datetime import date

    return date.today().isoformat()
