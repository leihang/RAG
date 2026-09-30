"""钉钉通讯录客户端 —— 依据 `docs/spec/50-dingtalk.md` §5（**已核实**的契约）。

三个**必须照做**的细节（都是官方文档明确写的，错了会静默少数据）：

1. **部门列表没有分页，且只返回「当前部门的下一级」** —— 全量部门树必须**自己递归**；
   根部门 ``dept_id = 1``。
2. **成员列表是游标分页**：首次 ``cursor=0``，之后传上次的 ``next_cursor``，看 ``has_more``。
3. **旧版 OAPI 路径（``topapi/v2/*``）的 token 走 URL 查询参数**，而新版
   （``/v1.0/contact/empLeaveRecords``）走请求头 —— 两套规范长期并存，混用报 40014。
   本模块把这件事收敛在 :meth:`_legacy_post` 与 :meth:`_new_get` 两个方法里。

⚠️ 现在的官方文档给出的通讯录接口**仍是旧版规范**（`docs/spec/50` §5.1 专门纠正过
「新版 `contact/v3/*`」的说法 —— 那些路径在官方全量 API 目录里查不到）。

⚠️ ``mobile``/``email`` 等敏感字段需要额外权限点；**取不到不是错误**，不要因此让同步失败。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

import httpx

from ekbs.config import Settings
from ekbs.errors import ConfigError, ModelUnavailable
from ekbs.logging import get_logger
from ekbs.provider.http_client import CloudError, request_with_retry, resolve_base_url

log = get_logger(component="dingtalk.directory")

LEGACY_BASE_URL = "https://oapi.dingtalk.com"
NEW_BASE_URL = "https://api.dingtalk.com"

DEPT_LISTSUB_PATH = "/topapi/v2/department/listsub"
DEPT_GET_PATH = "/topapi/v2/department/get"
USER_LIST_PATH = "/topapi/v2/user/list"
USER_GET_PATH = "/topapi/v2/user/get"
LEAVE_RECORDS_PATH = "/v1.0/contact/empLeaveRecords"

#: 根部门（官方两处写明「根部门 ID 为 1」）
ROOT_DEPT_ID = 1
#: 成员分页大小（官方示例 10，页内未给上限；取 100 以减少请求数）
USER_PAGE_SIZE = 100
#: 递归深度护栏（防脏数据成环把同步拖死）
MAX_DEPTH = 20
#: 已离职/待入职用户查详情会报这个错（官方列为成因之一）
ERR_USER_OUT_OF_SCOPE = 50002


@dataclass
class Department:
    dept_id: int
    name: str
    parent_id: int | None = None
    order: int = 0
    member_count: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class DirectoryUser:
    userid: str
    name: str = ""
    unionid: str = ""
    dept_ids: list[int] = field(default_factory=list)
    job_number: str = ""
    title: str = ""
    mobile: str = ""
    email: str = ""
    active: bool = True
    leader: bool = False
    hired_date: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class LeaveRecord:
    userid: str
    name: str = ""
    leave_time: str = ""
    leave_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class DingTalkDirectoryClient:
    def __init__(
        self,
        *,
        token_client: Any,
        settings: Settings | None = None,
        legacy_base_url: str | None = None,
        base_url: str | None = None,
        timeout_s: float = 10.0,
        max_retries: int = 2,
        retry_base_delay: float = 1.0,
        page_size: int = USER_PAGE_SIZE,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.token_client = token_client
        self.legacy_base = resolve_base_url(legacy_base_url, LEGACY_BASE_URL)
        self.base = resolve_base_url(base_url, NEW_BASE_URL)
        self.timeout_s = timeout_s
        self.max_retries = max(0, int(max_retries))
        self.retry_base_delay = retry_base_delay
        self.page_size = max(1, int(page_size))
        self._client = client

    # ------------------------------------------------------------------
    # 两套协议的调用底座
    # ------------------------------------------------------------------

    async def _legacy_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """旧版 OAPI：token 在 **URL 查询参数**上。"""
        token = await self.token_client.get_token()
        url = f"{self.legacy_base}{path}?access_token={token}"
        payload = await self._call("POST", url, json_body=body, headers={"Content-Type": "application/json"})
        self._raise_on_legacy_error(payload, path)
        return payload

    async def _new_get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """新版 OpenAPI：token 在**请求头**上。"""
        token = await self.token_client.get_token()
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{self.base}{path}?{query}"
        payload = await self._call(
            "GET", url, headers={"x-acs-dingtalk-access-token": token}
        )
        return payload

    async def _call(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        client = self._client or httpx.AsyncClient(timeout=self.timeout_s)
        owned = self._client is None
        try:
            response = await request_with_retry(
                client, method, url, provider="dingtalk",
                endpoint=url.split("?")[0].rsplit("/", 1)[-1],
                kind="llm", max_retries=self.max_retries,
                base_delay=self.retry_base_delay, json_body=json_body, headers=headers,
            )
        except CloudError as exc:
            raise ModelUnavailable(f"钉钉通讯录接口失败：{exc}") from exc
        finally:
            if owned:
                await client.aclose()
        try:
            payload = response.json() or {}
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            raise ModelUnavailable("钉钉通讯录接口返回了非对象响应")
        return payload

    @staticmethod
    def _raise_on_legacy_error(payload: dict[str, Any], path: str) -> None:
        code = int(payload.get("errcode", 0) or 0)
        if code == 0:
            return
        message = f"钉钉接口 {path} 返回 errcode={code} errmsg={payload.get('errmsg')}"
        if code in {40014, 42001, 40001}:
            raise ConfigError(f"{message}（access_token 无效或过期）")
        raise ModelUnavailable(message)

    # ------------------------------------------------------------------
    # 部门
    # ------------------------------------------------------------------

    async def list_sub_departments(self, dept_id: int | None = None) -> list[Department]:
        """只返回**下一级**部门（官方明确不支持一次取全部层级）。"""
        body: dict[str, Any] = {"language": "zh_CN"}
        if dept_id is not None:
            body["dept_id"] = int(dept_id)
        payload = await self._legacy_post(DEPT_LISTSUB_PATH, body)
        return [
            Department(
                dept_id=int(item.get("dept_id") or 0),
                name=str(item.get("name") or ""),
                parent_id=(
                    int(item["parent_id"]) if item.get("parent_id") is not None else dept_id
                ),
                raw=item,
            )
            for item in (payload.get("result") or [])
        ]

    async def get_department(self, dept_id: int) -> Department | None:
        try:
            payload = await self._legacy_post(DEPT_GET_PATH, {"dept_id": int(dept_id), "language": "zh_CN"})
        except ModelUnavailable as exc:
            log.warning("dingtalk_dept_get_failed", dept_id=dept_id, error=str(exc))
            return None
        result = payload.get("result") or {}
        if not result:
            return None
        return Department(
            dept_id=int(result.get("dept_id") or dept_id),
            name=str(result.get("name") or ""),
            parent_id=int(result["parent_id"]) if result.get("parent_id") is not None else None,
            order=int(result.get("order") or 0),
            member_count=(
                int(result["member_count"]) if result.get("member_count") is not None else None
            ),
            raw=result,
        )

    async def fetch_department_tree(self, root_id: int = ROOT_DEPT_ID) -> list[Department]:
        """递归拉取全量部门树（BFS + 去重 + 深度护栏）。

        官方没有「取全部层级」的接口，所以只能自己递归；同时用 ``seen`` 防脏数据成环。
        """
        root = await self.get_department(root_id)
        collected: dict[int, Department] = {}
        if root is not None:
            collected[root.dept_id] = root
        else:
            # 根详情取不到也要继续（listsub 缺省 dept_id 即根）
            collected[root_id] = Department(dept_id=root_id, name="根部门", parent_id=None)

        frontier = [root_id]
        depth = 0
        while frontier and depth < MAX_DEPTH:
            next_frontier: list[int] = []
            for parent in frontier:
                for child in await self.list_sub_departments(parent):
                    if child.dept_id in collected:
                        continue
                    collected[child.dept_id] = child
                    next_frontier.append(child.dept_id)
            frontier = next_frontier
            depth += 1
        if frontier:
            log.warning("dingtalk_dept_tree_depth_truncated", depth=MAX_DEPTH)
        return list(collected.values())

    # ------------------------------------------------------------------
    # 成员
    # ------------------------------------------------------------------

    async def list_department_users(
        self, dept_id: int, *, max_pages: int = 200
    ) -> list[DirectoryUser]:
        """游标分页取某个部门的全部成员（**不含子部门**）。"""
        users: list[DirectoryUser] = []
        cursor = 0
        for _ in range(max_pages):
            payload = await self._legacy_post(
                USER_LIST_PATH,
                {
                    "dept_id": int(dept_id),
                    "cursor": cursor,
                    "size": self.page_size,
                    "order_field": "custom",
                    "language": "zh_CN",
                },
            )
            result = payload.get("result") or {}
            for item in result.get("list") or []:
                users.append(_to_user(item))
            if not result.get("has_more"):
                break
            cursor = int(result.get("next_cursor") or 0)
        else:  # pragma: no cover - 分页护栏
            log.warning("dingtalk_user_list_pages_exhausted", dept_id=dept_id)
        return users

    async def get_user(self, userid: str) -> DirectoryUser | None:
        """用户详情。已离职/待入职会报 ``50002`` —— 这不是错误，是本函数的合法返回。"""
        try:
            payload = await self._legacy_post(USER_GET_PATH, {"userid": userid, "language": "zh_CN"})
        except ModelUnavailable as exc:
            if str(ERR_USER_OUT_OF_SCOPE) in str(exc):
                log.info("dingtalk_user_out_of_scope", userid=userid)
                return None
            raise
        result = payload.get("result") or {}
        return _to_user(result) if result else None

    async def iter_all_users(self, departments: Iterable[Department]) -> Iterator[DirectoryUser]:
        """按部门遍历全部成员（同一用户可能在多个部门出现，由调用方按 userid 合并）。"""
        for dept in departments:
            for user in await self.list_department_users(dept.dept_id):
                yield user

    # ------------------------------------------------------------------
    # 离职
    # ------------------------------------------------------------------

    async def list_leave_records(self, *, start_time: str, end_time: str) -> list[LeaveRecord]:
        """离职记录（官方明确的侧向表达方式；``leaveReason`` ∈ oapi/cancel/leave/unknown/delete）。"""
        payload = await self._new_get(
            LEAVE_RECORDS_PATH, {"startTime": start_time, "endTime": end_time}
        )
        records = payload.get("result") or payload.get("data") or []
        if isinstance(records, dict):
            records = records.get("list") or []
        out: list[LeaveRecord] = []
        for item in records:
            if not isinstance(item, dict):
                continue
            userid = str(item.get("userId") or item.get("userid") or "")
            if not userid:
                continue
            out.append(
                LeaveRecord(
                    userid=userid,
                    name=str(item.get("name") or ""),
                    leave_time=str(item.get("leaveTime") or ""),
                    leave_reason=str(item.get("leaveReason") or ""),
                    raw=item,
                )
            )
        return out


def _to_user(item: dict[str, Any]) -> DirectoryUser:
    dept_list = item.get("dept_id_list") or []
    if isinstance(dept_list, str):  # 个别接口返回逗号分隔串
        dept_list = [part for part in dept_list.split(",") if part.strip()]
    return DirectoryUser(
        userid=str(item.get("userid") or item.get("userId") or ""),
        name=str(item.get("name") or ""),
        unionid=str(item.get("unionid") or ""),
        dept_ids=[int(d) for d in dept_list if str(d).strip().lstrip("-").isdigit()],
        job_number=str(item.get("job_number") or ""),
        title=str(item.get("title") or ""),
        mobile=_mask_mobile(str(item.get("mobile") or "")),
        email=str(item.get("email") or item.get("org_email") or ""),
        active=bool(item.get("active", True)),
        leader=bool(item.get("leader", False)),
        hired_date=str(item.get("hired_date") or ""),
        raw=item,
    )


def _mask_mobile(mobile: str) -> str:
    """手机号脱敏后再入库（`01` §5.1 的 ``mobile_masked`` 列**只存脱敏值**）。"""
    digits = "".join(ch for ch in mobile if ch.isdigit())
    if len(digits) < 7:
        return mobile
    return f"{digits[:3]}****{digits[-4:]}"
