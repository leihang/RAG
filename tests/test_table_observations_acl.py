"""观测表双写 + structured_query ACL / B–E 验收。"""

from __future__ import annotations

from ekbs.ingest.chunker import TableInput, _table_leaf_blocks
from ekbs.ingest.table_observations import (
    build_observations_for_table,
    build_observations_with_type,
    delete_observations_for_doc,
    write_observations_for_tables,
)
from ekbs.ingest.table_serialize import serialize_information_blocks, serialize_table_body
from ekbs.permission.context import PermContext
from ekbs.retrieval.structured_query import (
    count_observations,
    is_structured_query_intent,
    try_structured_query,
)


def _perm(*, shards: tuple[str, ...]) -> PermContext:
    return PermContext(
        user_id=1,
        dingtalk_userid="u1",
        clearance=2,
        dept_chain=("d1",),
        visible_shard_ids=shards,
        visible_kb_ids=(1,),
        policy_version=1,
    )


def _month_table_html() -> str:
    return (
        "<table>"
        "<tr><th>铺号</th><th>用户名</th><th>年度</th>"
        "<th>1月</th><th>2月</th><th>3月</th><th>4月</th><th>5月</th><th>6月</th></tr>"
        "<tr><td>A-1</td><td>甲商户</td><td>2026年</td>"
        "<td>10</td><td>20</td><td>30</td><td>40</td><td>50</td><td>60</td></tr>"
        "<tr><td>B-2</td><td>乙商户</td><td>2026年</td>"
        "<td>11</td><td>21</td><td>31</td><td>41</td><td>51</td><td>61</td></tr>"
        "</table>"
    )


def _html(matrix: list[list[str]]) -> str:
    parts = ["<table>"]
    for i, row in enumerate(matrix):
        tag = "th" if i == 0 else "td"
        parts.append("<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in row) + "</tr>")
    parts.append("</table>")
    return "".join(parts)


def test_type_a_builds_tidy_observations() -> None:
    rows = [
        ["铺号", "用户名", "年度", "1月", "2月", "3月", "4月", "5月", "6月"],
        ["A-1", "甲商户", "2026年", "10", "20", "30", "40", "50", "60"],
        ["B-2", "乙商户", "2026年", "11", "21", "31", "41", "51", "61"],
    ]
    obs = build_observations_for_table(caption="大户用电", rows=rows, table_id="t1")
    assert obs
    assert all(o.table_type == "A" for o in obs)
    assert len(obs) == 2 * 6
    hit = next(o for o in obs if o.entity_label == "乙商户" and o.period == "2026-06")
    assert hit.value == 61.0
    assert hit.metric == "用电量"


def test_unknown_writes_no_observations() -> None:
    rows = [["项目", "说明"], ["电话", "123"]]
    assert build_observations_for_table(caption="备注", rows=rows) == []


def test_invisible_shard_structured_path_empty(db) -> None:
    """不可见 shard → count=0；无可见分片 structured 拒答。"""
    with db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, default_level, status) "
            "VALUES ('obs-acl', '观测ACL', 2, 1)"
        )
        kb_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, "
            "visible_dept_ids, status, checksum, doc_version) "
            "VALUES (?, '用电', 'a.xlsx', 2, '[]', 'published', 'x', 'v1')",
            (kb_id,),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.commit()

        tables = [
            TableInput(body=_month_table_html(), caption="大户用电监视", table_id="1")
        ]
        written = write_observations_for_tables(
            conn, doc_id=doc_id, shard_id="kb_all_L2", tables=tables
        )
        conn.commit()
        assert written > 0

        visible = _perm(shards=("kb_all_L2",))
        invisible = _perm(shards=("kb_hr_L2",))
        empty = _perm(shards=())

        assert count_observations(conn, perm=visible, doc_id=doc_id) == written
        assert count_observations(conn, perm=invisible, doc_id=doc_id) == 0
        assert count_observations(conn, perm=empty, doc_id=doc_id) == 0

        denied = try_structured_query(conn, "一共多少", empty)
        assert denied is not None
        assert denied.reason == "no_visible_shard"

        # 越权分片不得读到观测（COUNT 意图 → 0）
        sneak = try_structured_query(conn, "有多少条记录", invisible)
        assert sneak is not None
        assert sneak.op == "count"
        assert sneak.value == 0

        delete_observations_for_doc(conn, doc_id)
        conn.commit()
        assert count_observations(conn, perm=visible, doc_id=doc_id) == 0


def test_structured_intent_detection() -> None:
    assert is_structured_query_intent("某户某月电费合计多少")
    assert is_structured_query_intent("有多少条欠费记录")
    assert is_structured_query_intent("营业成本同比多少")
    assert is_structured_query_intent("电费平均多少")
    assert not is_structured_query_intent("9月谁用电更高")
    assert not is_structured_query_intent("制度里怎么规定年假")


def test_month_series_chunks_still_exist_alongside_observations() -> None:
    rows = [
        ["铺号", "用户名", "年度", "1月", "2月", "3月", "4月", "5月", "6月"],
        ["A-1", "甲商户", "2026年", "10", "20", "30", "40", "50", "60"],
    ]
    leaves = serialize_information_blocks(caption="大户用电", rows=rows, row_chunks=True)
    tidy = [b for b in leaves if "月份=" in b and "排名类型=" not in b]
    obs = build_observations_for_table(caption="大户用电", rows=rows)
    assert len(obs) == len(tidy) == 6


def test_fee_detail_sum_and_count(db) -> None:
    body = _html(
        [
            ["户号", "月份", "费用项", "金额"],
            ["H001", "2026-09", "电费", "100"],
            ["H001", "2026-09", "服务费", "50"],
            ["H002", "2026-09", "电费", "80"],
        ]
    )

    with db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, default_level, status) "
            "VALUES ('fee-b', '收费', 2, 1)"
        )
        kb_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, "
            "visible_dept_ids, status, checksum, doc_version) "
            "VALUES (?, '收费明细', 'fee.xlsx', 2, '[]', 'published', 'y', 'v1')",
            (kb_id,),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        write_observations_for_tables(
            conn,
            doc_id=doc_id,
            shard_id="kb_all_L2",
            tables=[TableInput(body=body, caption="收费明细", table_id="1")],
        )
        conn.commit()
        perm = _perm(shards=("kb_all_L2",))
        summ = try_structured_query(conn, "户号H001 9月合计多少", perm)
        assert summ is not None and not summ.is_na
        assert float(summ.value) == 150.0
        # 费项等值过滤：只加电费
        elec = try_structured_query(conn, "户号H001 9月电费合计多少", perm)
        assert elec is not None and not elec.is_na
        assert float(elec.value) == 100.0
        assert elec.metric == "电费"
        cnt = try_structured_query(conn, "有多少条欠费记录", perm)
        assert cnt is not None and cnt.op == "count"
        assert int(cnt.value) == 3
        avg = try_structured_query(conn, "电费平均多少", perm)
        assert avg is not None and avg.op == "avg"
        assert float(avg.value) == 90.0  # (100+80)/2
        # ACL：不可见分片不得读到 B 观测
        denied = try_structured_query(conn, "户号H001 9月合计多少", _perm(shards=()))
        assert denied is not None and denied.reason == "no_visible_shard"


def test_financial_printed_total_and_yoy_na(db) -> None:
    """印刷合计优先于明细重算；无同比列严禁用本期/上期推算。"""
    body = _html(
        [
            ["科目", "本期金额", "本年累计", "上期金额"],
            # 明细与印刷合计刻意不一致：若误 SUM 明细会得到 80，正确应抄 100
            ["营业成本", "80", "800", "70"],
            ["管理费用", "50", "500", "40"],
            ["营业成本合计", "100", "1000", "90"],
        ]
    )
    with db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, default_level, status) "
            "VALUES ('fin-c', '财报', 2, 1)"
        )
        kb_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, "
            "visible_dept_ids, status, checksum, doc_version) "
            "VALUES (?, '利润表', 'pl.xlsx', 2, '[]', 'published', 'z', 'v1')",
            (kb_id,),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        write_observations_for_tables(
            conn,
            doc_id=doc_id,
            shard_id="kb_all_L2",
            tables=[TableInput(body=body, caption="利润表", table_id="1")],
        )
        conn.commit()
        roles = {
            r["line_role"]
            for r in conn.execute(
                "SELECT DISTINCT line_role FROM ekbs_table_observation "
                "WHERE doc_id = ? AND table_type = 'C'",
                (doc_id,),
            )
        }
        assert "total" in roles and "normal" in roles

        total = try_structured_query(
            conn, "营业成本合计是多少", _perm(shards=("kb_all_L2",))
        )
        assert total is not None
        assert total.op == "printed_total"
        assert float(total.value) == 100.0  # 本期印刷合计，不是明细 80
        assert "营业成本合计" in (total.entity_key or total.metric)

        ytd = try_structured_query(
            conn, "营业成本本年累计合计是多少", _perm(shards=("kb_all_L2",))
        )
        assert ytd is not None and ytd.op == "printed_total"
        assert float(ytd.value) == 1000.0
        assert "本年累计" in (ytd.period or "")

        # 有本期/上期但无同比列 → N/A，不得算出 (100-90)/90
        yoy = try_structured_query(
            conn, "营业成本同比多少", _perm(shards=("kb_all_L2",))
        )
        assert yoy is not None
        assert yoy.op == "yoy_na"
        assert yoy.is_na
        assert yoy.reason == "yoy_column_missing"


def test_financial_copies_printed_yoy_column_when_present(db) -> None:
    body = _html(
        [
            ["项目", "本期金额", "同比"],
            ["营业收入", "200", "0.12"],
            ["营业收入合计", "200", "0.12"],
        ]
    )
    with db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, default_level, status) "
            "VALUES ('fin-yoy', '财报', 2, 1)"
        )
        kb_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, "
            "visible_dept_ids, status, checksum, doc_version) "
            "VALUES (?, '利润表', 'pl2.xlsx', 2, '[]', 'published', 'y', 'v1')",
            (kb_id,),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        write_observations_for_tables(
            conn,
            doc_id=doc_id,
            shard_id="kb_all_L2",
            tables=[TableInput(body=body, caption="利润表", table_id="2")],
        )
        conn.commit()
        hit = try_structured_query(
            conn, "营业收入同比多少", _perm(shards=("kb_all_L2",))
        )
        assert hit is not None
        assert not hit.is_na
        assert float(hit.value) == 0.12
        assert hit.extras.get("yoy_copied") is True


def test_ledger_lookup_and_filter(db) -> None:
    body = _html(
        [
            ["合同号", "乙方", "状态"],
            ["HT-001", "甲公司", "在用"],
            ["HT-002", "乙公司", "停用"],
            ["HT-003", "丙公司", "在用"],
        ]
    )
    with db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, default_level, status) "
            "VALUES ('led-d', '台账', 2, 1)"
        )
        kb_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, "
            "visible_dept_ids, status, checksum, doc_version) "
            "VALUES (?, '合同台账', 'led.xlsx', 2, '[]', 'published', 'w', 'v1')",
            (kb_id,),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        write_observations_for_tables(
            conn,
            doc_id=doc_id,
            shard_id="kb_all_L2",
            tables=[TableInput(body=body, caption="合同台账", table_id="1")],
        )
        conn.commit()
        look = try_structured_query(
            conn, "合同 HT-001 的乙方是谁", _perm(shards=("kb_all_L2",))
        )
        assert look is not None
        assert look.value == "甲公司"
        filt = try_structured_query(
            conn, "状态=在用 有哪些", _perm(shards=("kb_all_L2",))
        )
        assert filt is not None
        assert int(filt.value) == 2
        assert "HT-001" in (filt.surface or "") and "HT-003" in (filt.surface or "")
        assert "HT-002" not in (filt.surface or "")


def test_ledger_asset_lookup_from_docx_style_table(db) -> None:
    """docx/pdf 抽出的 HTML 表同样可写观测并查找（不限 xlsx）。"""
    body = _html(
        [
            ["资产编号", "资产名称", "位置", "状态"],
            ["AS-9", "冷水机组", "地下室", "在用"],
            ["AS-10", "水泵", "机房", "停用"],
        ]
    )
    with db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, default_level, status) "
            "VALUES ('led-asset', '台账', 2, 1)"
        )
        kb_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        # source_path 伪装成 Word：双写逻辑不应再按后缀拒绝
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, "
            "visible_dept_ids, status, checksum, doc_version) "
            "VALUES (?, '设备台账', 'var/raw/gongkai/设备台账.docx', 2, '[]', "
            "'published', 'wa', 'v1')",
            (kb_id,),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        n = write_observations_for_tables(
            conn,
            doc_id=doc_id,
            shard_id="kb_all_L2",
            tables=[TableInput(body=body, caption="设备台账", table_id="t1")],
            source_path="var/raw/gongkai/设备台账.docx",
        )
        assert n >= 2
        conn.commit()
        hit = try_structured_query(
            conn, "资产编号 AS-9 的位置是什么", _perm(shards=("kb_all_L2",))
        )
        assert hit is not None
        assert hit.value == "地下室"


def test_wide_bill_observations_melt() -> None:
    rows = [
        [
            "序号",
            "水卡号",
            "用途",
            "上月起度/2024-01-01",
            "本月止度/2024-01-01",
            "用量/2024-01-01",
            "金额/2024-01-01",
            "上月起度/2024-02-01",
            "本月止度/2024-02-01",
            "用量/2024-02-01",
            "金额/2024-02-01",
        ],
        ["1", "30005", "绿化", "100", "110", "10", "30", "110", "125", "15", "45"],
    ]
    ttype, obs = build_observations_with_type(
        caption="水费账单", body=_html(rows), table_id="1"
    )
    assert ttype == "E"
    # 每月金额+用量 → 至少 4 条
    assert len(obs) >= 4
    assert all(o.entity_key == "30005" for o in obs)
    jan = [o for o in obs if o.period == "2024-01"]
    feb = [o for o in obs if o.period == "2024-02"]
    assert any(o.metric == "金额" and o.value == 30.0 for o in jan)
    assert any(o.metric == "用量" and o.value == 10.0 for o in jan)
    assert any(o.metric == "金额" and o.value == 45.0 for o in feb)


def test_wide_bill_structured_amount_by_card_and_month(db) -> None:
    """验收：某水卡某月金额走观测表，不串到其它指标组。"""
    rows = [
        [
            "序号",
            "水卡号",
            "用途",
            "上月起度/2024-01-01",
            "本月止度/2024-01-01",
            "用量/2024-01-01",
            "金额/2024-01-01",
            "上月起度/2024-02-01",
            "本月止度/2024-02-01",
            "用量/2024-02-01",
            "金额/2024-02-01",
        ],
        ["1", "30005", "绿化", "100", "110", "10", "30", "110", "125", "15", "45"],
        ["2", "30006", "消防", "0", "20", "20", "60", "20", "50", "30", "90"],
    ]
    with db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, default_level, status) "
            "VALUES ('bill-e', '账单', 2, 1)"
        )
        kb_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, "
            "visible_dept_ids, status, checksum, doc_version) "
            "VALUES (?, '水费账单', 'water.xlsx', 2, '[]', 'published', 'e1', 'v1')",
            (kb_id,),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        write_observations_for_tables(
            conn,
            doc_id=doc_id,
            shard_id="kb_all_L2",
            tables=[
                TableInput(body=_html(rows), caption="水费账单", table_id="w1")
            ],
        )
        conn.commit()
        perm = _perm(shards=("kb_all_L2",))
        jan_amt = try_structured_query(
            conn, "水卡号30005 2024年1月金额是多少", perm
        )
        assert jan_amt is not None and not jan_amt.is_na
        assert float(jan_amt.value) == 30.0
        feb_usage = try_structured_query(
            conn, "水卡 30005 2月用量多少", perm
        )
        assert feb_usage is not None and not feb_usage.is_na
        assert float(feb_usage.value) == 15.0
        # 合计两月金额，不串用量
        total = try_structured_query(
            conn, "水卡号30005 金额合计多少", perm
        )
        assert total is not None and float(total.value) == 75.0
        other = try_structured_query(
            conn, "水卡号30006 2024年1月金额多少", perm
        )
        assert other is not None and float(other.value) == 60.0


def test_wide_bill_lookup_by_usage_label(db) -> None:
    """「1期绿化总水」在 attrs.用途，entity_key 是水卡号 —— 按用途仍应命中当月金额。"""
    from ekbs.retrieval.structured_query import execute_structured_plan

    rows = [
        [
            "序号",
            "水卡号",
            "用途",
            "上月起度/2026-08-01",
            "本月止度/2026-08-01",
            "用量/2026-08-01",
            "金额/2026-08-01",
            "上月起度/2026-09-01",
            "本月止度/2026-09-01",
            "用量/2026-09-01",
            "金额/2026-09-01",
        ],
        [
            "2",
            "30005020066",
            "1期绿化总水",
            "50",
            "100",
            "50",
            "200",
            "100",
            "1137",
            "1037",
            "4718.35",
        ],
        [
            "3",
            "30005020081",
            "1期消防用水",
            "0",
            "100",
            "100",
            "400",
            "100",
            "264",
            "164",
            "746.20",
        ],
    ]
    with db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, default_level, status) "
            "VALUES ('bill-usage', '账单用途', 2, 1)"
        )
        kb_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, "
            "visible_dept_ids, status, checksum, doc_version) "
            "VALUES (?, '水费账单', 'water-usage.xlsx', 2, '[]', 'published', 'u1', 'v1')",
            (kb_id,),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        write_observations_for_tables(
            conn,
            doc_id=doc_id,
            shard_id="kb_all_L2",
            tables=[
                TableInput(body=_html(rows), caption="水费账单", table_id="wu1")
            ],
        )
        conn.commit()
        perm = _perm(shards=("kb_all_L2",))
        hit = execute_structured_plan(
            conn,
            perm,
            op="extract",
            entity="1期绿化总水",
            period="2026-09",
            metric="水费",
            question="2026年9月1期绿化水费是多少",
        )
        assert hit is not None and not hit.is_na, hit
        assert float(hit.value) == 4718.35
        # 问句实体抽成「1期绿化」（无「总水」）也应命中用途
        by_q = try_structured_query(
            conn, "2026年9月1期绿化水费是多少", perm
        )
        assert by_q is not None and not by_q.is_na, by_q
        assert float(by_q.value) == 4718.35


def test_month_range_bare_entity_electricity_sum(db) -> None:
    """钉钉错答回归：「嘛嘛公寓2026年1-9月用电量合计」不得把「1-9月」吃成仅 9 月、
    也不得缺实体/指标时扫整库；应按 label + 用电量 + 月区间 SUM。"""
    from ekbs.retrieval.structured_query import (
        _entity_from_question,
        _metric_from_question,
        _period_from_question,
        is_structured_query_intent,
    )

    assert _period_from_question("嘛嘛公寓2026年1-9月的用电量合计") == "2026-01..2026-09"
    assert _period_from_question("2026年1—9月用电量") == "2026-01..2026-09"
    assert _entity_from_question("嘛嘛公寓2026年1-9月的用电量合计") == "嘛嘛公寓"
    assert _metric_from_question("嘛嘛公寓2026年1-9月的用电量合计") == "用电量"
    assert is_structured_query_intent("三期嘛嘛公寓2026年1—9月累计用电量是多少")

    rows = [
        ["铺号", "用户名", "年度", "1月", "2月", "3月", "4月", "5月", "6月", "9月", "10月"],
        ["M1", "嘛嘛公寓", "2026年", "100", "200", "0", "0", "0", "0", "300", "999"],
        ["M2", "嘛嘛公寓", "2026年", "10", "20", "0", "0", "0", "0", "30", "99"],
        ["X1", "其它公寓", "2026年", "1000", "1000", "1000", "1000", "1000", "1000", "1000", "1000"],
    ]
    with db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, default_level, status) "
            "VALUES ('mama-a', '能耗', 2, 1)"
        )
        kb_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, "
            "visible_dept_ids, status, checksum, doc_version) "
            "VALUES (?, '能耗分析', 'e.xlsx', 2, '[]', 'published', 'mama1', 'v1')",
            (kb_id,),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        write_observations_for_tables(
            conn,
            doc_id=doc_id,
            shard_id="kb_all_L2",
            tables=[
                TableInput(body=_html(rows), caption="大户用电监视", table_id="a1")
            ],
        )
        conn.commit()
        perm = _perm(shards=("kb_all_L2",))
        # 裸合计无实体无指标 → underspecified，禁止扫库
        bare = try_structured_query(conn, "合计多少", perm)
        assert bare is not None and bare.reason == "underspecified"
        hit = try_structured_query(
            conn, "嘛嘛公寓2026年1-9月的用电量合计", perm
        )
        assert hit is not None and not hit.is_na
        # 1–9 月两表（含中间 0）：100+200+300 + 10+20+30 = 660；不得含 10 月 999/99
        assert float(hit.value) == 660.0
        assert hit.metric == "用电量"
        assert hit.entity_key == "嘛嘛公寓"
        assert hit.period == "2026-01..2026-09"
        assert hit.rows_touched == 14  # 两表 × (1–6 月 + 9 月)


def test_month_only_electricity_pins_to_latest_year_not_cross_year_sum(db) -> None:
    """「三期嘛嘛公寓2月用电量是多少」无年份时，不得把多年 2 月 SUM 成假合计。

    大户监视已有单月现成数；跨年相加（如 59744+48948+29905）是错答案。
    """
    rows = [
        ["铺号", "用户名", "年度", "1月", "2月", "3月", "4月", "5月", "6月"],
        ["三期3幢表号SG-1", "嘛嘛公寓", "2024年", "1", "59744", "3", "4", "5", "6"],
        ["三期3幢表号SG-1", "嘛嘛公寓", "2025年", "1", "48948", "3", "4", "5", "6"],
        ["三期3幢表号SG-1", "嘛嘛公寓", "2026年", "1", "29905", "3", "4", "5", "6"],
        ["四期总表X", "嘛嘛公寓", "2026年", "1", "23586", "3", "4", "5", "6"],
    ]
    with db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, default_level, status) "
            "VALUES ('mama-feb', '能耗', 2, 1)"
        )
        kb_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, "
            "visible_dept_ids, status, checksum, doc_version) "
            "VALUES (?, '大户用电', 'f.xlsx', 2, '[]', 'published', 'feb1', 'v1')",
            (kb_id,),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        write_observations_for_tables(
            conn,
            doc_id=doc_id,
            shard_id="kb_all_L2",
            tables=[
                TableInput(body=_html(rows), caption="大户用电监视", table_id="a1")
            ],
        )
        conn.commit()
        perm = _perm(shards=("kb_all_L2",))
        hit = try_structured_query(conn, "三期嘛嘛公寓2月用电量是多少", perm)
        assert hit is not None and not hit.is_na
        assert float(hit.value) == 29905.0
        assert hit.period == "2026-02"
        assert hit.rows_touched == 1
        assert (hit.extras or {}).get("period_pinned_from") == "02"


def test_meter_reading_price_classifies_electric_and_sums_by_bearer() -> None:
    """户内抄表明细：按名取列；单价 <1.5 判电（总表无「电表」二字也不得漏）；
    「0.04,0.77」双价可解析；退铺→0；按费用承担对象聚合。"""
    from ekbs.ingest.table_observations import (
        _classify_utility_kind,
        _parse_price_cells,
        _parse_usage_cell,
        _period_from_meter_date,
        build_observations_with_type,
    )

    assert _parse_usage_cell("退铺") == 0.0
    assert _parse_price_cells("0.04,0.77") == [0.04, 0.77]
    assert _classify_utility_kind("SG-0601184986（嘛嘛公寓总表）", [0.54]) == "electric"
    assert _classify_utility_kind("205086056024空调", [0.77]) == "electric"
    assert _classify_utility_kind("绿化用水", [4.55]) == "water"
    assert _period_from_meter_date("2026-10-01") == "2026-09"

    headers = [
        "项目名称",
        "资源编号",
        "费用承担对象",
        "表计编号",
        "表计名称",
        "抄表日期",
        "起数",
        "止数",
        "旧表用量",
        "倍率",
        "损耗",
        "合计用量",
        "单价（元）",
        "金额（元）",
        "抄表来源",
        "操作人",
        "操作时间",
    ]
    rows = [
        headers,
        # 总表：表名无「电表」，靠单价 0.54 判电
        [
            "熙街",
            "x",
            "重庆嘛嘛公寓管理有限公司",
            "SG-1",
            "SG-0601184986（嘛嘛公寓总表）",
            "2026-10-01",
            "0",
            "0",
            "",
            "1",
            "",
            "58678",
            "0.54",
            "100",
            "",
            "a",
            "t",
        ],
        [
            "熙街",
            "x",
            "重庆嘛嘛公寓管理有限公司",
            "SG-2",
            "1300012750455总表",
            "2026-10-01",
            "0",
            "0",
            "",
            "1",
            "",
            "26070",
            "0.54",
            "50",
            "",
            "a",
            "t",
        ],
        # 水：单价 4.55 不得进用电量
        [
            "熙街",
            "x",
            "重庆嘛嘛公寓管理有限公司",
            "W-1",
            "公区水表",
            "2026-10-01",
            "0",
            "0",
            "",
            "1",
            "",
            "999",
            "4.55",
            "10",
            "",
            "a",
            "t",
        ],
        # 双价电
        [
            "熙街",
            "x",
            "重庆全宜酒店管理有限公司",
            "E-1",
            "原表6000014换新",
            "2026-10-01",
            "0",
            "0",
            "",
            "1",
            "",
            "17660",
            "0.04,0.77",
            "1",
            "",
            "a",
            "t",
        ],
        # 退铺
        [
            "熙街",
            "x",
            "重庆全宜酒店管理有限公司",
            "E-2",
            "电表",
            "2026-10-01",
            "0",
            "0",
            "",
            "1",
            "",
            "退铺",
            "0.84",
            "0",
            "",
            "a",
            "t",
        ],
    ]
    ttype, obs = build_observations_with_type(
        caption="模板", rows=rows, table_id="m1", header_row_count=1
    )
    assert ttype == "B"
    elec = [o for o in obs if o.metric == "用电量"]
    assert sum(o.value or 0 for o in elec if "嘛嘛" in o.entity_label) == 84748.0
    assert all(o.period == "2026-09" for o in elec)
    assert sum(o.value or 0 for o in elec if "全宜" in o.entity_label) == 17660.0
    assert not any(o.metric == "用电量" and (o.value or 0) == 999 for o in obs)


def test_phase_entity_and_prefer_doc_relax(db) -> None:
    """「三期嘛嘛公寓」要跨列合取（key 含三期、label 含嘛嘛公寓）；
    prefer_doc 指到无观测文档时须回退全分片，避免 9 月分析抢走 1–9 月合计。"""
    with db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, default_level, status) "
            "VALUES ('phase-a', '能耗', 2, 1)"
        )
        kb_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, "
            "visible_dept_ids, status, checksum, doc_version) "
            "VALUES (?, '宽表', 'w.xlsx', 2, '[]', 'published', 'ph1', 'v1')",
            (kb_id,),
        )
        doc_obs = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, "
            "visible_dept_ids, status, checksum, doc_version) "
            "VALUES (?, '9月分析', 'sep.xlsx', 2, '[]', 'published', 'ph2', 'v1')",
            (kb_id,),
        )
        doc_sep = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        # 贴近生产：key=三期3幢… / 四期总表…，label=嘛嘛公寓
        rows = [
            ("三期3幢表号SG-3", "嘛嘛公寓", "2026-01", 100.0),
            ("三期3幢表号SG-3", "嘛嘛公寓", "2026-02", 200.0),
            ("三期3幢表号SG-3", "嘛嘛公寓", "2026-09", 50.0),
            ("四期总表SG-4", "嘛嘛公寓", "2026-01", 10.0),
            ("四期总表SG-4", "嘛嘛公寓", "2026-02", 20.0),
            ("四期总表SG-4", "嘛嘛公寓", "2026-09", 5.0),
        ]
        for key, label, period, value in rows:
            conn.execute(
                "INSERT INTO ekbs_table_observation("
                "doc_id, shard_id, table_id, table_type, entity_key, entity_label, "
                "metric, period, value, value_surface, line_role, source_row, chunk_id"
                ") VALUES (?, 'kb_all_L2', 'p1', 'A', ?, ?, '用电量', ?, ?, ?, 'detail', 0, NULL)",
                (doc_obs, key, label, period, value, str(value)),
            )
        conn.commit()
        perm = _perm(shards=("kb_all_L2",))
        hit = try_structured_query(
            conn, "三期嘛嘛公寓2026年1-9月的用电量合计", perm
        )
        assert hit is not None and not hit.is_na
        assert float(hit.value) == 350.0
        assert hit.rows_touched == 3
        hit2 = try_structured_query(
            conn,
            "三期嘛嘛公寓2026年1-9月的用电量合计",
            perm,
            prefer_doc_ids=[doc_sep],
        )
        assert hit2 is not None and not hit2.is_na
        assert float(hit2.value) == 350.0
        assert (hit2.extras or {}).get("prefer_doc_relaxed") is True


def test_leaf_sample_budget_for_fee_detail() -> None:
    parts = ["<table><tr><th>户号</th><th>月份</th><th>金额</th></tr>"]
    for i in range(250):
        parts.append(f"<tr><td>H{i:04d}</td><td>2026-09</td><td>{i}</td></tr>")
    parts.append("</table>")
    body = "".join(parts)
    table = TableInput(body=body, caption="收费明细", table_id="1")
    full = serialize_table_body(caption=table.caption, body=body, table_id="1")
    leaves = _table_leaf_blocks(
        table,
        full,
        row_chunks=True,
        leaf_policy="auto",
        leaf_sample_max=64,
        leaf_sample_min_rows=200,
    )
    assert len(leaves) <= 64
    assert len(leaves) < 250
    # 摘要叶存在，便于向量定位；全量仍写观测
    assert any("数据行数=250" in leaf for leaf in leaves)
    ttype, obs = build_observations_with_type(
        caption="收费明细", body=body, table_id="1"
    )
    assert ttype == "B"
    assert len(obs) == 250
    assert len(leaves) < len(obs)


def test_hash_switchroom_loss_not_whole_table_sum(db) -> None:
    """「二期15#专配室…变压器损耗合计」必须抽出含 # 的地点，禁止全表 SUM。"""
    import json

    with db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, default_level, status) "
            "VALUES ('xfmr3', '能耗', 2, 1)"
        )
        kb_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, "
            "visible_dept_ids, status, checksum, doc_version) "
            "VALUES (?, '2026年9月能耗分析', 't15.xlsx', 2, '[]', 'published', 'xf15', 'v1')",
            (kb_id,),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        # 目标专配室 2 台 + 其它地点干扰行 + 全表汇总行
        samples = [
            ("3", "二期15#专配室", "-2744"),
            ("4", "二期15#专配室", "3150"),
            ("5", "二期18#专配室", "9999"),
            ("", "", "91064.8"),
        ]
        for seq, loc, loss in samples:
            attrs = json.dumps(
                {"序号": seq, "安装地点": loc, "损耗": loss},
                ensure_ascii=False,
            )
            conn.execute(
                "INSERT INTO ekbs_table_observation("
                "doc_id, shard_id, table_id, table_type, entity_key, entity_label, "
                "period, metric, value, value_surface, attrs_json) "
                "VALUES (?, 'kb_all_L2', '1', 'B', ?, '', '', '变压器损耗', NULL, '', ?)",
                (doc_id, seq or "agg", attrs),
            )
        conn.commit()
        perm = _perm(shards=("kb_all_L2",))
        hit = try_structured_query(
            conn, "二期15#专配室2026年9月变压器损耗合计", perm
        )
        assert hit is not None and not hit.is_na
        assert float(hit.value) == 406.0  # -2744 + 3150
        assert hit.entity_key == "二期15#专配室"
        bare = try_structured_query(conn, "2026年9月变压器损耗合计", perm)
        assert bare is not None and bare.reason == "underspecified"


def test_switchroom_transformer_loss_uses_attrs_and_doc_title_period(db) -> None:
    """「1期4号配电房2026年9月变压器损耗」：地点别名 + attrs.损耗 + 标题账期。"""
    import json

    with db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, default_level, status) "
            "VALUES ('xfmr2', '能耗', 2, 1)"
        )
        kb_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, "
            "visible_dept_ids, status, checksum, doc_version) "
            "VALUES (?, '2026年9月能耗分析', 't9.xlsx', 2, '[]', 'published', 'xf9', 'v1')",
            (kb_id,),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        for seq, loss in (("1", "-2569"), ("2", "-2802")):
            attrs = json.dumps(
                {
                    "序号": seq,
                    "安装地点": "一期4#专配室",
                    "额定容量": "1600KVA",
                    "损耗": loss,
                    "理论值": "5814",
                },
                ensure_ascii=False,
            )
            conn.execute(
                "INSERT INTO ekbs_table_observation("
                "doc_id, shard_id, table_id, table_type, entity_key, entity_label, "
                "period, metric, value, value_surface, attrs_json) "
                "VALUES (?, 'kb_all_L2', '1', 'B', ?, '', '', '变压器损耗', NULL, '', ?)",
                (doc_id, seq, attrs),
            )
        conn.commit()
        perm = _perm(shards=("kb_all_L2",))
        hit = try_structured_query(
            conn, "1期4号配电房2026年9月变压器损耗是多少", perm
        )
        assert hit is not None and not hit.is_na
        assert float(hit.value) == -5371.0
        assert hit.metric == "变压器损耗"
        assert hit.period == "2026-09"


def test_equipment_unit_count_dedups_by_serial_not_row_count(db) -> None:
    """「一共有多少台变压器」按序号去重，禁止把损耗明细行数当成台数。"""
    import json

    with db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, default_level, status) "
            "VALUES ('xfmr', '能耗', 2, 1)"
        )
        kb_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, "
            "visible_dept_ids, status, checksum, doc_version) "
            "VALUES (?, '变压器损耗', 't.xlsx', 2, '[]', 'published', 'xf1', 'v1')",
            (kb_id,),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        # 2 台设备 × 2 期明细 = 4 行；正确台数是 2，不是 4
        for seq, period in (("1", "2026-08"), ("1", "2026-09"), ("2", "2026-08"), ("2", "2026-09")):
            attrs = json.dumps(
                {"序号": seq, "安装地点": f"配室{seq}", "额定容量": "1600KVA"},
                ensure_ascii=False,
            )
            conn.execute(
                "INSERT INTO ekbs_table_observation("
                "doc_id, shard_id, table_id, table_type, entity_key, entity_label, "
                "period, metric, value, value_surface, attrs_json) "
                "VALUES (?, 'kb_all_L2', '1', 'B', ?, '', ?, '变压器损耗', 100.0, '100', ?)",
                (doc_id, seq, period, attrs),
            )
        conn.commit()
        perm = _perm(shards=("kb_all_L2",))
        hit = try_structured_query(conn, "一共有多少台变压器", perm)
        assert hit is not None and hit.op == "count"
        assert not hit.is_na
        assert int(hit.value) == 2
        bare = try_structured_query(conn, "一共有多少台", perm)
        assert bare is not None and bare.reason == "underspecified"
