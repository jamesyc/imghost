from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .request_context import get_state

router = APIRouter()


@router.get("/health/live")
async def health_live() -> PlainTextResponse:
    return PlainTextResponse("ok")


@router.get("/health/ready")
async def health_ready(request: Request) -> JSONResponse:
    state = get_state(request)
    payload = await state.readiness_status()
    return JSONResponse(payload, status_code=200 if payload["ok"] else 503)
