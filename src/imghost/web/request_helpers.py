from __future__ import annotations

from fastapi import Request

from ..models import User
from ..public_origin import trusted_forwarded_client_ip
from .request_context import get_state


def client_ip(request: Request) -> str:
    forwarded = trusted_forwarded_client_ip(request, get_state(request).settings)
    if forwarded is not None:
        return forwarded
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def auth_rate_limit_ip_key(request: Request) -> str:
    return client_ip(request)


def upload_rate_limit_key(request: Request, user: User | None) -> str:
    if user is not None:
        return user.id
    return client_ip(request)
