"""权限上下文 —— 一次请求内「这个用户能看到什么」的唯一载体。

铁律（`03` §6.1、`00` C2）：

* 只由**服务端**计算，**不接受调用方声明**（`04` §1.5 修掉的信任路径）；
* 每轮问答重新构建，**不跨轮复用**；
* 检索、缓存、C5、审计都读取同一个对象，谁都不许自行重算或裁剪 ``visible_shard_ids``。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ekbs.permission.fingerprint import perm_fingerprint, normalize_clearance


@dataclass(frozen=True)
class PermContext:
    """不可变的权限快照（对应 `03` §4.1 的 ``PermContext``）。"""

    user_id: int
    dingtalk_userid: str
    clearance: int
    #: 本部门 + 全部祖先部门的 dingtalk_dept_id（JSON 数组口径，见 `01` §5.1）
    dept_chain: tuple[str, ...]
    #: 分片路由的**唯一**输出；顺序稳定（升序），因此可直接作为指纹输入
    visible_shard_ids: tuple[str, ...]
    visible_kb_ids: tuple[int, ...]
    policy_version: int
    #: shard 级 ACL 明细，供 C5 的「跨部门授权」判定复用（`06` §2.2）
    acl_allow_shards: frozenset[str] = frozenset()
    acl_deny_shards: frozenset[str] = frozenset()
    #: 命中的 dept_code（调试与审计用；不参与指纹）
    matched_dept_codes: frozenset[str] = frozenset()
    #: 角色/用户组展开出的 ACL 主体，形如 ``("role", "kb_admin")``
    subjects: frozenset[tuple[str, str]] = frozenset()
    #: 授权用部门主体（dept 主体经闭包含子部门，`03` §2.3）
    dept_subjects: frozenset[str] = frozenset()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def fingerprint(self) -> str:
        """权限指纹（`00` §3.4）—— 由本对象四要素即时算出，避免两处不一致。"""
        return perm_fingerprint(
            self.user_id,
            self.visible_shard_ids,
            normalize_clearance(self.clearance),
            self.dept_chain,
            self.policy_version,
        )

    @property
    def has_visible_shard(self) -> bool:
        return bool(self.visible_shard_ids)

    def key_fields(self) -> dict[str, object]:
        """审计/日志用的安全投影（不含正文，不含密钥）。"""
        return {
            "user_id": self.user_id,
            "clearance": self.clearance,
            "visible_shard_count": len(self.visible_shard_ids),
            "visible_shards": list(self.visible_shard_ids),
            "policy_version": self.policy_version,
            "fingerprint": self.fingerprint,
        }
