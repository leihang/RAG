"""权限点目录与角色授权 —— **单一来源**，供 ``seed_demo`` 与迁移工具共用。

为什么单独抽一个模块：权限点是**接口契约**（`05` §8.2 的映射表逐条对应），
它同时被三处引用 —— 新建库（seed）、升级既有库（migrate）、以及测试断言。
三处各写一份必然漂移（我在第 10 轮就吃过这个亏：自造的 ``kb:document:upload``
与 `05` 的 ``doc:document:create`` 对不上，接口与权限点错位）。

命名规则：**一律以 `05` §8.2 / §3.4 为准**，不自行发明。

角色授权的依据是 `05` §3.4 的角色矩阵（列序 SA / KA / DR / AU / EU）：

| 权限点 | SA | KA | DR | AU | EU |
|---|---|---|---|---|---|
| ``doc:document:read`` | ✔ | ✔ | ✔ | — | — |
| ``doc:document:create`` / ``update`` | ✔ | ✔ | ✔ / ✔ | — | — |
| ``doc:document:delete`` | ✔ | ✔ | — | — | — |
| ``doc:document:offline`` | ✔ | ✔ | ✔ | — | — |
| ``doc:document:reparse`` | ✔ | ✔ | ✔ | — | — |
| ``parse:task:read`` | ✔ | ✔ | ✔ | ✔ | — |
| ``parse:task:manage`` | ✔ | ✔ | — | — | — |
| ``audit:qa:read`` | ✔ | ✔ | — | ✔ | — |
| ``audit:qa:read_all`` / ``audit:trace:read`` | ✔ | — | — | ✔ | — |
| ``sys:dashboard:read`` | ✔ | ✔ | — | ✔ | — |
| ``sys:health:read`` | ✔ | ✔ | — | ✔ | — |
| ``sys:index:rebuild`` / ``sys:setting:read`` | ✔ | — | — | — | — |
| ``qa:chat`` | ✔ | ✔ | ✔ | ✔ | ✔ |

``super_admin`` 在判定处按**通配**处理（`ekbs.authz`），这里仍显式授全量，
便于管理台把「该角色有哪些权限点」展示成一张完整的表。
"""

from __future__ import annotations

from typing import Final

#: 权限点目录：(code, 名称, 模块)。**只列本项目已实现接口所需**；
#: `05` §3.4 的完整 57 点集属二期，届时按表追加即可。
PERMISSIONS: Final[tuple[tuple[str, str, str], ...]] = (
    # 文档（05 §3.4 M4）
    ("doc:document:read", "查看文档", "doc"),
    ("doc:document:create", "上传/登记文档", "doc"),
    ("doc:document:update", "编辑文档元数据", "doc"),
    ("doc:document:delete", "删除文档", "doc"),
    ("doc:document:publish", "发布文档", "doc"),
    ("doc:document:offline", "下线文档", "doc"),
    ("doc:document:reparse", "重新解析", "doc"),
    ("doc:document:download", "下载原文", "doc"),
    ("doc:chunk:read", "查看分块", "doc"),
    ("doc:chunk:update", "编辑分块", "doc"),
    ("doc:chunk:reembed", "重新嵌入分块", "doc"),
    ("doc:chunk:delete", "删除分块", "doc"),
    # 知识库
    ("kb:knowledge_base:read", "查看知识库", "kb"),
    ("kb:knowledge_base:create", "新建知识库", "kb"),
    # 解析
    ("parse:task:read", "查看解析任务", "parse"),
    ("parse:task:manage", "管理解析任务", "parse"),
    # 问答与检索
    ("qa:chat", "发起问答", "qa"),
    ("rag:playground:run", "检索调试台执行", "rag"),
    ("rag:playground:simulate", "以指定用户身份检索（高危）", "rag"),
    # 审计
    ("audit:qa:read", "查看问答日志", "audit"),
    ("audit:qa:read_all", "查看全部用户问答", "audit"),
    ("audit:trace:read", "查看数据访问日志", "audit"),
    # 权限管理（05 §3.4 M9）
    ("perm:user:read", "查看用户", "perm"),
    ("perm:user:update", "编辑用户（启用/禁用）", "perm"),
    ("perm:user:set_clearance", "设置用户密级上限", "perm"),
    ("perm:role:read", "查看角色", "perm"),
    ("perm:role:create", "创建角色", "perm"),
    ("perm:role:update", "编辑角色与权限点", "perm"),
    ("perm:role:delete", "删除角色", "perm"),
    ("perm:role:assign", "角色分配给用户", "perm"),
    ("perm:dept:read", "查看部门", "perm"),
    ("perm:dept:map", "部门映射配置", "perm"),
    ("perm:acl:read", "查看 ACL", "perm"),
    ("perm:acl:create", "新增 ACL 授权", "perm"),
    ("perm:acl:delete", "撤销 ACL 授权", "perm"),
    ("perm:acl:simulate", "权限模拟", "perm"),
    ("perm:audit:read", "查看权限变更审计", "perm"),
    # 系统
    ("sys:dashboard:read", "查看概览看板", "sys"),
    ("sys:health:read", "查看系统健康与分片", "sys"),
    ("sys:index:rebuild", "重建分片索引", "sys"),
    ("sys:setting:read", "查看生效配置", "sys"),
)

#: 角色 → 权限点（依据 `05` §3.4 的矩阵；super_admin 额外由代码通配兜底）
ROLE_GRANTS: Final[dict[str, tuple[str, ...]]] = {
    "end_user": ("qa:chat",),
    "auditor": (
        "qa:chat",
        "audit:qa:read",
        "audit:qa:read_all",
        "audit:trace:read",
        "doc:chunk:read",
        "parse:task:read",
        "perm:acl:read",
        "perm:audit:read",
        "perm:dept:read",
        "perm:role:read",
        "perm:user:read",
        "sys:dashboard:read",
        "sys:health:read",
    ),
    "doc_reviewer": (
        "qa:chat",
        "doc:document:read",
        "doc:document:create",
        "doc:document:update",
        "doc:document:publish",
        "doc:document:offline",
        "doc:document:reparse",
        "doc:document:download",
        "doc:chunk:read",
        "doc:chunk:update",
        "kb:knowledge_base:read",
        "parse:task:read",
        "rag:playground:run",
    ),
    "kb_admin": (
        "doc:document:read",
        "doc:document:create",
        "doc:document:update",
        "doc:document:delete",
        "doc:document:publish",
        "doc:document:offline",
        "doc:document:reparse",
        "doc:document:download",
        "doc:chunk:read",
        "doc:chunk:update",
        "doc:chunk:reembed",
        "kb:knowledge_base:read",
        "kb:knowledge_base:create",
        "parse:task:read",
        "parse:task:manage",
        "rag:playground:run",
        "perm:acl:create",
        "perm:acl:delete",
        "perm:acl:read",
        "perm:audit:read",
        "perm:dept:read",
        "perm:role:read",
        "perm:user:read",
        "perm:user:update",
        "sys:dashboard:read",
        "sys:health:read",
    ),
}

#: 历史/自造权限点 → 权威权限点（迁移用）。
#: 左列是 v1 骨架里 ``seed_demo`` 自造或早期命名的代码，右列是 `05` §8.2 的权威命名。
#: 映射为 ``None`` 表示**废弃**（`05` 明确这类配置走 YAML + Git 评审，不暴露写接口）。
LEGACY_PERMISSION_MAP: Final[dict[str, str | None]] = {
    "kb:document:upload": "doc:document:create",
    "kb:document:publish": "doc:document:publish",
    "system:model:config": None,  # 模型/知识库配置走 YAML（05 §8.1 的说明）
}

#: 内置角色（`00` §3.3 固定五个）
BUILTIN_ROLES: Final[tuple[tuple[str, str], ...]] = (
    ("super_admin", "超级管理员"),
    ("kb_admin", "知识库管理员"),
    ("doc_reviewer", "文档审核员"),
    ("auditor", "审计员"),
    ("end_user", "普通用户"),
)
