"""钉钉通讯录人员变动 —— 入职、离职、职位与部门变化。

全量登记只在手动同步时做。日常靠 Stream 推过来的事件改本地用户，
不再每天凌晨把整本通讯录重拉一遍（整点调用会撞上钉钉的频率限制）。
"""

from __future__ import annotations

from typing import Any

from ekbs.logging import get_logger

log = get_logger(component="dingtalk.org_events")

#: 会改「这个人能不能提问、属于哪个部门、什么职位」的事件。
PERSONNEL_EVENTS = frozenset({
    "user_add_org",
    "user_active_org",
    "user_modify_org",
    "user_leave_org",
})


def event_type_of(header_type: str, data: dict[str, Any]) -> str:
    return str(header_type or data.get("eventType") or data.get("EventType") or "").strip()


def user_ids_of(data: dict[str, Any]) -> list[str]:
    raw = (
        data.get("userId")
        or data.get("UserId")
        or data.get("userid")
        or data.get("staffId")
        or []
    )
    if isinstance(raw, str):
        text = raw.strip()
        return [text] if text else []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def build_org_event_handler(service: Any, on_connected: Any = None) -> Any:
    """包一层 Stream ``EventHandler``。未知事件直接 ACK，人员事件落库后再 ACK。"""
    from dingtalk_stream import AckMessage, EventHandler

    class _OrgEventHandler(EventHandler):
        async def process(self, event: Any) -> tuple[int, str]:
            if on_connected is not None:
                on_connected()
            data = event.data if isinstance(getattr(event, "data", None), dict) else {}
            header_type = ""
            headers = getattr(event, "headers", None)
            if headers is not None:
                header_type = str(getattr(headers, "event_type", "") or "")
            event_type = event_type_of(header_type, data)
            if event_type not in PERSONNEL_EVENTS:
                log.info("dingtalk_org_event_ignored", event_type=event_type or "(empty)")
                return AckMessage.STATUS_OK, "OK"
            user_ids = user_ids_of(data)
            if not user_ids:
                log.warning("dingtalk_org_event_without_user", event_type=event_type)
                return AckMessage.STATUS_OK, "OK"
            try:
                await service.apply_personnel_event(event_type, user_ids)
            except Exception as exc:  # noqa: BLE001 - 交给钉钉重投
                log.error(
                    "dingtalk_org_event_failed",
                    event_type=event_type,
                    error=f"{type(exc).__name__}: {exc}",
                )
                return AckMessage.STATUS_SYSTEM_EXCEPTION, "retry"
            return AckMessage.STATUS_OK, "OK"

    return _OrgEventHandler()
