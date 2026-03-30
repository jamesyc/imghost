from __future__ import annotations

import getpass
import os
import socket
from hashlib import sha256
from typing import Sequence
from uuid import uuid4

from fastapi import Request

from ..models import User
from ..public_origin import trusted_forwarded_client_ip
from .models import TelemetryActor, TelemetryProcessContext, TelemetryRequestContext


def user_actor(user: User, *, actor_type: str | None = None) -> TelemetryActor:
    return TelemetryActor(id=user.id, type=actor_type or ("admin" if user.is_admin else "user"), display=user.username)


def anonymous_actor() -> TelemetryActor:
    return TelemetryActor(id=None, type="anonymous")


def cli_actor() -> TelemetryActor:
    return TelemetryActor(id=None, type="cli", display=_safe_username())


def build_request_context(request: Request, *, auth_method: str | None = None) -> TelemetryRequestContext:
    settings = getattr(getattr(request.app.state, "imghost", None), "settings", None)
    client_ip = None
    if settings is not None:
        client_ip = trusted_forwarded_client_ip(request, settings)
    if client_ip is None and request.client is not None:
        client_ip = request.client.host
    route = getattr(request.scope.get("route"), "path", None)
    request_id = getattr(request.state, "request_id", None) or request.headers.get("X-Request-ID") or str(uuid4())
    correlation_id = (
        getattr(request.state, "correlation_id", None) or request.headers.get("X-Correlation-ID") or str(uuid4())
    )
    return TelemetryRequestContext(
        request_id=request_id,
        correlation_id=correlation_id,
        method=request.method,
        route=route,
        path=request.url.path,
        host=request.headers.get("Host"),
        origin=request.headers.get("Origin"),
        referer=request.headers.get("Referer"),
        user_agent=request.headers.get("User-Agent"),
        client_ip=client_ip,
        forwarded_for=request.headers.get("X-Forwarded-For"),
        auth_method=auth_method or getattr(request.state, "telemetry_auth_method", None),
    )


def build_runtime_process_context(source: str, *, command: str | None = None) -> TelemetryProcessContext:
    return TelemetryProcessContext(
        source=source,
        hostname=socket.gethostname(),
        pid=os.getpid(),
        command=command,
        build_id=os.getenv("GIT_SHA") or os.getenv("IMAGE_TAG"),
    )


def build_cli_process_context(argv: Sequence[str]) -> TelemetryProcessContext:
    return build_runtime_process_context("cli", command=" ".join(_redact_cli_argv(argv)))


def hash_client_ip(client_ip: str | None) -> str | None:
    if not client_ip:
        return None
    return sha256(client_ip.encode("utf-8")).hexdigest()


def _safe_username() -> str | None:
    try:
        return getpass.getuser()
    except Exception:
        return None


def _redact_cli_argv(argv: Sequence[str]) -> list[str]:
    sensitive_flags = {"--password"}
    redacted: list[str] = []
    hide_next = False
    for arg in argv:
        if hide_next:
            redacted.append("[REDACTED]")
            hide_next = False
            continue
        if arg in sensitive_flags:
            redacted.append(arg)
            hide_next = True
            continue
        if any(arg.startswith(f"{flag}=") for flag in sensitive_flags):
            flag, _, _ = arg.partition("=")
            redacted.append(f"{flag}=[REDACTED]")
            continue
        redacted.append(arg)
    return redacted
