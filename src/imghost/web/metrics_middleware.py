from __future__ import annotations

from time import monotonic

from fastapi import Request


def _route_label(request: Request) -> str:
    route = getattr(request.scope.get("route"), "path", None)
    if isinstance(route, str) and route:
        return route
    if request.url.path.startswith("/static/"):
        return "/static/*"
    return "unmatched"


async def observe_http_metrics(request: Request, call_next):
    started_at = monotonic()
    try:
        response = await call_next(request)
    except Exception:
        state = getattr(request.app.state, "imghost", None)
        if state is not None and request.url.path != "/metrics":
            state.telemetry.observe_http_request(
                method=request.method,
                route=_route_label(request),
                status_code=500,
                duration_seconds=monotonic() - started_at,
            )
        raise
    if request.url.path != "/metrics":
        state = getattr(request.app.state, "imghost", None)
        if state is not None:
            state.telemetry.observe_http_request(
                method=request.method,
                route=_route_label(request),
                status_code=response.status_code,
                duration_seconds=monotonic() - started_at,
            )
    return response
