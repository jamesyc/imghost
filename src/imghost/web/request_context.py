from __future__ import annotations

from uuid import uuid4

from fastapi import Request

from ..app_state import AppState


def get_state(request: Request) -> AppState:
    return request.app.state.imghost


def correlation_id(request: Request) -> str:
    return request.headers.get("X-Correlation-ID") or str(uuid4())
