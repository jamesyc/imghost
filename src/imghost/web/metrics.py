from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST

from .request_context import get_state

router = APIRouter()


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    state = get_state(request)
    return Response(state.telemetry.render_metrics(), media_type=CONTENT_TYPE_LATEST)
