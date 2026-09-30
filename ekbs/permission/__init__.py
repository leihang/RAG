"""权限层：分片路由 + 权限指纹 + C5 文档闸门。

对外只用这三样：

* :func:`ekbs.permission.fingerprint.perm_fingerprint` —— 全仓库唯一实现（`00` §3.4）；
* :class:`ekbs.permission.router.ShardRouter` —— ``visible_shard_ids`` 唯一入口；
* :class:`ekbs.permission.acl.DocumentGate` —— C5 闸门。
"""

from ekbs.permission.acl import AclDecision, AclEntry, DocumentFacts, DocumentGate
from ekbs.permission.changes import ChangeResult, PermissionChangeService
from ekbs.permission.context import PermContext
from ekbs.permission.fingerprint import normalize_clearance, perm_fingerprint
from ekbs.permission.router import RoutingResult, ShardRouter, bump_policy_version

__all__ = [
    "AclDecision",
    "AclEntry",
    "DocumentFacts",
    "DocumentGate",
    "ChangeResult",
    "PermContext",
    "RoutingResult",
    "PermissionChangeService",
    "ShardRouter",
    "bump_policy_version",
    "normalize_clearance",
    "perm_fingerprint",
]
