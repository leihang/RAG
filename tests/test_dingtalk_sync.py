"""钉钉通讯录客户端与组织同步（`04` §4；契约依据 `docs/spec/50` §5）。

客户端部分用 respx 验 HTTP 契约（含三个易错点：部门**只返回下一级**、成员**游标分页**、
旧版路径 token **走查询参数**）；同步逻辑用假目录对象验分支（更快、覆盖更全）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from ekbs.dingtalk.directory import (
    AuthScopes,
    Department,
    DingTalkDirectoryClient,
    DirectoryUser,
    LeaveRecord,
)
from ekbs.dingtalk.org_events import PERSONNEL_EVENTS, event_type_of, user_ids_of
from ekbs.dingtalk.sync import OrgSyncService, build_sync_service
from ekbs.errors import ConfigError, ModelUnavailable
from ekbs.observability import metrics as m

LEGACY = "https://oapi.dingtalk.com"
NEW = "https://api.dingtalk.com"
LISTS_SUB = f"{LEGACY}/topapi/v2/department/listsub"
DEPT_GET = f"{LEGACY}/topapi/v2/department/get"
USER_LIST = f"{LEGACY}/topapi/v2/user/list"
USER_GET = f"{LEGACY}/topapi/v2/user/get"
LEAVE = f"{NEW}/v1.0/contact/empLeaveRecords"


class FakeTokenClient:
    async def get_token(self, *, force: bool = False) -> str:
        return "tok-dir"


@pytest.fixture(autouse=True)
def _reset_metrics():
    m.reset()
    yield
    m.reset()


def make_client(settings=None) -> DingTalkDirectoryClient:
    return DingTalkDirectoryClient(
        token_client=FakeTokenClient(), settings=settings, retry_base_delay=0.0
    )


# ---------------------------------------------------------------------------
# 客户端：HTTP 契约
# ---------------------------------------------------------------------------


@respx.mock
async def test_department_tree_requires_recursion() -> None:
    """官方接口**只返回下一级**，全量树必须自己递归（漏了这步会静默少部门）。"""
    def listsub(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        parent = body.get("dept_id", 1)
        children = {
            1: [{"dept_id": 100, "name": "总部", "parent_id": 1}],
            100: [
                {"dept_id": 101, "name": "人力资源部", "parent_id": 100},
                {"dept_id": 102, "name": "财务部", "parent_id": 100},
            ],
        }
        return httpx.Response(
            200, json={"errcode": 0, "errmsg": "ok", "result": children.get(parent, [])}
        )

    respx.post(url__regex=rf"{LISTS_SUB}.*").mock(side_effect=listsub)
    respx.post(url__regex=rf"{DEPT_GET}.*").mock(
        return_value=httpx.Response(
            200, json={"errcode": 0, "result": {"dept_id": 1, "name": "根部门", "parent_id": None}}
        )
    )
    departments = await make_client().fetch_department_tree()
    ids = {d.dept_id for d in departments}
    assert ids == {1, 100, 101, 102}, f"应递归收集全部层级，实际 {ids}"


@respx.mock
async def test_department_tree_survives_cycles() -> None:
    """脏数据成环时不能死循环（有 seen 去重 + 深度护栏）。"""
    def listsub(request: httpx.Request) -> httpx.Response:
        parent = json.loads(request.content).get("dept_id", 1)
        mapping = {1: [{"dept_id": 2, "name": "A", "parent_id": 1}],
                   2: [{"dept_id": 1, "name": "根", "parent_id": 2}]}
        return httpx.Response(200, json={"errcode": 0, "result": mapping.get(parent, [])})

    respx.post(url__regex=rf"{LISTS_SUB}.*").mock(side_effect=listsub)
    respx.post(url__regex=rf"{DEPT_GET}.*").mock(
        return_value=httpx.Response(200, json={"errcode": 0, "result": {}})
    )
    departments = await make_client().fetch_department_tree()
    assert {d.dept_id for d in departments} == {1, 2}


@respx.mock
async def test_user_list_paginates_with_cursor() -> None:
    seen_cursors: list[int] = []

    def user_list(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_cursors.append(int(body["cursor"]))
        if int(body["cursor"]) == 0:
            return httpx.Response(
                200,
                json={"errcode": 0, "result": {"has_more": True, "next_cursor": 100,
                                               "list": [{"userid": "u1", "name": "甲",
                                                         "dept_id_list": [101]}]}},
            )
        return httpx.Response(
            200,
            json={"errcode": 0, "result": {"has_more": False, "next_cursor": 0,
                                           "list": [{"userid": "u2", "name": "乙",
                                                     "dept_id_list": [101]}]}},
        )

    route = respx.post(url__regex=rf"{USER_LIST}.*").mock(side_effect=user_list)
    users = await make_client().list_department_users(101)
    assert [u.userid for u in users] == ["u1", "u2"]
    assert seen_cursors == [0, 100], "首次 cursor=0，之后传上次的 next_cursor"
    assert route.call_count == 2


@respx.mock
async def test_legacy_paths_put_token_in_query_new_paths_in_header() -> None:
    route = respx.post(url__regex=rf"{LISTS_SUB}.*").mock(
        return_value=httpx.Response(200, json={"errcode": 0, "result": []})
    )
    await make_client().list_sub_departments(1)
    assert "access_token=tok-dir" in str(route.calls[0].request.url)

    leave_route = respx.get(url__regex=rf"{LEAVE}.*").mock(
        return_value=httpx.Response(200, json={"result": []})
    )
    await make_client().list_leave_records(start_time="2026-01-01T00:00:00Z",
                                          end_time="2026-01-02T00:00:00Z")
    request = leave_route.calls[0].request
    assert request.headers["x-acs-dingtalk-access-token"] == "tok-dir"
    assert "access_token=" not in str(request.url), "新版路径不应把 token 放查询参数"


@respx.mock
async def test_user_fields_are_normalised_and_mobile_masked() -> None:
    respx.post(url__regex=rf"{USER_LIST}.*").mock(
        return_value=httpx.Response(
            200,
            json={"errcode": 0, "result": {"has_more": False, "list": [{
                "userid": "u9", "name": "张三", "unionid": "un9", "mobile": "13812345678",
                "email": "z@example.com", "title": "主管", "job_number": "E001",
                "dept_id_list": [101, 102], "active": True, "leader": True,
                "hired_date": "2020-01-01",
            }]}},
        )
    )
    user = (await make_client().list_department_users(101))[0]
    assert user.dept_ids == [101, 102]
    assert user.mobile == "138****5678", "01 §5.1 的 mobile_masked 列只存脱敏值"
    assert user.title == "主管" and user.leader is True


@respx.mock
async def test_user_out_of_scope_is_not_an_error() -> None:
    """已离职/待入职查详情会报 50002 —— 那是合法返回，不是故障。"""
    respx.post(url__regex=rf"{USER_GET}.*").mock(
        return_value=httpx.Response(
            200, json={"errcode": 50002, "errmsg": "请求的员工 userid 不在授权范围内"}
        )
    )
    assert await make_client().get_user("left-user") is None


@respx.mock
async def test_invalid_token_raises_config_error() -> None:
    respx.post(url__regex=rf"{LISTS_SUB}.*").mock(
        return_value=httpx.Response(200, json={"errcode": 40014, "errmsg": "invalid access_token"})
    )
    with pytest.raises(ConfigError):
        await make_client().list_sub_departments(1)


@respx.mock
async def test_leave_records_are_parsed() -> None:
    respx.get(url__regex=rf"{LEAVE}.*").mock(
        return_value=httpx.Response(
            200,
            json={"result": [
                {"userId": "u1", "name": "甲", "leaveTime": "2026-09-01T10:00:00Z",
                 "leaveReason": "leave"},
                {"userId": "u2", "leaveTime": "2026-09-02T10:00:00Z", "leaveReason": "delete"},
            ]},
        )
    )
    records = await make_client().list_leave_records(
        start_time="2026-09-01T00:00:00Z", end_time="2026-09-03T00:00:00Z"
    )
    assert [r.userid for r in records] == ["u1", "u2"]
    assert records[0].leave_reason == "leave" and records[1].leave_reason == "delete"


# ---------------------------------------------------------------------------
# 同步服务（假目录对象）
# ---------------------------------------------------------------------------


class FakeDirectory:
    """按 04 §4.2 的映射造一份组织数据；可注入失败。"""

    def __init__(self) -> None:
        self.departments = [
            Department(dept_id=1, name="根部门", parent_id=None),
            Department(dept_id=100, name="总部", parent_id=1),
            Department(dept_id=101, name="人力资源部", parent_id=100),
        ]
        self.users = {
            1: [DirectoryUser(userid="u_boss", name="老板", dept_ids=[1])],
            100: [
                DirectoryUser(userid="u_alice", name="Alice", dept_ids=[100, 101],
                              mobile="13812345678", title="HR 主管"),
                DirectoryUser(userid="u_carol", name="Carol", dept_ids=[100]),
            ],
            101: [DirectoryUser(userid="u_alice", name="Alice", dept_ids=[101])],
        }
        self.leaves: list[LeaveRecord] = []
        self.fail_tree = False

    async def fetch_department_tree(self, root_id: int = 1) -> list[Department]:
        if self.fail_tree:
            raise ModelUnavailable("模拟钉钉不可用")
        return list(self.departments)

    async def list_department_users(self, dept_id: int, **_: object) -> list[DirectoryUser]:
        return list(self.users.get(dept_id, []))

    async def list_leave_records(self, *, start_time: str, end_time: str) -> list[LeaveRecord]:
        return list(self.leaves)


def service(runtime, directory: FakeDirectory) -> OrgSyncService:
    return OrgSyncService(
        db=runtime.db, settings=runtime.settings, directory=directory, cache=runtime.cache
    )


async def test_full_sync_upserts_org_and_rebuilds_chain(seeded) -> None:
    runtime, _ = seeded
    directory = FakeDirectory()
    report = await service(runtime, directory).sync_full()

    assert report.departments == 3 and report.users == 3
    assert report.policy_version >= 1
    with runtime.db.sync_connect() as conn:
        depts = {
            str(r["dingtalk_dept_id"]): r
            for r in conn.execute(
                "SELECT dept_id, dingtalk_dept_id, name, parent_dept_id, depth FROM ekbs_dept"
            ).fetchall()
        }
        assert "101" in depts
        # 父子关系已解析成内部 dept_id（而不是留字符串）
        assert depts["101"]["parent_dept_id"] == depts["100"]["dept_id"]
        closure = int(
            conn.execute("SELECT COUNT(*) FROM ekbs_dept_closure").fetchone()[0]
        )
        assert closure >= 3, "闭包必须重建（含自身 depth=0）"
        chain = conn.execute(
            "SELECT dept_chain FROM ekbs_user WHERE dingtalk_userid = 'u_alice'"
        ).fetchone()["dept_chain"]
        assert json.loads(chain) == ["1", "100", "101"], "dept_chain = 本部门 + 全部祖先"
        log_row = conn.execute(
            "SELECT mode, deleted, detail FROM ekbs_sync_log ORDER BY log_id DESC LIMIT 1"
        ).fetchone()
    assert log_row["mode"] == "full"
    assert m.current(m.ORG_SYNC_DURATION) >= 1, "同步耗时必须被观测（ORG_SYNC_DURATION）"


async def test_full_sync_is_idempotent(seeded) -> None:
    runtime, _ = seeded
    directory = FakeDirectory()
    first = await service(runtime, directory).sync_full()
    second = await service(runtime, directory).sync_full()
    assert first.created > 0
    assert second.created == 0 and second.users == first.users
    with runtime.db.sync_connect() as conn:
        duplicates = int(
            conn.execute(
                "SELECT COUNT(*) FROM (SELECT dingtalk_userid FROM ekbs_user "
                "GROUP BY dingtalk_userid HAVING COUNT(*) > 1)"
            ).fetchone()[0]
        )
    assert duplicates == 0


async def test_sync_does_not_overwrite_clearance(seeded) -> None:
    """密级是内部配置，同步**不得**把它改回默认值（否则等于悄悄提权/降权）。"""
    runtime, _ = seeded
    directory = FakeDirectory()
    await service(runtime, directory).sync_full()
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "UPDATE ekbs_user SET clearance = 3 WHERE dingtalk_userid = 'u_alice'"
        )
        conn.commit()
    await service(runtime, directory).sync_full()
    with runtime.db.sync_connect() as conn:
        clearance = int(
            conn.execute(
                "SELECT clearance FROM ekbs_user WHERE dingtalk_userid = 'u_alice'"
            ).fetchone()["clearance"]
        )
    assert clearance == 3


async def test_leave_records_mark_user_left_and_record_lag(seeded) -> None:
    """离职必须**立刻**生效：状态置 left + 审计 + 缓存失效 + 滞后指标。"""
    runtime, _ = seeded
    directory = FakeDirectory()
    svc = service(runtime, directory)
    await svc.sync_full()
    leave_time = (datetime.now(timezone.utc) - timedelta(seconds=120)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    directory.leaves = [
        LeaveRecord(userid="u_alice", name="Alice", leave_time=leave_time, leave_reason="leave")
    ]
    # 先写一条该用户的权限缓存，验证会被清掉
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_cache(cache_key, value, expires_at) VALUES "
            "('ekbs:perm:user:1', '{}', 9999999999)"
        )
        conn.commit()
    with runtime.db.sync_connect() as conn:
        before = int(conn.execute("SELECT version FROM ekbs_policy_version").fetchone()[0])
    report = await svc.sync_leave_records(days=1)

    assert report.deleted == 1
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT status, left_at FROM ekbs_user WHERE dingtalk_userid = 'u_alice'"
        ).fetchone()
        assert row["status"] == "left" and row["left_at"]
        change = conn.execute(
            "SELECT change_type, reason FROM ekbs_perm_change_log WHERE change_type = 'leave' "
            "ORDER BY change_id DESC LIMIT 1"
        ).fetchone()
        assert change["reason"] == "leave"
        after = int(
            conn.execute("SELECT version FROM ekbs_policy_version").fetchone()[0]
        )
        perm_cache = int(
            conn.execute(
                "SELECT COUNT(*) FROM ekbs_cache WHERE cache_key LIKE 'ekbs:perm:%'"
            ).fetchone()[0]
        )
    assert after > before, "离职必须让 policy_version 自增（全量缓存失效）"
    assert perm_cache == 0, "该用户的权限缓存必须立刻删除，不能等 TTL"
    lag = m.current(m.PERM_REVOKE_LAG)
    assert 100 <= lag <= 300, f"权限回收滞后应约 120s，实际 {lag}"


async def test_absent_users_are_kept_by_default(seeded) -> None:
    """默认不因「没拉到」就判离职 —— 钉钉可见范围会让用户合法地不出现。"""
    runtime, _ = seeded
    directory = FakeDirectory()
    svc = service(runtime, directory)
    await svc.sync_full()
    directory.users[100] = [u for u in directory.users[100] if u.userid != "u_carol"]
    report = await svc.sync_full()
    assert report.deleted == 0
    with runtime.db.sync_connect() as conn:
        status = conn.execute(
            "SELECT status FROM ekbs_user WHERE dingtalk_userid = 'u_carol'"
        ).fetchone()["status"]
    assert status == "active"

    # 先造一个「只属于同步范围之外部门」的用户（模拟钉钉应用可见范围外的人）
    with runtime.db.sync_connect() as conn:
        conn.execute(
            "INSERT INTO ekbs_dept(dingtalk_dept_id, name, dept_path) VALUES ('999', '范围外部', '/')"
        )
        conn.execute(
            "INSERT INTO ekbs_user(dingtalk_userid, nick, status) VALUES ('u_outside', '范围外', 'active')"
        )
        outside_user = int(
            conn.execute(
                "SELECT user_id FROM ekbs_user WHERE dingtalk_userid = 'u_outside'"
            ).fetchone()["user_id"]
        )
        outside_dept = int(
            conn.execute("SELECT dept_id FROM ekbs_dept WHERE dingtalk_dept_id = '999'").fetchone()[
                "dept_id"
            ]
        )
        conn.execute(
            "INSERT INTO ekbs_user_dept(user_id, dept_id, is_primary) VALUES (?, ?, 1)",
            (outside_user, outside_dept),
        )
        conn.commit()

    # 显式要求时才清理。注意语义：本次同步范围**含根部门 1**，因此挂在根部门的用户
    # （种子数据里的演示用户也算）都在范围内，缺席即被判离职 —— 这正是
    # `_mark_absent` docstring 里那条「根部门在范围内时要格外小心」的由来。
    report2 = await svc.sync_full(mark_absent=True)
    assert report2.deleted >= 1
    with runtime.db.sync_connect() as conn:
        carol_status = conn.execute(
            "SELECT status FROM ekbs_user WHERE dingtalk_userid = 'u_carol'"
        ).fetchone()["status"]
        outside_status = conn.execute(
            "SELECT status FROM ekbs_user WHERE dingtalk_userid = 'u_outside'"
        ).fetchone()["status"]
    assert carol_status == "left"
    # **真正的安全性质**：同步范围之外的用户绝不能被标离职（可见范围 ≠ 离职）
    assert outside_status == "active"


async def test_reconcile_reports_but_does_not_write(seeded) -> None:
    runtime, _ = seeded
    directory = FakeDirectory()
    svc = service(runtime, directory)
    with runtime.db.sync_connect() as conn:
        logs_before = int(conn.execute("SELECT COUNT(*) FROM ekbs_sync_log").fetchone()[0])
        depts_before = int(conn.execute("SELECT COUNT(*) FROM ekbs_dept").fetchone()[0])
    report = await svc.reconcile()
    assert report["remote_departments"] == 3
    assert report["missing_locally"] >= 3
    with runtime.db.sync_connect() as conn:
        # 对账只报告、不写库
        assert int(conn.execute("SELECT COUNT(*) FROM ekbs_sync_log").fetchone()[0]) == logs_before
        assert int(conn.execute("SELECT COUNT(*) FROM ekbs_dept").fetchone()[0]) == depts_before


async def test_sync_failure_increments_metric_and_reraises(seeded) -> None:
    runtime, _ = seeded
    directory = FakeDirectory()
    directory.fail_tree = True
    with pytest.raises(ModelUnavailable):
        await service(runtime, directory).sync_full()
    assert m.current(m.ORG_SYNC_FAILED, scope="full") == 1
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT failed, mode FROM ekbs_sync_log ORDER BY log_id DESC LIMIT 1"
        ).fetchone()
    assert int(row["failed"]) == 1 and row["mode"] == "full"


def test_user_ids_come_from_either_event_shape() -> None:
    assert user_ids_of({"userId": ["a", " b"]}) == ["a", "b"]
    assert user_ids_of({"userid": "c"}) == ["c"]
    assert event_type_of("", {"eventType": "user_modify_org"}) == "user_modify_org"
    assert {"user_add_org", "user_modify_org", "user_leave_org"} <= PERSONNEL_EVENTS


@respx.mock
async def test_fetch_auth_scopes_reads_authorized_people() -> None:
    respx.get(url__regex=rf"{LEGACY}/auth/scopes.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "errcode": 0,
                "errmsg": "ok",
                "auth_user_field": ["name", "position"],
                "auth_org_scopes": {"authed_dept": [], "authed_user": ["u1"]},
            },
        )
    )
    scopes = await make_client().fetch_auth_scopes()
    assert scopes.authed_dept_ids == []
    assert scopes.authed_user_ids == ["u1"]
    assert scopes.limited is True


class ScopedDirectory(FakeDirectory):
    """授权范围里只有一个人，禁止再去拉根部门。"""

    async def fetch_auth_scopes(self) -> AuthScopes:
        return AuthScopes(authed_user_ids=["u_only"])

    async def fetch_department_tree(self, root_id: int = 1) -> list[Department]:
        raise AssertionError("授权范围不含部门时不应从根部门起拉")

    async def get_user(self, userid: str) -> DirectoryUser | None:
        if userid != "u_only":
            return None
        return DirectoryUser(userid=userid, name="仅授权", title="测试岗", dept_ids=[])

    async def get_department(self, dept_id: int) -> Department | None:
        return None


async def test_full_sync_registers_scoped_users_without_root(seeded) -> None:
    runtime, _ = seeded
    report = await service(runtime, ScopedDirectory()).sync_full()
    assert report.detail["source"] == "auth_scopes"
    assert report.detail["authed_departments"] == 0
    assert report.detail["authed_users"] == 1
    assert report.users == 1 and report.failed == 0
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT nick, title, status FROM ekbs_user WHERE dingtalk_userid = 'u_only'"
        ).fetchone()
        role = conn.execute(
            "SELECT r.code FROM ekbs_user_role ur JOIN ekbs_role r ON r.role_id = ur.role_id "
            "JOIN ekbs_user u ON u.user_id = ur.user_id WHERE u.dingtalk_userid = 'u_only'"
        ).fetchone()
    assert row["nick"] == "仅授权" and row["title"] == "测试岗" and row["status"] == "active"
    assert role["code"] == "end_user"


async def test_department_change_replaces_old_membership(seeded) -> None:
    runtime, _ = seeded
    directory = FakeDirectory()
    await service(runtime, directory).sync_full()
    directory.users = {
        1: [
            DirectoryUser(userid="u_boss", name="老板", dept_ids=[1]),
            DirectoryUser(userid="u_alice", name="Alice", dept_ids=[1], title="顾问"),
        ],
        100: [DirectoryUser(userid="u_carol", name="Carol", dept_ids=[100])],
        101: [],
    }
    await service(runtime, directory).sync_full()
    with runtime.db.sync_connect() as conn:
        chain = json.loads(conn.execute(
            "SELECT dept_chain FROM ekbs_user WHERE dingtalk_userid = 'u_alice'"
        ).fetchone()["dept_chain"])
        title = conn.execute(
            "SELECT title FROM ekbs_user WHERE dingtalk_userid = 'u_alice'"
        ).fetchone()["title"]
    assert chain == ["1"]
    assert title == "顾问"


class PersonnelDirectory:
    def __init__(self) -> None:
        self.user = DirectoryUser(userid="u_new", name="新人", dept_ids=[101], title="专员")

    async def get_user(self, userid: str) -> DirectoryUser | None:
        return self.user if userid == self.user.userid else None

    async def get_department(self, dept_id: int) -> Department | None:
        if int(dept_id) == 101:
            return Department(dept_id=101, name="人力资源部", parent_id=1)
        if int(dept_id) == 1:
            return Department(dept_id=1, name="根部门", parent_id=None)
        return None


async def test_personnel_events_cover_hire_move_and_leave(seeded) -> None:
    runtime, _ = seeded
    directory = PersonnelDirectory()
    svc = service(runtime, directory)
    hired = await svc.apply_personnel_event("user_add_org", ["u_new"])
    assert hired.users == 1 and hired.created >= 1
    with runtime.db.sync_connect() as conn:
        title = conn.execute(
            "SELECT title, status FROM ekbs_user WHERE dingtalk_userid = 'u_new'"
        ).fetchone()
    assert title["title"] == "专员" and title["status"] == "active"

    directory.user = DirectoryUser(userid="u_new", name="新人", dept_ids=[1], title="主管")
    moved = await svc.apply_personnel_event("user_modify_org", ["u_new"])
    assert moved.updated >= 1
    with runtime.db.sync_connect() as conn:
        row = conn.execute(
            "SELECT title, dept_chain FROM ekbs_user WHERE dingtalk_userid = 'u_new'"
        ).fetchone()
    assert row["title"] == "主管"
    assert json.loads(row["dept_chain"]) == ["1"]

    left = await svc.apply_personnel_event("user_leave_org", ["u_new"])
    assert left.deleted == 1
    with runtime.db.sync_connect() as conn:
        status = conn.execute(
            "SELECT status FROM ekbs_user WHERE dingtalk_userid = 'u_new'"
        ).fetchone()["status"]
    assert status == "left"


def test_scheduler_does_not_pull_org_on_a_cron(runtime) -> None:
    """组织全量不能被固定 cron 无条件拉取。

    管理台保存的「定时更新」是开关 + 小时间隔（`permission/org_schedule.py`），
    调度器只挂 5 分钟一次的「到点检查」，未开启/未到间隔时 `claim_org_sync` 直接返回。
    """
    from apscheduler.triggers.interval import IntervalTrigger

    from ekbs.scheduler import build_scheduler

    jobs = {job.id: job for job in build_scheduler(runtime).get_jobs()}
    job = jobs.get("org_sync")
    assert job is not None, "调度器需要到点执行管理台保存的定时更新"
    assert isinstance(job.trigger, IntervalTrigger), "组织同步只能是到点检查，不能是固定 cron"
    assert job.trigger.interval.total_seconds() == 300


def test_build_sync_service_requires_credentials(runtime) -> None:
    from ekbs.config import Secrets

    with pytest.raises(ConfigError) as excinfo:
        build_sync_service(runtime.db, runtime.settings, Secrets({"EKBS_INTERNAL_TOKEN": "t"}))
    assert "DINGTALK_CLIENT_ID" in str(excinfo.value)
