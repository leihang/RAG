"""分片路由 —— ``visible_shard_ids`` 的**唯一入口**（`03` §4.1、`06` §2.5）。

规则（`docs/spec/10` §4，逐条对应）：

1. 候选 = ``ekbs_shard`` 中 ``status='active'`` 的分片（building/retired 不出现）；
2. 密级（纵向）：``level <= clearance``；
3. 部门（横向）：``dept_code == 'all'`` 或命中用户部门链；
4. ACL 叠加：``object_type='shard'`` 且 ``permission ∈ {read, manage}``、未过期；
5. ``(候选 ∪ allow) − deny``，**deny 最后减、优先于 allow**；``sorted()`` 输出。

路线之后任何检索路径都**只读取** :class:`~ekbs.permission.context.PermContext`，
不得自行重算或裁剪 —— 否则「指纹相同 ⇒ 分片集合相同」的恒等式（C2）就破了。

⚠️ **dept_code 与部门 ID 的映射（O6-1 / K1）**：权威方案未定义 ``dept_code`` 存在哪里
（`06` §7.3 把它列为开放项）。本实现用 ``permission.dept_code_map`` 配置承载
（``{hr: ["10203", ...]}``），**不新增 Schema**；正式定稿后应改用权威位置。
未配置该映射时，部门分片只能靠 shard 级 ACL 授予。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from ekbs.config import Settings
from ekbs.db import Database
from ekbs.errors import SchemaError
from ekbs.logging import get_logger
from ekbs.permission.context import PermContext

log = get_logger(component="shard_router")


@dataclass(frozen=True)
class RoutingResult:
    """路由的中间产物（含主体展开结果，供 C5 复用）。"""

    user_id: int
    dingtalk_userid: str
    clearance: int
    dept_chain: tuple[str, ...]
    visible_shard_ids: tuple[str, ...]
    visible_kb_ids: tuple[int, ...]
    policy_version: int
    acl_allow_shards: frozenset[str]
    acl_deny_shards: frozenset[str]
    matched_dept_codes: frozenset[str]
    subjects: frozenset[tuple[str, str]]
    dept_subjects: frozenset[str]
    warnings: tuple[str, ...] = ()


class ShardRouter:
    """纯 SQLite 的同步实现（单次 ≤5ms），异步调用方用 ``asyncio.to_thread`` 包一层。"""

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    # ------------------------------------------------------------------
    # 主体展开
    # ------------------------------------------------------------------

    @staticmethod
    def _role_codes(conn: Any, user_id: int) -> set[str]:
        rows = conn.execute(
            "SELECT r.code FROM ekbs_user_role ur JOIN ekbs_role r ON r.role_id = ur.role_id "
            "WHERE ur.user_id = ? AND r.status = 1",
            (user_id,),
        ).fetchall()
        return {str(r["code"]) for r in rows}

    @staticmethod
    def _group_codes(conn: Any, user_id: int) -> set[str]:
        rows = conn.execute(
            "SELECT g.code FROM ekbs_group_member gm "
            "JOIN ekbs_user_group g ON g.group_id = gm.group_id WHERE gm.user_id = ?",
            (user_id,),
        ).fetchall()
        return {str(r["code"]) for r in rows}

    @staticmethod
    def _dept_ids(conn: Any, user_id: int) -> set[str]:
        """用户在 ``ekbs_dept`` 中的整数 ``dept_id``（含祖先，经闭包表）。"""
        rows = conn.execute(
            "SELECT DISTINCT dc.ancestor_id AS dept_id FROM ekbs_user_dept ud "
            "JOIN ekbs_dept_closure dc ON dc.dept_id = ud.dept_id WHERE ud.user_id = ?",
            (user_id,),
        ).fetchall()
        return {str(r["dept_id"]) for r in rows}

    def _expand_subjects(
        self, conn: Any, user_id: int, dept_chain: Iterable[str]
    ) -> tuple[frozenset[tuple[str, str]], frozenset[str]]:
        """展开 ACL 主体：user / dept / role / group（`03` §2.3）。

        ``dept_subjects`` 同时收 **dingtalk_dept_id 文本** 与 **整数 dept_id** 两种写法，
        以兼容 K1 未定稿的值域（多收不会放宽语义：两者都指向同一部门）。
        """
        subjects: set[tuple[str, str]] = {("user", str(user_id))}
        dept_subjects: set[str] = {str(d) for d in dept_chain}
        dept_subjects |= self._dept_ids(conn, user_id)
        for code in self._role_codes(conn, user_id):
            subjects.add(("role", code))
        for code in self._group_codes(conn, user_id):
            subjects.add(("group", code))
        for dept in dept_subjects:
            subjects.add(("dept", dept))
        return frozenset(subjects), frozenset(dept_subjects)

    # ------------------------------------------------------------------
    # 部门 → dept_code（O6-1 的配置态替身）
    # ------------------------------------------------------------------

    def resolve_dept_codes(self, dept_subjects: frozenset[str]) -> frozenset[str]:
        dept_code_map: dict[str, list[str]] = self.settings.permission.dept_code_map or {}
        codes: set[str] = set()
        for code, dept_ids in dept_code_map.items():
            if {str(d) for d in dept_ids} & set(dept_subjects):
                codes.add(code)
        return frozenset(codes)

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------

    def policy_version(self, conn: Any) -> int:
        row = conn.execute("SELECT version FROM ekbs_policy_version WHERE id = 1").fetchone()
        if row is None:
            raise SchemaError("ekbs_policy_version 无数据（01 §5.3 种子缺失）")
        return int(row["version"])

    @staticmethod
    def _shard_acl(
        conn: Any, subjects: frozenset[tuple[str, str]], now: str
    ) -> tuple[frozenset[str], frozenset[str]]:
        """shard 级 ACL：``object_type='shard'``；允许读取的权限点见 `03` §6.3。"""
        rows = conn.execute(
            "SELECT object_id, subject_type, subject_id, effect FROM ekbs_acl_entry "
            "WHERE object_type = 'shard' AND permission IN ('read', 'manage') "
            "AND (expire_at IS NULL OR expire_at > ?)",
            (now,),
        ).fetchall()
        allow: set[str] = set()
        deny: set[str] = set()
        for row in rows:
            if (str(row["subject_type"]), str(row["subject_id"])) not in subjects:
                continue
            (allow if row["effect"] == "allow" else deny).add(str(row["object_id"]))
        return frozenset(allow), frozenset(deny)

    @staticmethod
    def _visible_kb_ids(
        conn: Any, subjects: frozenset[tuple[str, str]], now: str
    ) -> tuple[int, ...]:
        """知识库可见性（`03` §6.3 的 ``object_type='kb'``）。

        ⚠️ K3：``00`` §3.2 规定「L1 全员可见」，而按 kb ACL 收敛会与之冲突。
        本实现的取舍：**kb 级 ACL 只做「显式 deny 收敛 + 显式 allow 补齐」**，
        没有任何 kb 级条目时全部 active kb 可见（从而不破坏 L1 全员可见）。
        """
        rows = conn.execute(
            "SELECT kb_id, status FROM ekbs_kb"
        ).fetchall()
        active = {int(r["kb_id"]) for r in rows if int(r["status"]) == 1}
        acl_rows = conn.execute(
            "SELECT object_id, subject_type, subject_id, effect FROM ekbs_acl_entry "
            "WHERE object_type = 'kb' AND permission IN ('read', 'manage') "
            "AND (expire_at IS NULL OR expire_at > ?)",
            (now,),
        ).fetchall()
        deny: set[int] = set()
        allow: set[int] = set()
        for row in acl_rows:
            if (str(row["subject_type"]), str(row["subject_id"])) not in subjects:
                continue
            try:
                kb_id = int(row["object_id"])
            except (TypeError, ValueError):
                continue
            (allow if row["effect"] == "allow" else deny).add(kb_id)
        return tuple(sorted((active | allow) - deny))

    def route(self, user_id: int) -> RoutingResult:
        """计算该用户的可见分片集合（`docs/spec/10` §4 的四步）。"""
        with self.db.sync_connect() as conn:
            user = conn.execute(
                "SELECT user_id, dingtalk_userid, clearance, dept_chain, status "
                "FROM ekbs_user WHERE user_id = ? AND deleted_at IS NULL",
                (user_id,),
            ).fetchone()
            if user is None:
                raise PermissionError(f"用户不存在或已删除：user_id={user_id}")
            if str(user["status"]) != "active":
                raise PermissionError(
                    f"用户状态为 {user['status']}，无任何可见分片：user_id={user_id}"
                )

            import json

            dept_chain = tuple(sorted({str(d) for d in json.loads(user["dept_chain"] or "[]")}))
            clearance = int(user["clearance"])
            policy_version = self.policy_version(conn)
            now = str(conn.execute("SELECT datetime('now')").fetchone()[0])

            subjects, dept_subjects = self._expand_subjects(conn, user_id, dept_chain)
            dept_codes = self.resolve_dept_codes(dept_subjects)
            acl_allow, acl_deny = self._shard_acl(conn, subjects, now)

            shard_rows = conn.execute(
                "SELECT shard_id, dept_code, level FROM ekbs_shard WHERE status = 'active'"
            ).fetchall()
            level_of = {str(r["shard_id"]): int(r["level"]) for r in shard_rows}
            visible: set[str] = set()
            for row in shard_rows:
                if int(row["level"]) > clearance:
                    continue
                dept_code = str(row["dept_code"])
                if dept_code == "all" or dept_code in dept_codes:
                    visible.add(str(row["shard_id"]))
            # ACL allow **也必须过密级闸**：ACL 是跨部门的横向补丁，不能突破 clearance 这条纵向上限
            # （00 §3.2 规则 1：访问需满足 doc.level <= user.clearance）。
            granted_allow = frozenset(
                shard for shard in acl_allow if level_of.get(shard, 9) <= clearance
            )
            visible = (visible | set(granted_allow)) - set(acl_deny)

            warnings: list[str] = []
            if not dept_code_map_configured(self.settings) and not acl_allow:
                warnings.append(
                    "dept_code_map 未配置：部门分片只能靠 shard 级 ACL 授予（O6-1 未定稿）"
                )
            if not visible:
                # TC-SHARD-009：空集不是 500，也不是「空答案冒充正常」，必须留痕
                log.warning("user_has_no_visible_shard", user_id=user_id, clearance=clearance)
                warnings.append("当前身份没有任何可见分片，请检查部门/密级/ACL 配置")

            return RoutingResult(
                user_id=int(user["user_id"]),
                dingtalk_userid=str(user["dingtalk_userid"]),
                clearance=clearance,
                dept_chain=dept_chain,
                visible_shard_ids=tuple(sorted(visible)),
                visible_kb_ids=self._visible_kb_ids(conn, subjects, now),
                policy_version=policy_version,
                acl_allow_shards=granted_allow,
                acl_deny_shards=acl_deny,
                matched_dept_codes=dept_codes,
                subjects=subjects,
                dept_subjects=dept_subjects,
                warnings=tuple(warnings),
            )

    def build_context(self, user_id: int) -> PermContext:
        """:meth:`route` 的产物 → 不可变的 :class:`PermContext`。"""
        result = self.route(user_id)
        return PermContext(
            user_id=result.user_id,
            dingtalk_userid=result.dingtalk_userid,
            clearance=result.clearance,
            dept_chain=result.dept_chain,
            visible_shard_ids=result.visible_shard_ids,
            visible_kb_ids=result.visible_kb_ids,
            policy_version=result.policy_version,
            acl_allow_shards=result.acl_allow_shards,
            acl_deny_shards=result.acl_deny_shards,
            matched_dept_codes=result.matched_dept_codes,
            subjects=result.subjects,
            dept_subjects=result.dept_subjects,
            warnings=result.warnings,
        )


def dept_code_map_configured(settings: Settings) -> bool:
    return bool(settings.permission.dept_code_map)


def bump_policy_version(conn: Any, *, reason: str = "") -> int:
    """全局策略版本 +1（= 全量缓存失效）。

    **必须与 ACL / 密级 / 部门变更同事务**（`06` §2.6、`04` §4.6），否则会出现
    「ACL 变了、指纹没变、旧缓存复用」的窗口。返回自增后的版本号。
    """
    conn.execute(
        "UPDATE ekbs_policy_version SET version = version + 1, updated_at = datetime('now') "
        "WHERE id = 1"
    )
    row = conn.execute("SELECT version FROM ekbs_policy_version WHERE id = 1").fetchone()
    version = int(row["version"])
    log.info("policy_version_bumped", version=version, reason=reason)
    return version
