"""钉钉接入（`04`）：令牌、Stream 适配器、卡片、免登、通讯录同步。

契约依据：``docs/spec/50-dingtalk.md``（按官方文档核实；其中 14 处与 `方案/04` 不一致，
已在 README 的发现清单中列出，待架构负责人确认后同步文档）。

现状：令牌客户端、OpenAPI 客户端（消息/卡片/流式）与消息处理器已实现并**离线测试**；
真正的 Stream 长连接待企业应用凭据（AppKey/AppSecret/RobotCode/卡片模板 ID）就绪后启动。
"""

from ekbs.dingtalk.client import (
    CardTarget,
    DingTalkOpenApiClient,
    markdown_msg_param,
    text_msg_param,
)
from ekbs.dingtalk.directory import (
    Department,
    DingTalkDirectoryClient,
    DirectoryUser,
    LeaveRecord,
)
from ekbs.dingtalk.handler import InboundMessage, StreamMessageHandler, make_handler
from ekbs.dingtalk.sync import OrgSyncService, SyncReport, build_sync_service
from ekbs.dingtalk.token import DingTalkTokenClient, TokenStatus

__all__ = [
    "CardTarget",
    "Department",
    "DingTalkDirectoryClient",
    "DirectoryUser",
    "LeaveRecord",
    "OrgSyncService",
    "SyncReport",
    "build_sync_service",
    "DingTalkOpenApiClient",
    "DingTalkTokenClient",
    "InboundMessage",
    "StreamMessageHandler",
    "TokenStatus",
    "make_handler",
    "markdown_msg_param",
    "text_msg_param",
]
