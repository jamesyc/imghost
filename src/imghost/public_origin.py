from __future__ import annotations

import logging
from ipaddress import ip_address, ip_network
from urllib.parse import urlsplit

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
    try:
        port = parsed.port
    except ValueError:
        return None
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


def _request_peer_host(request: Request) -> str | None:
    client = request.client
    if client is None:
        return None
    host = (client.host or "").strip()
    return host or None


def request_uses_trusted_proxy_headers(request: Request, settings: Settings) -> bool:
    if not settings.trusted_proxy_cidrs_enabled:
        return True
    peer_host = _request_peer_host(request)
    if peer_host is None:
        return False
    try:
        peer_ip = ip_address(peer_host)
    except ValueError:
        return False
    return any(peer_ip in ip_network(cidr, strict=False) for cidr in settings.trusted_proxy_cidrs)


def trusted_forwarded_client_ip(request: Request, settings: Settings) -> str | None:
    if not request_uses_trusted_proxy_headers(request, settings):
        return None

    for header_name in ("CF-Connecting-IP", "X-Real-IP", "X-Forwarded-For"):
        value = _header_first_value(request, header_name)
        if not value:
            continue
        try:
            return str(ip_address(value))
        except ValueError:
            continue
    return None


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


def trusted_forwarded_origin(request: Request, settings: Settings) -> str | None:
    if not request_uses_trusted_proxy_headers(request, settings):
        return None
    return _forwarded_origin(request)


def trusted_forwarded_proto(request: Request, settings: Settings) -> str | None:
    if not request_uses_trusted_proxy_headers(request, settings):
        return None
    proto = _header_first_value(request, "X-Forwarded-Proto").lower()
    return proto or None


def public_base_url(request: Request, settings: Settings) -> str:
    trusted = _trusted_origin_set(settings)
    fallback = _normalize_origin(settings.base_url) or settings.base_url
    telemetry = getattr(getattr(request.app.state, "imghost", None), "telemetry", None)

    forwarded = trusted_forwarded_origin(request, settings)
    if forwarded is None and _forwarded_origin(request) is not None and settings.trusted_proxy_cidrs_enabled:
        peer_host = _request_peer_host(request) or "unknown"
        if telemetry is None or telemetry.should_log_untrusted_origin("proxy_peer", peer_host):
            logger.warning(
                "forwarded_headers_ignored_untrusted_proxy",
                extra={"peer_host": peer_host, "path": request.url.path},
            )
    if not settings.public_origin_enabled:
        if forwarded is not None:
            return forwarded
        request_origin = _request_origin(request)
        if request_origin is not None:
            return request_origin
        return fallback

    if forwarded is not None:
        if forwarded in trusted:
            return forwarded
        if telemetry is None or telemetry.should_log_untrusted_origin("forwarded", forwarded):
            logger.warning(
                "untrusted_public_origin",
                extra={"source": "forwarded", "candidate_origin": forwarded, "fallback_origin": fallback, "path": request.url.path},
            )
        return fallback

    request_origin = _request_origin(request)
    if request_origin is not None and request_origin in trusted:
        return request_origin
    if request_origin is not None:
        if telemetry is None or telemetry.should_log_untrusted_origin("request", request_origin):
            logger.warning(
                "untrusted_public_origin",
                extra={"source": "request", "candidate_origin": request_origin, "fallback_origin": fallback, "path": request.url.path},
            )
    return fallback
