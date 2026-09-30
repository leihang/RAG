"""API 层：应用装配、鉴权、健康端点、问答端点、资源端点。"""

from ekbs.api.app import ROUTE_PREFIXES, create_app
from ekbs.api.auth import AuthUser, Authenticator

__all__ = ["ROUTE_PREFIXES", "AuthUser", "Authenticator", "create_app"]
