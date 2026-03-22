from __future__ import annotations

from uuid import uuid4

from fastapi import Request

from ..app_state import AppState


def get_state(request: Request) -> AppState:
    return request.app.state.imghost


def correlation_id(request: Request) -> str:
    existing = getattr(request.state, "correlation_id", None)
    if existing is not None:
        return existing
    value = request.headers.get("X-Correlation-ID") or str(uuid4())
    request.state.correlation_id = value
    return value


def request_id(request: Request) -> str:
    existing = getattr(request.state, "request_id", None)
    if existing is not None:
        return existing
    value = request.headers.get("X-Request-ID") or str(uuid4())
    request.state.request_id = value
    return value


async def assign_request_context(request: Request, call_next):
    request.state.correlation_id = request.headers.get("X-Correlation-ID") or str(uuid4())
    request.state.request_id = request.headers.get("X-Request-ID") or str(uuid4())
    response = await call_next(request)
    response.headers.setdefault("X-Correlation-ID", request.state.correlation_id)
    response.headers.setdefault("X-Request-ID", request.state.request_id)
    return response
