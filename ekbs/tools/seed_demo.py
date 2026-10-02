"""``python -m ekbs.tools.seed_demo`` —— 生成合成演示语料（**仅合成数据**）。

为什么需要它：合规闸（`00` §7）在拿到书面确认前是**关着**的 —— 真实制度语料不得做云端嵌入。
因此骨架阶段用合成语料把「入库 → 分片 → 检索 → C5 → 生成 → 引用」整条链跑通，
将来换成真实语料时，链路代码一行都不用改。

生成内容：

| 部门 | 用户 | 密级 | 说明 |
|---|---|---|---|
| 总部(1) | carol | L1 | 只能看 L1 公共分片 |
| 人力资源部(102) | alice | L2 | 能看 L1 + ``kb_hr_L2`` |
| 财务部(103) | bob | L3 | 能看 L1 + ``kb_finance_L3`` |

三份合成文档分别落 ``kb_all_L1`` / ``kb_hr_L2`` / ``kb_finance_L3``，
其中 L3 那份**只应由 bob 检索到** —— 这就是冒烟里「越权 0 泄漏」红线的本地版本。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from ekbs.config import Secrets, Settings, load_settings
from ekbs.db import Database
from ekbs.ingest.pipeline import IngestPipeline
from ekbs.logging import configure_logging, get_logger
from ekbs.runtime import Runtime
from ekbs.tools.sync_org import import_fixture

log = get_logger(component="tools.seed_demo")

DEMO_DEPTS = [
    {"dingtalk_dept_id": "1", "name": "总部", "parent_dingtalk_dept_id": None, "order_no": 1},
    {"dingtalk_dept_id": "102", "name": "人力资源部", "parent_dingtalk_dept_id": "1", "order_no": 2},
    {"dingtalk_dept_id": "103", "name": "财务部", "parent_dingtalk_dept_id": "1", "order_no": 3},
]

DEMO_USERS = [
    {
        "dingtalk_userid": "demo_carol", "nick": "Carol", "clearance": 1,
        "dept_ids": ["1"], "primary_dept_id": "1", "title": "行政专员",
    },
    {
        "dingtalk_userid": "demo_alice", "nick": "Alice", "clearance": 2,
        "dept_ids": ["102", "1"], "primary_dept_id": "102", "title": "HR 专员",
    },
    {
        "dingtalk_userid": "demo_bob", "nick": "Bob", "clearance": 3,
        "dept_ids": ["103", "1"], "primary_dept_id": "103", "title": "财务主管",
    },
    # 管理台的两个必要视角（方案 05 §1.2 五类角色画像）：
    # 没有它们，「问答日志与审计」（audit:qa:read 等）在演示库里永远打不开，
    # 看起来像功能缺失，实际是缺身份。
    {
        "dingtalk_userid": "demo_dave", "nick": "Dave", "clearance": 3,
        "dept_ids": ["1"], "primary_dept_id": "1", "title": "内审专员",
    },
    {
        "dingtalk_userid": "demo_root", "nick": "Root", "clearance": 4,
        "dept_ids": ["1"], "primary_dept_id": "1", "title": "平台管理员",
    },
]

BUILTIN_ROLES = [
    ("super_admin", "超级管理员"), ("kb_admin", "知识库管理员"), ("doc_reviewer", "文档审核员"),
    ("auditor", "审计员"), ("end_user", "普通用户"),
]

# 权限点与角色授权统一来自 ekbs.rbac_catalog（单一来源，避免与迁移工具漂移）
from ekbs.rbac_catalog import BUILTIN_ROLES, PERMISSIONS as BUILTIN_PERMISSIONS, ROLE_GRANTS

DEMO_DOCS: list[dict[str, Any]] = [
    {
        "title": "员工手册（总则）",
        "level": 1,
        "visible_dept_ids": [],
        "dept_code": "all",
        "markdown": (
            "# 员工手册（总则）\n\n"
            "## 第一章 行为规范\n\n"
            "全体员工应遵守公司考勤制度，工作日上午 9:00 前完成打卡。"
            "因公外出需提前在系统提交外出申请，由直属主管审批。"
            # 故意加长：确保切出 child，供分块编辑 API 用例使用
            + ("本条对考勤、外出与异常打卡的细则作补充说明。" * 40)
            + "\n\n"
            "## 第二章 办公秩序\n\n"
            "办公区域保持安静，会议室内禁止饮食。公司资产（含笔记本电脑、门禁卡）"
            "仅限本人使用，离职时须归还。"
            + ("办公秩序与资产保管细则继续补充如下。" * 40)
            + "\n"
        ),
    },
    {
        "title": "年假管理制度",
        "level": 2,
        "visible_dept_ids": ["102"],
        "dept_code": "hr",
        "markdown": (
            "# 年假管理制度\n\n"
            "## 第一条 年假天数\n\n"
            "员工年假天数按司龄确定：入职满 1 年享受 5 天，满 3 年享受 10 天，"
            "满 5 年享受 15 天。年假以自然年为周期计算，当年未休完的部分可结转至次年 3 月 31 日前使用。"
            + ("年假天数核算、结转与年度清算的补充说明。" * 40)
            + "\n\n"
            "## 第二条 申请流程\n\n"
            "年假申请须提前 3 个工作日在 HR 系统提交，经直属主管与人力资源部审批后生效。"
            "连续休假超过 5 个工作日的，须提前 10 个工作日申请。"
            + ("申请材料、审批时限与系统操作细则补充如下。" * 40)
            + "\n\n"
            "## 第三条 未休年假补偿\n\n"
            "因公司原因导致当年年假无法安排的，经人力资源部确认后可结转或按日工资标准补偿。"
            + ("未休年假补偿计算与发放细则补充如下。" * 40)
            + "\n"
        ),
    },
    {
        "title": "财务保密制度",
        "level": 3,
        "visible_dept_ids": ["103"],
        "dept_code": "finance",
        "markdown": (
            "# 财务保密制度\n\n"
            "## 第一条 保密范围\n\n"
            "年度预算明细、成本核算表、供应商报价单、银行账户信息属于公司机密，"
            "仅财务部相关人员可见，不得对外提供或转发至非财务部门。"
            + ("财务机密范围与接触权限的补充说明。" * 40)
            + "\n\n"
            "## 第二条 印章管理\n\n"
            "财务专用章与法人章由财务负责人分别保管，用印须填写用印申请单并留存影像记录。"
            + ("印章保管、用印审批与影像留存细则补充如下。" * 40)
            + "\n\n"
            "## 第三条 违规处理\n\n"
            "泄露财务机密信息者，视情节给予警告直至解除劳动合同，并保留追究法律责任的权利。"
            + ("违规查处流程与责任追究细则补充如下。" * 40)
            + "\n"
        ),
    },
]


def seed_static(db: Database, settings: Settings) -> dict[str, Any]:
    """组织、角色、知识库等静态数据（可重复执行）。"""
    summary = import_fixture(db, {"depts": DEMO_DEPTS, "users": DEMO_USERS})
    with db.sync_connect() as conn:
        for code, name in BUILTIN_ROLES:
            conn.execute(
                "INSERT INTO ekbs_role(code, name, is_builtin, status, created_at) "
                "VALUES (?, ?, 1, 1, datetime('now')) ON CONFLICT(code) DO NOTHING",
                (code, name),
            )
        for code, name, module in BUILTIN_PERMISSIONS:
            conn.execute(
                "INSERT INTO ekbs_permission(code, name, module) VALUES (?, ?, ?) "
                "ON CONFLICT(code) DO NOTHING",
                (code, name, module),
            )
        conn.execute(
            "INSERT INTO ekbs_role_permission(role_id, perm_id) "
            "SELECT r.role_id, p.perm_id FROM ekbs_role r, ekbs_permission p "
            "WHERE r.code = 'super_admin' ON CONFLICT DO NOTHING"
        )
        # 角色 → 权限点：依据 05 §3.4 的矩阵（目录见 ekbs/rbac_catalog.py）
        for role_code, perm_codes in ROLE_GRANTS.items():
            for perm_code in perm_codes:
                conn.execute(
                    "INSERT INTO ekbs_role_permission(role_id, perm_id) "
                    "SELECT r.role_id, p.perm_id FROM ekbs_role r, ekbs_permission p "
                    "WHERE r.code = ? AND p.code = ? ON CONFLICT DO NOTHING",
                    (role_code, perm_code),
                )
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, description, default_level, status, created_at) "
            "VALUES ('main', '企业知识库', '默认知识库；可在管理台「导入」页新建更多', 2, 1, "
            "datetime('now')) ON CONFLICT(code) DO NOTHING"
        )
        kb_row = conn.execute("SELECT kb_id FROM ekbs_kb WHERE code = 'main'").fetchone()
        kb_id = int(kb_row["kb_id"])
        # 角色授予：carol 为 kb_admin，alice/bob 为 end_user
        for dt_userid, role_code in (
            ("demo_carol", "kb_admin"), ("demo_alice", "end_user"), ("demo_bob", "end_user"),
            ("demo_dave", "auditor"), ("demo_root", "super_admin"),
        ):
            conn.execute(
                "INSERT INTO ekbs_user_role(user_id, role_id, scope_type, scope_id, granted_at) "
                "SELECT u.user_id, r.role_id, 'global', 0, datetime('now') "
                "FROM ekbs_user u, ekbs_role r WHERE u.dingtalk_userid = ? AND r.code = ? "
                "ON CONFLICT DO NOTHING",
                (dt_userid, role_code),
            )
        conn.commit()
    summary["kb_id"] = kb_id
    return summary


async def seed_documents(runtime: Runtime, kb_id: int) -> list[dict[str, Any]]:
    pipeline = IngestPipeline(
        db=runtime.db, settings=runtime.settings, providers=runtime.providers,
        shards=runtime.shards, fts=runtime.fts,
        figure_index=runtime.figure_index,
    )
    results: list[dict[str, Any]] = []
    # ⚠️ L3 入库闸（00 C5 / 01 §7）：默认 l3_ingest_enabled=false，**生产配置不得改**。
    #    这里只在本次进程内临时开闸，用于生成**合成** L3 分片，以验证「低权限用户
    #    召不回高密级内容」这条红线（tests/test_permission.py、tests/test_retrieval.py 覆盖）。
    #    闸门针对的是真实语料出网与片内 deny；合成语料 + 已实现的 C5 不构成该风险。
    original_flag = runtime.settings.permission.l3_ingest_enabled
    original_ack = runtime.settings.permission.corpus_egress_ack
    runtime.settings.permission.l3_ingest_enabled = True
    runtime.settings.permission.corpus_egress_ack = True
    try:
        for doc in DEMO_DOCS:
            existing = _existing_doc(runtime.db, str(doc["title"]))
            result = await pipeline.ingest_markdown(
                title=str(doc["title"]),
                markdown=str(doc["markdown"]),
                kb_id=kb_id,
                level=int(doc["level"]),
                visible_dept_ids=list(doc["visible_dept_ids"]),
                source_path=f"demo://{doc['title']}",
                real_corpus=True,  # 已临时开闸；见上方说明
                existing_doc_id=existing,
            )
            # shard 级 ACL allow：让该部门能路由到这片（也演示 06 §2.2 的横向授权路径）
            if doc["visible_dept_ids"]:
                _grant_shard(runtime.db, result.shard_id, str(doc["visible_dept_ids"][0]))
            results.append({**result.as_dict(), "title": doc["title"], "level": doc["level"]})
    finally:
        runtime.settings.permission.l3_ingest_enabled = original_flag
        runtime.settings.permission.corpus_egress_ack = original_ack
    return results


def _existing_doc(db: Database, title: str) -> int | None:
    with db.sync_connect() as conn:
        row = conn.execute(
            "SELECT doc_id FROM ekbs_document WHERE title = ? ORDER BY doc_id DESC LIMIT 1",
            (title,),
        ).fetchone()
    return int(row["doc_id"]) if row else None


def _grant_shard(db: Database, shard_id: str, dept_ref: str) -> None:
    with db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_acl_entry(object_type, object_id, subject_type, subject_id, "
            "permission, effect, created_at) "
            "VALUES ('shard', ?, 'dept', ?, 'read', 'allow', datetime('now')) "
            "ON CONFLICT(object_type, object_id, subject_type, subject_id, permission) "
            "DO NOTHING",
            (shard_id, dept_ref),
        )
        conn.commit()


async def seed(settings: Settings | None = None, *, runtime: Runtime | None = None) -> dict[str, Any]:
    settings = settings or load_settings()
    owned = runtime is None
    runtime = runtime or Runtime.create(settings, service="tools", init_schema=True)
    try:
        # dept_code 映射（O6-1 / K1）：权威方案未定义该映射存哪，这里临时落到进程内的
        # permission.dept_code_map，使合成数据的分片名符合 00 §3.1 的示例（kb_hr_L2）。
        # **不改配置文件**：生产是否启用该映射由架构负责人定稿后再写入 application.yaml。
        # 同时 seed 会写 shard 级 ACL allow，因此即便没有这份映射也能路由到分片。
        if not settings.permission.dept_code_map:
            settings.permission.dept_code_map = {"hr": ["102"], "finance": ["103"]}
            log.warning(
                "dept_code_map_in_memory_only",
                mapping=settings.permission.dept_code_map,
                hint="O6-1 定稿后请写入 application.yaml 的 permission.dept_code_map",
            )
        static = seed_static(runtime.db, settings)
        docs = await seed_documents(runtime, int(static["kb_id"]))
        # 建分片后清除内存索引缓存，确保后续请求看到最新向量
        for doc in docs:
            runtime.shards.drop_from_cache(str(doc["shard_id"]))
        summary = {**static, "documents": docs}
        log.info(
            "seed_done",
            depts=static["depts"], users=static["users"],
            documents=len(docs), vectors=sum(int(d["vector_count"]) for d in docs),
        )
        return summary
    finally:
        if owned:
            await runtime.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成合成演示语料（仅合成数据）")
    parser.add_argument(
        "--identities-only",
        action="store_true",
        help="只重建组织/角色/权限/用户，不建知识库、不灌语料（清库后从零开始测试用）",
    )
    args = parser.parse_args(argv)
    settings = load_settings()
    configure_logging(settings, "tools")
    # 清库后「能登录但一个知识库都没有」是部署前功能测试的起点：
    # 演示语料会真的调云端嵌入（有 token 成本）并且会塞进一个演示知识库，
    # 而这两件事在"我要自己建库、自己传文档"的场景里都是污染。
    if args.identities_only:
        settings.ensure_dirs()
        db = Database(settings)
        db.ensure_schema()
        static = seed_static(db, settings)
        with db.sync_connect() as conn:
            removed = int(conn.execute("SELECT count(*) AS c FROM ekbs_kb").fetchone()["c"])
            conn.execute("DELETE FROM ekbs_kb")
            conn.commit()
        print("已重建身份（未建知识库、未灌语料）：")
        print(f"  部门 {static['depts']}  用户 {static['users']}")
        print(f"  seed 顺带建的知识库已删除 {removed} 个 —— 当前库里 0 个知识库。")
        print("  下一步：在管理台「导入」页点「新建」建第一个知识库，再上传文档。")
        return 0
    summary = asyncio.run(seed(settings))
    print("已生成演示数据：")
    print(f"  部门 {summary['depts']}  用户 {summary['users']}  知识库 kb_id={summary['kb_id']}")
    for doc in summary["documents"]:
        print(
            f"  - [{doc['title']}] L{doc['level']} → {doc['shard_id']} "
            f"父子块 {doc['parent_count']}/{doc['child_count']}  向量 {doc['vector_count']}"
        )
    print("\n提示：dept_code 与部门 ID 的映射（O6-1）当前由 shard 级 ACL 承载；")
    print("      正式部署请在 application.yaml 的 permission.dept_code_map 中配置。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
