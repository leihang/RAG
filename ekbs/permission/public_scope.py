"""群聊公开范围：把个人私有库从权限快照中剔除。

为什么需要单独一层：钉钉群消息的检索/回复用的是**提问者**的权限，而回复会**公开**
发到群里。个人库（``personal-<uid>`` 知识库、``kb_p<uid>_L*`` 分片）只属于提问者，
一旦在群聊里被检索/列目录/发文件，群里其他人也能看到。群聊只允许公开文档，因此
在进入检索、目录、文件发送之前，把个人库分片/KB 从 ``PermContext`` 里剪掉——
所有下游（D18、C5、catalog、file）都读同一个 perm，不再需要各自补漏。

注意：这里只剔除**个人库**；公共库里按密级/部门可见的文档仍遵循提问者原有权限。
群聊的更强口径（群 ACL / 群可见部门）属于 P1，不在这一层。
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from ekbs.permission.context import PermContext

#: 个人分片命名：``kb_p<uid>_L<level>``（见 shard_router 的 personal 分支）
_PERSONAL_SHARD_RE = re.compile(r"^kb_p\d+_L[1-4]$")
#: 个人知识库命名：``personal-<uid>``（见 ekbs.personal.ensure_personal_kb）
_PERSONAL_KB_CODE = "personal-%"


def public_scope_perm(perm: PermContext, db: Any) -> PermContext:
    """返回只保留公开文档权限的快照（个人库分片/KB/allow ACL 全部剪掉）。

    没有任何个人库可剪时原样返回；否则用 ``dataclasses.replace`` 生成新快照 ——
    fingerprint 会随之变化，答案缓存自然按“群聊公开范围”分开，不会串用单聊答案。
    """
    shards = tuple(
        shard for shard in perm.visible_shard_ids if not _PERSONAL_SHARD_RE.match(shard)
    )
    kbs = tuple(perm.visible_kb_ids)
    if kbs:
        placeholders = ",".join("?" for _ in kbs)
        with db.sync_connect() as conn:
            personal_kb_ids = {
                int(row["kb_id"])
                for row in conn.execute(
                    f"SELECT kb_id FROM ekbs_kb WHERE code LIKE ? AND kb_id IN ({placeholders})",
                    (_PERSONAL_KB_CODE, *kbs),
                ).fetchall()
            }
        if personal_kb_ids:
            kbs = tuple(kb_id for kb_id in kbs if kb_id not in personal_kb_ids)
    allow = frozenset(
        shard for shard in perm.acl_allow_shards if not _PERSONAL_SHARD_RE.match(shard)
    )
    if shards == perm.visible_shard_ids and kbs == perm.visible_kb_ids and allow == perm.acl_allow_shards:
        return perm
    return replace(
        perm,
        visible_shard_ids=shards,
        visible_kb_ids=kbs,
        acl_allow_shards=allow,
    )
