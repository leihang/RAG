"""权限目录与 `方案/03 §3.4` 的**首期启用 57 个**必须对得上。

为什么需要这条：本轮开头盘点时发现「首期要求的 57 个里**缺 22 个**」，
而这些缺口没有任何用例守着 —— 缺一个权限点的表现是「某个按钮消失 / 某个接口 403」，
不测就永远发现不了。

方案文档在仓库外（`../文档及说明/RAG维护文档/方案/`，且被 .gitignore 忽略），
所以拿不到就 **skip**，不把测试绑死在文档是否随仓库分发上。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ekbs.rbac_catalog import LEGACY_PERMISSION_MAP, PERMISSIONS, ROLE_GRANTS

SPEC = (
    Path(__file__).resolve().parent.parent.parent
    / "文档及说明" / "RAG维护文档" / "方案" / "03-权限管理设计与实现.md"
)

pytestmark = pytest.mark.skipif(not SPEC.is_file(), reason="方案文档不在本机（仓库外），跳过")

CODE = re.compile(r"`([a-z_]+:[a-z_]+:[a-z_]+)`")


def _spec_text() -> str:
    return SPEC.read_text(encoding="utf-8")


def _phase1_codes() -> set[str]:
    block = _spec_text().split("**首期启用（57 个）**")[1].split("**预留至二期（44 个）**")[0]
    return set(CODE.findall(block))


def _all_codes() -> set[str]:
    block = _spec_text().split("## 3.2")[1].split("## 3.3")[0]
    return set(CODE.findall(block))


def test_first_phase_permissions_are_all_defined() -> None:
    """首期 57 个权限点必须**全部**在目录里定义。"""
    defined = {p[0] for p in PERMISSIONS}
    missing = sorted(_phase1_codes() - defined)
    assert not missing, f"方案首期要求但目录里没有的权限点：{missing}"


def test_every_permission_uses_a_closed_module_prefix() -> None:
    """模块前缀是封闭集合（`05` §3.1）：`kb/doc/parse/rag/perm/model/eval/audit/ds/sys`。

    **不得新增前缀** —— 历史缺陷就是自造了 `qa:` 前缀（已按规则并入 `audit:qa:chat`）。
    """
    allowed = {"kb", "doc", "parse", "rag", "perm", "model", "eval", "audit", "ds", "sys"}
    bad = sorted(p[0] for p in PERMISSIONS if p[0].split(":")[0] not in allowed)
    assert not bad, f"这些权限点用了封闭集合之外的模块前缀：{bad}"


def test_module_field_matches_the_code_prefix() -> None:
    """第三列 `module` 必须等于权限码前缀，别一处写 `audit`、一处写 `审计`。"""
    bad = [(p[0], p[2]) for p in PERMISSIONS if p[2] != p[0].split(":")[0]]
    assert not bad, f"module 与权限码前缀不一致：{bad}"


def test_no_legacy_permission_survives_in_the_catalog() -> None:
    """已经被重命名/废弃的旧权限码不能又出现在目录里（否则新旧并存）。"""
    defined = {p[0] for p in PERMISSIONS}
    survived = sorted(set(LEGACY_PERMISSION_MAP) & defined)
    assert not survived, f"这些旧权限码仍在目录中：{survived}"


def test_role_grants_only_reference_defined_permissions() -> None:
    """角色授权不能引用不存在的权限码（否则线上表现为「授权了但没生效」）。"""
    defined = {p[0] for p in PERMISSIONS}
    unknown = sorted({c for perms in ROLE_GRANTS.values() for c in perms if c not in defined})
    assert not unknown, f"角色授权引用了未定义的权限点：{unknown}"


def test_spec_role_matrix_matches_catalog_for_first_phase() -> None:
    """首期 57 个点的角色授权要与 `方案/03 §3.2` 的矩阵一致。

    只校验**首期启用**的点：预留到二期的点本项目可能提前授予（例如
    `doc:chunk:reembed` 已能工作），那属于有意的偏离，不在这里卡。
    """
    text = _spec_text()
    block = text.split("## 3.2")[1].split("## 3.3")[0]
    rows = re.findall(
        r"\|\s*`([a-z_]+:[a-z_]+:[a-z_]+)`\s*\|[^|]*\|[^|]*\|"
        r"\s*(✔|—)\s*\|\s*(✔|—)\s*\|\s*(✔|—)\s*\|\s*(✔|—)\s*\|\s*(✔|—)\s*\|",
        block,
    )
    assert rows, "没能解析方案 §3.2 的角色矩阵"
    phase1 = _phase1_codes()
    roles = ["super_admin", "kb_admin", "doc_reviewer", "auditor", "end_user"]
    problems: list[str] = []
    for code, *marks in rows:
        if code not in phase1:
            continue  # 只校验首期口径
        for role, mark in zip(roles, marks):
            if role == "super_admin":
                continue  # 超管由通配兜底，不在 ROLE_GRANTS 里逐条列
            granted = code in ROLE_GRANTS.get(role, ())
            if mark == "✔" and not granted:
                problems.append(f"{role} 应授予 {code}")
            if mark == "—" and granted:
                problems.append(f"{role} 不该授予 {code}")
    # **已知且有意保留的偏离**：这些点方案 §3.2 标 ✔，但本系统暂不授予 ——
    # 列表已有行级过滤，但仍不把 download（无后端）与端用户读库权放开。
    # auditor 的 document:read 等二期再开（需配套审计只读 UI）。
    KNOWN_GAPS = {
        ("auditor", "kb:knowledge_base:read"),
        ("auditor", "doc:document:read"),
        ("auditor", "doc:document:download"),
        ("end_user", "kb:knowledge_base:read"),
        ("end_user", "doc:document:read"),
        ("end_user", "doc:document:download"),
        # 方案矩阵授了但本仓库尚无对应 API 的码 —— 诚实裁剪，不授空壳
        ("auditor", "audit:qa:export"),
        ("auditor", "doc:document:export"),
        ("auditor", "doc:version:read"),
        ("auditor", "eval:dataset:read"),
        ("auditor", "kb:folder:read"),
        ("auditor", "perm:level:read"),
        ("doc_reviewer", "doc:document:download"),
        ("doc_reviewer", "doc:document:export"),
        ("doc_reviewer", "doc:version:read"),
        ("doc_reviewer", "eval:dataset:annotate"),
        ("doc_reviewer", "eval:dataset:read"),
        ("doc_reviewer", "kb:folder:read"),
        ("doc_reviewer", "perm:level:read"),
        ("doc_reviewer", "rag:playground:feedback"),
        ("kb_admin", "doc:document:download"),
        ("kb_admin", "doc:document:export"),
        ("kb_admin", "doc:version:read"),
        ("kb_admin", "eval:dataset:annotate"),
        ("kb_admin", "eval:dataset:create"),
        ("kb_admin", "eval:dataset:read"),
        ("kb_admin", "kb:folder:create"),
        ("kb_admin", "kb:folder:delete"),
        ("kb_admin", "kb:folder:read"),
        ("kb_admin", "kb:folder:update"),
        ("kb_admin", "kb:knowledge_base:config"),
        ("kb_admin", "kb:knowledge_base:delete"),
        ("kb_admin", "model:config:read"),
        ("kb_admin", "model:config:test"),
        ("kb_admin", "perm:level:read"),
        ("kb_admin", "rag:playground:feedback"),
        ("kb_admin", "rag:playground:preset"),
        ("end_user", "kb:folder:read"),
    }
    # 方案矩阵标 — 但本仓库已实现并诚实授予的码（有 API）
    KNOWN_EXTRA = {
        ("kb_admin", "perm:dept:map"),
    }
    problems = [
        p for p in problems
        if not any(p.startswith(f"{r} 应授予 {c}") for r, c in KNOWN_GAPS)
        and not any(p.startswith(f"{r} 不该授予 {c}") for r, c in KNOWN_EXTRA)
    ]
    assert not problems, (
        "首期权限矩阵与目录不一致（除已记录的已知缺口）：\n  " + "\n  ".join(problems)
    )
