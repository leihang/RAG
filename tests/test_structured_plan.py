"""结构化规划：LLM 指令解析 + 观测表执行 + 答案数字核验。"""

from __future__ import annotations

from ekbs.ingest.table_observations import (
    _metric_name_from_header,
    _period_from_metric_header,
)
from ekbs.permission.context import PermContext
from ekbs.retrieval.structured_plan import (
    STRUCTURED_PLAN_REPAIR_MAX,
    StructuredPlan,
    build_planner_repair_user_message,
    parse_structured_plan,
    verify_answer_uses_hit,
)
from ekbs.retrieval.structured_query import StructuredQueryHit, execute_structured_plan


def _perm(*, shards: tuple[str, ...] = ("kb_all_L1",)) -> PermContext:
    return PermContext(
        user_id=1,
        dingtalk_userid="u1",
        clearance=2,
        dept_chain=("d1",),
        visible_shard_ids=shards,
        visible_kb_ids=(1,),
        policy_version=1,
    )


def test_period_from_metric_header_tolerates_slash_in_unit() -> None:
    """``用量(KW/h)/2026-02`` 不得截成 ``h)/2026-02``。"""
    assert _period_from_metric_header("用量(KW/h)/2026-02") == "2026-02"
    assert _period_from_metric_header("金额（元）/2026-02") == "2026-02"
    assert _metric_name_from_header("用量(KW/h)/2026-02", "用量") == "用量(KW/h)"
    assert _metric_name_from_header("金额（元）/2026-02", "金额") == "金额（元）"


def test_parse_structured_plan_accepts_lookup_json() -> None:
    plan = parse_structured_plan(
        '{"op":"lookup","entity":"差异","period":"2026-02",'
        '"metric":"金额（元）","attrs_contains":"用电","reason":"差异电费"}'
    )
    assert plan is not None
    # lookup 归一为 extract（四类主 op）
    assert plan.op == "extract"
    assert plan.entity == "差异"
    assert plan.period == "2026-02"
    assert plan.attrs_contains == "用电"
    assert parse_structured_plan('{"op":"na","reason":"不知道"}') is None


def test_parse_extract_with_columns() -> None:
    plan = parse_structured_plan(
        '{"op":"extract","entity":"公共","period":"2026-09",'
        '"metric":"用电量","attrs_contains":"公共",'
        '"columns":["项目","用量"],"reason":"明细"}'
    )
    assert plan is not None
    assert plan.op == "extract"
    assert plan.columns == ["项目", "用量"]


def test_planner_repair_message_includes_failure_and_headers() -> None:
    assert STRUCTURED_PLAN_REPAIR_MAX == 5
    failed = StructuredPlan(
        op="sum",
        entity="工程费用",
        period="2026-02",
        metric="费用",
        columns=["预算额度"],
        reason="合计",
    )
    msg = build_planner_repair_user_message(
        "2026年预算工程费用2月预算额度为多少",
        ["相关表头: 2月 | 2026年度计划金额\n项目=电梯维保费; 2月=37500"],
        failed_plan=failed,
        fail_reason="not_found",
        attempt=2,
        max_attempts=STRUCTURED_PLAN_REPAIR_MAX,
        history=[{"op": "sum", "fail_reason": "not_found"}],
    )
    assert "修正轮次：2/5" in msg
    assert "失败原因：not_found" in msg
    assert "此前已失败的指令" in msg
    assert '"op": "sum"' in msg or '"op":"sum"' in msg.replace(" ", "")
    assert "预算额度" in msg
    assert "2月=37500" in msg
    assert "问题：2026年预算工程费用2月预算额度为多少" in msg


def test_verify_answer_uses_hit_requires_surface() -> None:
    hit = StructuredQueryHit(value=270.98, surface="270.98", op="lookup")
    assert verify_answer_uses_hit("熙丰2026年2月差异电费为 270.98 元[1]", hit)
    assert not verify_answer_uses_hit("资料只有3月", hit)


def test_execute_value_lookup_with_attrs(db) -> None:
    """attrs 消歧：同 period 水电差异共存时只取用电。"""
    with db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, default_level, status) "
            "VALUES ('plan-t', '规划测', 2, 1)"
        )
        kb_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, "
            "visible_dept_ids, status, checksum, doc_version) "
            "VALUES (?, '能耗', 'a.xlsx', 2, '[]', 'published', 'p', 'v1')",
            (kb_id,),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        for value, caption in (
            (270.98, "熙丰用电统计分析"),
            (929.85, "熙丰用水统计分析"),
        ):
            conn.execute(
                "INSERT INTO ekbs_table_observation ("
                "doc_id, shard_id, table_id, table_type, entity_key, entity_label, "
                "period, metric, value, value_surface, line_role, attrs_json"
                ") VALUES (?, 'kb_all_L1', 't1', 'E', '差异', '差异', '2026-02', "
                "'金额（元）', ?, ?, 'normal', ?)",
                (doc_id, value, str(value), f'{{"caption": "{caption}"}}'),
            )
        conn.commit()
        hit = execute_structured_plan(
            conn,
            _perm(),
            op="lookup",
            entity="差异",
            period="2026-02",
            metric="金额（元）",
            attrs_contains="用电",
        )
    assert hit is not None
    assert not hit.is_na
    assert float(hit.value) == 270.98


def test_numeric_mode_structured_plan_requires_spreadsheet() -> None:
    """旁路唯一规则：即使 numeric 模式，也必须命中电子表格原件。"""
    from ekbs.retrieval.numeric import MODE_NUMERIC, MODE_PROSE
    from ekbs.retrieval.pipeline import RetrievalPipeline, RetrievalResult, RetrievedChunk

    pipe = RetrievalPipeline.__new__(RetrievalPipeline)
    empty = RetrievalResult(query="q", chunks=[])
    ppt = RetrievalResult(
        query="q",
        chunks=[
            RetrievedChunk(
                chunk_id=1,
                doc_id=1,
                shard_id="s",
                content="公司承担 -19.47%",
                chunk_type="parent",
                source_path="/raw/汇报.pptx",
            )
        ],
    )
    xlsx = RetrievalResult(
        query="q",
        chunks=[
            RetrievedChunk(
                chunk_id=2,
                doc_id=2,
                shard_id="s",
                content="金额=270.98",
                chunk_type="table",
                source_path="/raw/能耗.xlsx",
            )
        ],
    )
    assert not pipe._should_try_structured_plan("随便问个数", empty, MODE_NUMERIC)
    assert not pipe._should_try_structured_plan("年假怎么请", empty, MODE_PROSE)
    assert not pipe._should_try_structured_plan(
        "2026年1-9月公司承担电费变化", ppt, MODE_NUMERIC
    )
    assert pipe._should_try_structured_plan("熙丰2026年2月差异电费", xlsx, MODE_PROSE)
    assert pipe._should_try_structured_plan("随便问个数", xlsx, MODE_NUMERIC)


def test_rule_diff_fee_lookup_uses_attrs(db) -> None:
    """规则路径：「差异电费」→ lookup 差异 + attrs 用电，不误 SUM 熙丰。"""
    from ekbs.retrieval.structured_query import try_structured_query

    with db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, default_level, status) "
            "VALUES ('plan-diff', '差异测', 2, 1)"
        )
        kb_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, "
            "visible_dept_ids, status, checksum, doc_version) "
            "VALUES (?, '能耗', 'b.xlsx', 2, '[]', 'published', 'p2', 'v1')",
            (kb_id,),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        for value, caption in (
            (270.98, "熙丰用电统计分析"),
            (929.85, "熙丰用水统计分析"),
        ):
            conn.execute(
                "INSERT INTO ekbs_table_observation ("
                "doc_id, shard_id, table_id, table_type, entity_key, entity_label, "
                "period, metric, value, value_surface, line_role, attrs_json"
                ") VALUES (?, 'kb_all_L1', 't1', 'E', '差异', '差异', '2026-02', "
                "'金额（元）', ?, ?, 'normal', ?)",
                (doc_id, value, str(value), f'{{"caption": "{caption}"}}'),
            )
        conn.commit()
        hit = try_structured_query(
            conn, "熙丰2026年2月的差异电费是多少", _perm()
        )
    assert hit is not None
    assert not hit.is_na
    assert float(hit.value) == 270.98
    assert hit.op == "lookup"


def test_period_first_entity_and_avg_unit_price(db) -> None:
    """「2025年1-3月锅炉燃气单价平均值」→ entity=锅炉，avg 单价，不得抽成「2025年」。"""
    from ekbs.retrieval.structured_query import (
        _entity_from_question,
        try_structured_query,
    )

    assert _entity_from_question("2025年1-3月锅炉燃气单价平均值") == "锅炉"
    assert _entity_from_question("嘛嘛公寓2026年1-9月的用电量合计") == "嘛嘛公寓"

    with db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, default_level, status) "
            "VALUES ('avg-gas', '均价测', 2, 1)"
        )
        kb_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, "
            "visible_dept_ids, status, checksum, doc_version) "
            "VALUES (?, '气费', 'g.xlsx', 2, '[]', 'published', 'g1', 'v1')",
            (kb_id,),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        for period, value in (("2025-01", 3.295), ("2025-02", 3.295), ("2025-03", 3.295)):
            conn.execute(
                "INSERT INTO ekbs_table_observation ("
                "doc_id, shard_id, table_id, table_type, entity_key, entity_label, "
                "period, metric, value, value_surface, line_role, attrs_json"
                ") VALUES (?, 'kb_all_L1', 't1', 'E', '锅炉', '锅炉', ?, "
                "'单价', ?, ?, 'normal', ?)",
                (doc_id, period, value, str(value), '{"caption": "气费账单"}'),
            )
        conn.commit()
        hit = try_structured_query(
            conn, "2025年1-3月锅炉燃气单价平均值", _perm()
        )
    assert hit is not None and not hit.is_na
    assert hit.op == "avg"
    assert float(hit.value) == 3.295
    assert hit.rows_touched == 3
    assert hit.entity_key == "锅炉"


def test_sum_range_respects_attrs_contains(db) -> None:
    """1-2 月差异电费：sum 必须按 attrs 过滤，不能把用水金额加进去。"""
    from ekbs.retrieval.structured_query import try_structured_query

    with db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, default_level, status) "
            "VALUES ('plan-range', '区间测', 2, 1)"
        )
        kb_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, "
            "visible_dept_ids, status, checksum, doc_version) "
            "VALUES (?, '能耗', 'c.xlsx', 2, '[]', 'published', 'p3', 'v1')",
            (kb_id,),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        rows = (
            ("2026-01", 1987.86, "熙丰用电统计分析"),
            ("2026-01", 1439.70, "熙丰用水统计分析"),
            ("2026-02", 270.98, "熙丰用电统计分析"),
            ("2026-02", 929.85, "熙丰用水统计分析"),
        )
        for period, value, caption in rows:
            conn.execute(
                "INSERT INTO ekbs_table_observation ("
                "doc_id, shard_id, table_id, table_type, entity_key, entity_label, "
                "period, metric, value, value_surface, line_role, attrs_json"
                ") VALUES (?, 'kb_all_L1', 't1', 'E', '差异', '差异', ?, "
                "'金额（元）', ?, ?, 'normal', ?)",
                (doc_id, period, value, str(value), f'{{"caption": "{caption}"}}'),
            )
        conn.commit()
        planned = execute_structured_plan(
            conn,
            _perm(),
            op="sum",
            entity="差异",
            period="2026-01..2026-02",
            metric="金额",
            attrs_contains="用电",
        )
        ruled = try_structured_query(
            conn, "熙丰2026年1-2月的差异电费是多少", _perm()
        )
    assert planned is not None and not planned.is_na
    assert float(planned.value) == 2258.84
    assert planned.rows_touched == 2
    assert ruled is not None and not ruled.is_na
    assert float(ruled.value) == 2258.84
    assert ruled.op == "sum"


def test_multi_metric_water_and_elec_not_summed(db) -> None:
    """「用水量和用电量」须分别提取，禁止把水量+电量+电费加总。"""
    from ekbs.retrieval.structured_query import (
        _metrics_from_question,
        try_structured_query,
    )

    q = "2026年1月公司承担用水量和用电量"
    assert _metrics_from_question(q) == ["用水量", "用电量"]

    with db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, default_level, status) "
            "VALUES ('multi-m', '双指标', 2, 1)"
        )
        kb_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, "
            "visible_dept_ids, status, checksum, doc_version) "
            "VALUES (?, '能耗', 'm.xlsx', 2, '[]', 'published', 'm1', 'v1')",
            (kb_id,),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        rows = (
            ("公司承担用水量", "用水量", 15961.5),
            ("公司承担用电量", "用电量", 393592.77),
            ("公司承担电费", "用电量", 75138.79),  # 误标 metric 也不该进量答
            ("公司承担水费", "用水量", 999.0),
        )
        for key, metric, value in rows:
            conn.execute(
                "INSERT INTO ekbs_table_observation ("
                "doc_id, shard_id, table_id, table_type, entity_key, entity_label, "
                "period, metric, value, value_surface, line_role, attrs_json"
                ") VALUES (?, 'kb_all_L1', 't1', 'E', ?, ?, '2026-01', "
                "?, ?, ?, 'normal', '{}')",
                (doc_id, key, key, metric, value, str(value)),
            )
        conn.commit()
        hit = try_structured_query(conn, q, _perm())
        # LLM 错下 sum + 单 metric 时仍应按问句拆双指标
        planned = execute_structured_plan(
            conn,
            _perm(),
            op="sum",
            entity="公司承担用电量",
            period="2026-01",
            metric="用电量",
            question=q,
        )
        solo = execute_structured_plan(
            conn,
            _perm(),
            op="sum",
            entity="公司承担",
            period="2026-01",
            metric="用电量",
            question="2026年1月公司承担用电量",
        )
    assert hit is not None and not hit.is_na
    assert hit.op == "extract"
    assert (hit.extras or {}).get("source") == "multi_metric_extract"
    assert "用水量=15961.5" in hit.surface
    assert "用电量=393592.77" in hit.surface
    assert "75138" not in hit.surface
    assert "468731" not in hit.surface
    assert planned is not None and not planned.is_na
    assert planned.op == "extract"
    assert "用水量=15961.5" in planned.surface
    assert "用电量=393592.77" in planned.surface
    assert solo is not None and not solo.is_na
    assert float(solo.value) == 393592.77


def test_multi_metric_ignores_single_attrs_contains(db) -> None:
    """规划器误带 attrs=用水 时，多指标仍应提出水量+电量，不得整单 not_found。"""
    q = "2026年1月公司承担用水量和用电量"
    with db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_kb(code, name, default_level, status) "
            "VALUES ('multi-attrs', '双指标attrs', 2, 1)"
        )
        kb_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            "INSERT INTO ekbs_document(kb_id, title, source_path, level, "
            "visible_dept_ids, status, checksum, doc_version) "
            "VALUES (?, '能耗', 'ma.xlsx', 2, '[]', 'published', 'ma1', 'v1')",
            (kb_id,),
        )
        doc_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        for key, metric, value in (
            ("公司承担水量", "用水量", 15961.5),
            ("公司承担电量", "用电量", 393592.77),
        ):
            conn.execute(
                "INSERT INTO ekbs_table_observation ("
                "doc_id, shard_id, table_id, table_type, entity_key, entity_label, "
                "period, metric, value, value_surface, line_role, attrs_json"
                ") VALUES (?, 'kb_all_L1', 't1', 'E', ?, ?, '2026-01', "
                "?, ?, ?, 'normal', ?)",
                (
                    doc_id,
                    key,
                    key,
                    metric,
                    value,
                    str(value),
                    f'{{"类型": "{key}"}}',
                ),
            )
        conn.commit()
        hit = execute_structured_plan(
            conn,
            _perm(),
            op="extract",
            entity="公司承担水量",
            period="2026-01",
            metric="用水量",
            attrs_contains="用水",
            question=q,
        )
    assert hit is not None and not hit.is_na
    assert "用水量=15961.5" in hit.surface
    assert "用电量=393592.77" in hit.surface
