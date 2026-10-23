"""个人私有库接口。钉钉机器人用内部令牌调用，管理台登录同样可用。"""

from __future__ import annotations

import base64

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ekbs.api.auth import (
    INTERNAL_CHANNEL_HEADER,
    INTERNAL_DT_USER_HEADER,
    INTERNAL_TOKEN_HEADER,
    INTERNAL_USER_HEADER,
)
from ekbs.api.discovery import _headers
from ekbs.api.qa import _authenticate
from ekbs.errors import ConfigError
from ekbs.personal import (
    delete_personal_file,
    format_personal_list,
    list_personal_files,
    register_personal_upload,
)

router = APIRouter(tags=["personal"])


class UploadIn(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=1)


class DeleteIn(BaseModel):
    title: str = Field(default="", max_length=200)


def _user(request: Request, authorization, token, user_id, dt_userid, channel):
    return _authenticate(
        request,
        _headers(authorization, token, user_id, dt_userid, channel),
    )


@router.get("/personal/files")
async def personal_files(
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
    x_internal_dt_userid: str | None = Header(default=None, alias=INTERNAL_DT_USER_HEADER),
    x_internal_channel: str | None = Header(default=None, alias=INTERNAL_CHANNEL_HEADER),
) -> JSONResponse:
    """列出当前用户私有库里的文件。"""
    user = _user(
        request, authorization, x_internal_token, x_internal_user_id,
        x_internal_dt_userid, x_internal_channel,
    )
    items = list_personal_files(request.app.state.runtime.db, user_id=user.user_id)
    return JSONResponse({"items": items, "message": format_personal_list(items)})


@router.post("/personal/files")
async def personal_upload(
    payload: UploadIn,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
    x_internal_dt_userid: str | None = Header(default=None, alias=INTERNAL_DT_USER_HEADER),
    x_internal_channel: str | None = Header(default=None, alias=INTERNAL_CHANNEL_HEADER),
) -> JSONResponse:
    """把一份文件登记进当前用户的私有库。"""
    user = _user(
        request, authorization, x_internal_token, x_internal_user_id,
        x_internal_dt_userid, x_internal_channel,
    )
    try:
        content = base64.b64decode(payload.content_base64, validate=True)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"message": "文件内容无法读取"}, status_code=422)
    runtime = request.app.state.runtime
    try:
        result = register_personal_upload(
            runtime.db, runtime.settings,
            user_id=user.user_id, filename=payload.filename, content=content,
        )
    except ConfigError as exc:
        return JSONResponse({"message": str(exc)}, status_code=422)
    return JSONResponse(result)


@router.post("/personal/files/delete")
async def personal_delete(
    payload: DeleteIn,
    request: Request,
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_internal_user_id: str | None = Header(default=None, alias=INTERNAL_USER_HEADER),
    x_internal_dt_userid: str | None = Header(default=None, alias=INTERNAL_DT_USER_HEADER),
    x_internal_channel: str | None = Header(default=None, alias=INTERNAL_CHANNEL_HEADER),
) -> JSONResponse:
    """按标题删除当前用户私有库里的一份文件。"""
    user = _user(
        request, authorization, x_internal_token, x_internal_user_id,
        x_internal_dt_userid, x_internal_channel,
    )
    result = delete_personal_file(
        request.app.state.runtime.db, user_id=user.user_id, title=payload.title
    )
    return JSONResponse(result)
