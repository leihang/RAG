"""文档上架范围（部门 × 密级）—— 补齐评估发现的两个缺口。

被钉住的两个规则：

1. **密级**：``文档 level ≤ 上传者 clearance``（原来完全不校验）；
2. **部门**：``visible_dept_ids`` 必须落在上传者的可管理部门内
   （自己部门 + 其所有下级）；不指定部门 = 全公司公开，要求管理范围覆盖全部部门
   （总部管理员）或超管。

`super_admin` 不受密级和部门限制。其余后台角色（知识库管理员、文档审核员、审计员）
不受钉钉部门限制，密级上限仍然有效。普通用户两边都受限制。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from ekbs.api.app import create_app
from ekbs.permission.upload_scope import check_document_scope
from tests.conftest import TEST_INTERNAL_TOKEN, set_clearance, user_id_of


# ---------------------------------------------------------------------------
# 规则函数（白盒）
# ---------------------------------------------------------------------------


def test_same_department_within_clearance_allowed(seeded) -> None:
    runtime, _ = seeded
    alice = user_id_of(runtime.db, "demo_alice")  # 人力资源部(102)，L2
    decision = check_document_scope(runtime.db, user_id=alice, level=2, visible_dept_ids=["102"])
    assert decision.allowed is True, decision.detail


def test_cross_department_is_rejected(seeded) -> None:
    """HR 的人不能往财务部传文档（读得到 ≠ 写得进）。"""
    runtime, _ = seeded
    alice = user_id_of(runtime.db, "demo_alice")
    decision = check_document_scope(runtime.db, user_id=alice, level=2, visible_dept_ids=["103"])
    assert decision.allowed is False
    assert decision.reason == "dept"


def test_level_above_clearance_is_rejected(seeded) -> None:
    """密级 2 的人不能传 L3 文档（原来这条完全不校验）。"""
    runtime, _ = seeded
    alice = user_id_of(runtime.db, "demo_alice")
    decision = check_document_scope(runtime.db, user_id=alice, level=3, visible_dept_ids=["102"])
    assert decision.allowed is False
    assert decision.reason == "level"


def test_company_wide_document_requires_global_scope(seeded) -> None:
    """不指定部门 = 全公司公开：单部门的人不能发；总部管理员可以。"""
    runtime, _ = seeded
    alice = user_id_of(runtime.db, "demo_alice")  # 部门 102
    denied = check_document_scope(runtime.db, user_id=alice, level=1, visible_dept_ids=[])
    assert denied.allowed is False and denied.reason == "global"

    carol = user_id_of(runtime.db, "demo_carol")  # 总部(1) —— 管理范围覆盖全部部门
    allowed = check_document_scope(runtime.db, user_id=carol, level=1, visible_dept_ids=[])
    assert allowed.allowed is True, allowed.detail


def test_root_department_admin_covers_all_departments(seeded) -> None:
    """总部（根部门）管理员能发布到任意部门 —— 这是"平台管理员"的既有语义。"""
    runtime, _ = seeded
    carol = user_id_of(runtime.db, "demo_carol")
    set_clearance(runtime, carol, 4)  # 平台管理员应有全量密级（seed 里是 L1 行政专员）
    assert check_document_scope(
        runtime.db, user_id=carol, level=2, visible_dept_ids=["102"]
    ).allowed is True
    assert check_document_scope(
        runtime.db, user_id=carol, level=3, visible_dept_ids=["103"]
    ).allowed is True


def test_super_admin_bypasses_both_rules(seeded) -> None:
    runtime, _ = seeded
    root = user_id_of(runtime.db, "demo_root")  # L4 + super_admin
    assert check_document_scope(
        runtime.db, user_id=root, level=4, visible_dept_ids=["103"]
    ).allowed is True
    assert check_document_scope(runtime.db, user_id=root, level=4, visible_dept_ids=[]).allowed is True


def test_super_admin_bypasses_even_with_low_clearance(seeded) -> None:
    """超管的例外是**角色**，不是密级数字：即便密级被设得很低也照常发布。"""
    runtime, _ = seeded
    root = user_id_of(runtime.db, "demo_root")
    set_clearance(runtime, root, 1)
    assert check_document_scope(
        runtime.db, user_id=root, level=4, visible_dept_ids=["103"]
    ).allowed is True


def test_unknown_department_is_rejected(seeded) -> None:
    runtime, _ = seeded
    carol = user_id_of(runtime.db, "demo_carol")
    set_clearance(runtime, carol, 4)  # 先过密级闸，才能验到"部门不存在"这一层
    decision = check_document_scope(
        runtime.db, user_id=carol, level=2, visible_dept_ids=["99999"]
    )
    assert decision.allowed is False and decision.reason == "unknown_dept"


def test_end_user_still_cannot_publish_outside_dept(seeded) -> None:
    """普通用户仍受钉钉部门限制。后台角色才跨部门。"""
    runtime, _ = seeded
    alice = user_id_of(runtime.db, "demo_alice")
    decision = check_document_scope(
        runtime.db, user_id=alice, level=2, visible_dept_ids=["103"]
    )
    assert decision.allowed is False and decision.reason == "dept"


# ---------------------------------------------------------------------------
# API 层（真的拦在上传入口）
# ---------------------------------------------------------------------------


async def _client(seeded):
    runtime, _ = seeded
    app = create_app(runtime.settings)
    transport = ASGITransport(app=app)
    return runtime, app, AsyncClient(transport=transport, base_url="http://testserver")


async def _grant(seeded, dingtalk_userid: str, role: str) -> int:
    runtime, _ = seeded
    user_id = user_id_of(runtime.db, dingtalk_userid)
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_user_role(user_id, role_id, scope_type, scope_id, granted_at) "
            "SELECT ?, role_id, 'global', 0, datetime('now') FROM ekbs_role WHERE code = ? "
            "ON CONFLICT DO NOTHING",
            (user_id, role),
        )
        conn.commit()
    return user_id


@pytest.mark.parametrize(
    "level,depts,expect_status,reason",
    [
        (2, ["102"], 200, ""),
        (2, ["103"], 200, ""),
        (3, ["102"], 403, "高于你的密级上限"),
    ],
)
async def test_upload_init_enforces_scope(seeded, level, depts, expect_status, reason) -> None:
    """知识库管理员（后台角色）：跨部门可以发布，超过本人密级仍然拒绝。"""
    hr_manager = await _grant(seeded, "demo_alice", "kb_admin")
    _, app, client = await _client(seeded)
    headers = {"X-Internal-Token": TEST_INTERNAL_TOKEN, "X-Internal-User-Id": str(hr_manager)}
    async with app.router.lifespan_context(app):
        async with client as c:
            response = await c.post(
                "/api/v1/docs/upload/init",
                headers=headers,
                json={
                    "filename": "制度.pdf",
                    "size_bytes": 1024,
                    "level": level,
                    "visible_dept_ids": depts,
                },
            )
    assert response.status_code == expect_status, response.text
    if reason:
        assert reason in response.json()["detail"]


async def test_register_document_enforces_scope(seeded) -> None:
    """后台角色可登记其他部门的文档；密级仍受本人上限约束。"""
    runtime, _ = seeded
    hr_manager = await _grant(seeded, "demo_alice", "kb_admin")
    _, app, client = await _client(seeded)
    headers = {"X-Internal-Token": TEST_INTERNAL_TOKEN, "X-Internal-User-Id": str(hr_manager)}
    raw = runtime.settings.data_paths()["raw"]
    source = raw / "越权登记.md"
    source.write_text("# 财务制度\n\n正文", encoding="utf-8")
    async with app.router.lifespan_context(app):
        async with client as c:
            response = await c.post(
                "/api/v1/docs/documents",
                headers=headers,
                json={
                    "title": "越权登记",
                    "kb_id": 1,
                    "level": 2,
                    "visible_dept_ids": ["103"],
                    "source_path": str(source),
                },
            )
    assert response.status_code in (200, 201), response.text
