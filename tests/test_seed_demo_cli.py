"""``ekbs.tools.seed_demo`` 的命令行分支。

清库重测的起点是「能登录，但一个知识库都没有」，而带参数的 ``seed_demo`` 会
顺带建一个演示知识库、并把演示语料真的调云端嵌入入库（有 token 成本）。
``--identities-only`` 就是为这个起点准备的，所以它必须满足：

* 建出部门/角色/权限/用户（否则登不进管理台）；
* **不留下任何知识库**（否则重测又是在演示库上做）；
* 不产生文档、分块、分片与向量（否则会真的花钱调云端嵌入）。
"""

from __future__ import annotations

from ekbs.config import load_settings
from ekbs.db import Database

import ekbs.tools.seed_demo as seed_demo

from tests.conftest import DEV_CONFIG


def _settings(tmp_path):
    return load_settings(
        DEV_CONFIG,
        data_dir=tmp_path / "var",
        db_path=tmp_path / "var" / "index" / "ekbs.db",
    )


def test_identities_only_gives_loginable_but_empty_system(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(seed_demo, "load_settings", lambda *a, **k: settings)

    assert seed_demo.main(["--identities-only"]) == 0

    db = Database(settings)
    with db.sync_connect() as conn:
        counts = {
            table: int(conn.execute(f"SELECT count(*) AS c FROM {table}").fetchone()["c"])
            for table in (
                "ekbs_kb",
                "ekbs_document",
                "ekbs_chunk",
                "ekbs_shard",
                "ekbs_user",
                "ekbs_dept",
                "ekbs_role",
                "ekbs_permission",
                "ekbs_user_role",
            )
        }

    assert counts["ekbs_kb"] == 0, "重测起点不该有任何知识库"
    assert counts["ekbs_document"] == 0
    assert counts["ekbs_chunk"] == 0
    assert counts["ekbs_shard"] == 0
    # 身份必须有：否则清库之后没人能登录
    assert counts["ekbs_user"] == 5
    assert counts["ekbs_dept"] == 3
    assert counts["ekbs_role"] == 5
    assert counts["ekbs_permission"] == 63, "权限点数量是 03 §3.4 的契约"
    assert counts["ekbs_user_role"] == 5
    # 没有知识库就不该有分片文件
    assert list((tmp_path / "var" / "index").glob("*.faiss")) == []


def test_identities_only_is_idempotent(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(seed_demo, "load_settings", lambda *a, **k: settings)

    assert seed_demo.main(["--identities-only"]) == 0
    # 第二次跑不该因为唯一约束炸掉，也不该把用户翻倍
    assert seed_demo.main(["--identities-only"]) == 0

    db = Database(settings)
    with db.sync_connect() as conn:
        users = int(conn.execute("SELECT count(*) AS c FROM ekbs_user").fetchone()["c"])
        kbs = int(conn.execute("SELECT count(*) AS c FROM ekbs_kb").fetchone()["c"])
    assert users == 5
    assert kbs == 0
