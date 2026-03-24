from __future__ import annotations

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

from imghost.main import app
from imghost.telemetry.api import Telemetry
from imghost.telemetry.metrics import TelemetryMetrics
from imghost.telemetry.state import TelemetryState
from imghost.web.metrics_middleware import _route_label, observe_http_metrics

from .helpers import PNG_1X1, wait_for_thumbnail


class _RecordingService:
    async def emit_event(self, **kwargs) -> None:
        return None

    async def query_audit_log(self, **kwargs):
        return []


def _make_request() -> Request:
    app_instance = FastAPI()
    app_instance.state.imghost = SimpleNamespace(settings=None)
    scope = {
        "type": "http",
        "app": app_instance,
        "method": "POST",
        "path": "/api/v1/test",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "scheme": "http",
        "query_string": b"",
    }
    request = Request(scope)
    request.state.telemetry_auth_method = None
    return request


def _metrics_text(telemetry: Telemetry) -> str:
    return telemetry.render_metrics().decode("utf-8")


def _assert_metric_line_contains(text: str, metric_name: str, *parts: str) -> None:
    for line in text.splitlines():
        if metric_name not in line:
            continue
        if all(part in line for part in parts):
            return
    raise AssertionError(f"Missing metric line for {metric_name!r} containing parts {parts!r}")


def test_telemetry_metrics_render_state_and_counters() -> None:
    telemetry = Telemetry(_RecordingService(), TelemetryState(), TelemetryMetrics())
    request = _make_request()
    user = SimpleNamespace(id="user-1", username="demo", is_admin=False)

    async def run() -> None:
        await telemetry.record_login_failed(request, login_identifier="user@example.com", reason="invalid_credentials")
        await telemetry.record_login_succeeded(request, user=user, remember_me=True)
        await telemetry.record_oauth_denied(request, reason="invalid_state")
        await telemetry.record_oauth_succeeded(
            request,
            user=user,
            provider="google",
            provider_uid="google-1",
            outcome="linked",
        )
        await telemetry.record_oauth_disconnected(request, user=user, provider="google")

    import asyncio

    asyncio.run(run())
    telemetry.observe_http_request(method="GET", route="/health/live", status_code=200, duration_seconds=0.05)
    telemetry.record_upload(
        result="success",
        media_type="image",
        actor_kind="anonymous",
        source="web",
        byte_count=len(PNG_1X1),
        duration_seconds=0.02,
    )
    telemetry.record_thumbnail_job(result="success", media_type="image", reason="none", duration_seconds=0.01)
    telemetry.mark_subsystem_degraded("tasks", operation="enqueue", reason="redis_unavailable")
    telemetry.mark_subsystem_recovered("tasks", operation="enqueue")
    telemetry.mark_worker_started()
    telemetry.mark_worker_stopped()
    telemetry.record_task_enqueued(queue="thumbnails", task_name="generate_thumbnail")

    text = _metrics_text(telemetry)
    assert 'imghost_http_requests_total{method="GET",route="/health/live",status_class="2xx"} 1.0' in text
    assert 'imghost_uploads_total{actor_kind="anonymous",media_type="image",result="success",source="web"} 1.0' in text
    assert f'imghost_upload_bytes_total{{actor_kind="anonymous",media_type="image",source="web"}} {float(len(PNG_1X1))}' in text
    assert 'imghost_thumbnail_jobs_total{media_type="image",reason="none",result="success"} 1.0' in text
    assert 'imghost_auth_events_total{event="login",method="password",result="denied"} 1.0' in text
    assert 'imghost_auth_events_total{event="login",method="password",result="success"} 1.0' in text
    assert 'imghost_oauth_events_total{event="callback",provider="google",result="denied"} 1.0' in text
    assert 'imghost_oauth_events_total{event="link",provider="google",result="success"} 1.0' in text
    assert 'imghost_oauth_events_total{event="disconnect",provider="google",result="success"} 1.0' in text
    _assert_metric_line_contains(text, "imghost_subsystem_transitions_total", 'subsystem="tasks"', 'state="degraded"', " 1.0")
    _assert_metric_line_contains(text, "imghost_subsystem_transitions_total", 'subsystem="tasks"', 'state="recovered"', " 1.0")
    assert 'imghost_subsystem_degraded{subsystem="tasks"} 0.0' in text
    assert "imghost_worker_running 0.0" in text
    assert 'imghost_tasks_enqueued_total{queue="thumbnails",task_name="generate_thumbnail"} 1.0' in text


def test_telemetry_without_metrics_is_noop_and_renders_empty_bytes() -> None:
    telemetry = Telemetry(_RecordingService(), TelemetryState(), None)

    telemetry.observe_http_request(method="GET", route="/health/live", status_code=200, duration_seconds=0.1)
    telemetry.record_upload(result="success", media_type="image", actor_kind="anonymous", source="web", byte_count=1)
    telemetry.record_thumbnail_job(result="failed", media_type="image", reason="boom", duration_seconds=0.1)
    telemetry.mark_subsystem_degraded("tasks", operation="enqueue", reason="redis_down")
    telemetry.mark_subsystem_recovered("tasks", operation="enqueue")
    telemetry.mark_worker_started()
    telemetry.mark_worker_stopped()
    telemetry.record_task_enqueued(queue="thumbnails", task_name="generate_thumbnail")

    assert telemetry.render_metrics() == b""


def test_metrics_clamp_negative_values_and_skip_non_success_upload_bytes() -> None:
    metrics = TelemetryMetrics()

    metrics.observe_http_request(method="get", route="/weird", status_code=-1, duration_seconds=-5.0)
    metrics.record_upload(
        result="rejected",
        media_type="video",
        actor_kind="user",
        source="api",
        byte_count=999,
        duration_seconds=-1.0,
    )
    metrics.record_upload(
        result="success",
        media_type="video",
        actor_kind="user",
        source="api",
        byte_count=-9,
        duration_seconds=1.0,
    )
    metrics.record_thumbnail_job(result="failed", media_type="video", reason=None, duration_seconds=-2.0)

    text = metrics.render().decode("utf-8")
    assert 'imghost_http_requests_total{method="GET",route="/weird",status_class="0xx"} 1.0' in text
    assert 'imghost_uploads_total{actor_kind="user",media_type="video",result="rejected",source="api"} 1.0' in text
    assert 'imghost_uploads_total{actor_kind="user",media_type="video",result="success",source="api"} 1.0' in text
    assert 'imghost_upload_bytes_total{actor_kind="user",media_type="video",source="api"} 0.0' in text
    assert 'imghost_thumbnail_jobs_total{media_type="video",reason="none",result="failed"} 1.0' in text


def test_metrics_repeated_subsystem_and_worker_updates_accumulate_current_state() -> None:
    metrics = TelemetryMetrics()

    metrics.mark_subsystem_degraded(subsystem="tasks")
    metrics.mark_subsystem_degraded(subsystem="tasks")
    metrics.mark_subsystem_recovered(subsystem="tasks")
    metrics.mark_subsystem_recovered(subsystem="tasks")
    metrics.mark_worker_started()
    metrics.mark_worker_started()
    metrics.mark_worker_stopped()
    metrics.record_task_enqueued(queue="default", task_name="demo")
    metrics.record_task_enqueued(queue="default", task_name="demo")

    text = metrics.render().decode("utf-8")
    _assert_metric_line_contains(text, "imghost_subsystem_transitions_total", 'subsystem="tasks"', 'state="degraded"', " 2.0")
    _assert_metric_line_contains(text, "imghost_subsystem_transitions_total", 'subsystem="tasks"', 'state="recovered"', " 2.0")
    assert 'imghost_subsystem_degraded{subsystem="tasks"} 0.0' in text
    assert "imghost_worker_running 0.0" in text
    assert 'imghost_tasks_enqueued_total{queue="default",task_name="demo"} 2.0' in text


def test_route_label_uses_route_static_and_unmatched_cases() -> None:
    app_instance = FastAPI()
    route_request = Request(
        {
            "type": "http",
            "app": app_instance,
            "method": "GET",
            "path": "/users/1",
            "headers": [],
            "scheme": "http",
            "query_string": b"",
            "route": SimpleNamespace(path="/users/{user_id}"),
        }
    )
    static_request = Request(
        {
            "type": "http",
            "app": app_instance,
            "method": "GET",
            "path": "/static/app.js",
            "headers": [],
            "scheme": "http",
            "query_string": b"",
        }
    )
    unmatched_request = Request(
        {
            "type": "http",
            "app": app_instance,
            "method": "GET",
            "path": "/no-match",
            "headers": [],
            "scheme": "http",
            "query_string": b"",
        }
    )

    assert _route_label(route_request) == "/users/{user_id}"
    assert _route_label(static_request) == "/static/*"
    assert _route_label(unmatched_request) == "unmatched"


def test_observe_http_metrics_middleware_records_success_exception_and_skips_metrics_route() -> None:
    telemetry = Telemetry(_RecordingService(), TelemetryState(), TelemetryMetrics())
    app_instance = FastAPI()
    app_instance.state.imghost = SimpleNamespace(telemetry=telemetry)

    async def run_success(path: str, route_obj=None) -> None:
        request = Request(
            {
                "type": "http",
                "app": app_instance,
                "method": "GET",
                "path": path,
                "headers": [],
                "scheme": "http",
                "query_string": b"",
                **({"route": route_obj} if route_obj is not None else {}),
            }
        )

        async def call_next(_request):
            return PlainTextResponse("ok", status_code=204)

        await observe_http_metrics(request, call_next)

    async def run_failure(path: str, route_obj=None) -> None:
        request = Request(
            {
                "type": "http",
                "app": app_instance,
                "method": "POST",
                "path": path,
                "headers": [],
                "scheme": "http",
                "query_string": b"",
                **({"route": route_obj} if route_obj is not None else {}),
            }
        )

        async def call_next(_request):
            raise RuntimeError("boom")

        try:
            await observe_http_metrics(request, call_next)
        except RuntimeError:
            pass

    import asyncio

    asyncio.run(run_success("/users/1", SimpleNamespace(path="/users/{user_id}")))
    asyncio.run(run_success("/metrics"))
    asyncio.run(run_failure("/missing"))

    text = telemetry.render_metrics().decode("utf-8")
    assert 'imghost_http_requests_total{method="GET",route="/users/{user_id}",status_class="2xx"} 1.0' in text
    assert 'imghost_http_requests_total{method="POST",route="unmatched",status_class="5xx"} 1.0' in text
    assert 'route="/metrics"' not in text


def test_observe_http_metrics_middleware_without_app_state_does_not_crash() -> None:
    app_instance = FastAPI()
    request = Request(
        {
            "type": "http",
            "app": app_instance,
            "method": "GET",
            "path": "/health/live",
            "headers": [],
            "scheme": "http",
            "query_string": b"",
        }
    )

    async def call_next(_request):
        return PlainTextResponse("ok", status_code=200)

    import asyncio

    response = asyncio.run(observe_http_metrics(request, call_next))
    assert response.status_code == 200


def test_metrics_route_reports_http_upload_and_thumbnail_metrics(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        health = client.get("/health/live")
        assert health.status_code == 200

        login = client.post("/api/v1/auth/login", json={"login": "nobody", "password": "bad"})
        assert login.status_code == 401

        uploaded = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Metrics"},
        )
        assert uploaded.status_code == 200
        media_id = uploaded.json()["items"][0]["media_id"]
        wait_for_thumbnail(client, media_id)

        metrics_response = client.get("/metrics")
        assert metrics_response.status_code == 200
        assert metrics_response.headers["content-type"].startswith("text/plain; version=")
        body = metrics_response.text

        assert 'imghost_http_requests_total{method="GET",route="/health/live",status_class="2xx"} 1.0' in body
        assert 'imghost_http_requests_total{method="POST",route="/api/v1/auth/login",status_class="4xx"} 1.0' in body
        assert 'imghost_http_requests_total{method="POST",route="/api/v1/upload",status_class="2xx"} 1.0' in body
        assert 'route="/metrics"' not in body
        assert 'imghost_auth_events_total{event="login",method="password",result="denied"} 1.0' in body
        assert 'imghost_uploads_total{actor_kind="anonymous",media_type="image",result="success",source="web"} 1.0' in body
        assert f'imghost_upload_bytes_total{{actor_kind="anonymous",media_type="image",source="web"}} {float(len(PNG_1X1))}' in body
        assert 'imghost_tasks_enqueued_total{queue="thumbnails",task_name="generate_thumbnail"} 1.0' in body
        assert 'imghost_thumbnail_jobs_total{media_type="image",reason="none",result="success"} 1.0' in body
