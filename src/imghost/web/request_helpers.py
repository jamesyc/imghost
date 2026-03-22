from __future__ import annotations

from fastapi import Request

from ..models import User
from ..rate_limits import hash_anon_identity


def client_ip(request: Request) -> str:
    for header_name in ("CF-Connecting-IP", "X-Real-IP", "X-Forwarded-For"):
        value = request.headers.get(header_name)
        if not value:
            continue
        if header_name == "X-Forwarded-For":
            return value.split(",", 1)[0].strip()
        return value.strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def upload_rate_limit_key(request: Request, user: User | None) -> str:
    if user is not None:
        return user.id
    return hash_anon_identity(client_ip(request), request.headers.get("User-Agent", ""))
