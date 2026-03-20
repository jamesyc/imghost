from __future__ import annotations

import logging
from urllib.parse import SplitResult, urlsplit

from fastapi import Request

from .config import Settings

logger = logging.getLogger(__name__)


def _normalize_origin(origin: str) -> str | None:
    candidate = origin.strip()
    if not candidate:
        return None
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return None
    host = (parsed.hostname or "").strip().lower()
    if not host or any(char in host for char in "/?#@"):
        return None
    port = parsed.port
    if (parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443):
        port = None
    netloc = host if port is None else f"{host}:{port}"
    return f"{parsed.scheme}://{netloc}"


def _trusted_origin_set(settings: Settings) -> set[str]:
    configured = {_normalize_origin(origin) for origin in settings.trusted_public_origins}
    trusted = {origin for origin in configured if origin is not None}
    fallback = _normalize_origin(settings.base_url)
    if fallback is not None:
        trusted.add(fallback)
    return trusted


def _header_first_value(request: Request, header_name: str) -> str:
    return request.headers.get(header_name, "").split(",", 1)[0].strip()


def _request_origin(request: Request) -> str | None:
    scheme = request.url.scheme.strip().lower()
    host = request.headers.get("Host", "").strip()
    if not host and request.url.netloc:
        host = request.url.netloc
    if not scheme or not host:
        return None
    return _normalize_origin(f"{scheme}://{host}")


def _forwarded_origin(request: Request) -> str | None:
    proto = _header_first_value(request, "X-Forwarded-Proto").lower()
    host = _header_first_value(request, "X-Forwarded-Host")
    if not proto or not host:
        return None
    return _normalize_origin(f"{proto}://{host}")


def public_base_url(request: Request, settings: Settings) -> str:
    trusted = _trusted_origin_set(settings)
    fallback = _normalize_origin(settings.base_url) or settings.base_url

    forwarded = _forwarded_origin(request)
    if forwarded is not None:
        if forwarded in trusted:
            return forwarded
        logger.warning("untrusted_forwarded_public_origin", extra={"candidate_origin": forwarded})
        return fallback

    request_origin = _request_origin(request)
    if request_origin is not None and request_origin in trusted:
        return request_origin
    if request_origin is not None:
        logger.warning("untrusted_request_public_origin", extra={"candidate_origin": request_origin})
    return fallback
