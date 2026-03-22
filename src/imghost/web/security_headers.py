from __future__ import annotations

from fastapi import Request

from ..public_origin import request_uses_trusted_proxy_headers
from .request_context import get_state

CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
        "object-src 'none'",
        "script-src 'self'",
        "script-src-attr 'none'",
        "style-src 'self'",
        "style-src-attr 'none'",
        "img-src 'self'",
        "media-src 'self' blob:",
        "font-src 'self'",
        "connect-src 'self'",
        "form-action 'self'",
        "frame-src 'none'",
        "worker-src 'none'",
    ]
)


def _request_is_effectively_https(request: Request) -> bool:
    if request.url.scheme.lower() == "https":
        return True

    state = get_state(request)
    if not request_uses_trusted_proxy_headers(request, state.settings):
        return False

    forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
    return forwarded_proto == "https"


async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)

    if _request_is_effectively_https(request):
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

    return response
