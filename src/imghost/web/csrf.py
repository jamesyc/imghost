from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

from ..config import Settings
from ..public_origin import (
    _forwarded_origin,
    _normalize_origin,
    _request_origin,
    _trusted_origin_set,
    request_uses_trusted_proxy_headers,
)


def _request_uses_bearer_auth(request: Request) -> bool:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    return scheme.lower() == "bearer" and bool(token)


def _referer_origin(referer: str | None) -> str | None:
    candidate = (referer or "").strip()
    if not candidate:
        return None
    parsed = urlsplit(candidate)
    if not parsed.scheme or not parsed.netloc:
        return None
    return _normalize_origin(f"{parsed.scheme}://{parsed.netloc}")


def _has_trusted_csrf_source(request: Request, settings: Settings) -> bool:
    trusted = _trusted_origin_set(settings)
    if not settings.public_origin_enabled:
        direct_request_origin = _request_origin(request)
        if direct_request_origin is not None:
            trusted.add(direct_request_origin)
        forwarded_origin = _forwarded_origin(request) if request_uses_trusted_proxy_headers(request, settings) else None
        if forwarded_origin is not None:
            trusted.add(forwarded_origin)
    origin = _normalize_origin(request.headers.get("Origin", ""))
    if origin is not None:
        return origin in trusted
    referer_origin = _referer_origin(request.headers.get("Referer"))
    if referer_origin is not None:
        return referer_origin in trusted
    return False


async def enforce_session_csrf(request: Request, call_next):
    if request.method not in {"POST", "PATCH", "DELETE"}:
        return await call_next(request)
    if request.url.path in {"/api/v1/auth/login", "/api/v1/auth/register"}:
        return await call_next(request)
    state = getattr(request.app.state, "imghost", None)
    if state is None:
        return await call_next(request)
    if _request_uses_bearer_auth(request):
        return await call_next(request)
    if not request.cookies.get(state.settings.session_cookie_name):
        return await call_next(request)
    if not _has_trusted_csrf_source(request, state.settings):
        return JSONResponse({"detail": "CSRF protection blocked the request."}, status_code=403)
    return await call_next(request)
