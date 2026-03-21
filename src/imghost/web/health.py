from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .context import get_state

router = APIRouter()


@router.get("/health/live")
async def health_live() -> PlainTextResponse:
    return PlainTextResponse("ok")


@router.get("/health/ready")
async def health_ready(request: Request) -> JSONResponse:
    state = get_state(request)
    payload = await state.runtime_status()
    ready = payload["database"]["ok"] and payload["storage"]["ok"]
    if state.settings.redis_mode == "required" and payload["redis"]["configured"] and not payload["redis"]["reachable"]:
        ready = False
    payload["ok"] = bool(ready)
    return JSONResponse(payload, status_code=200 if ready else 503)
