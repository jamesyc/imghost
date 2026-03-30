from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from imghost.telemetry import api as telemetry_api
from imghost.telemetry.api import Telemetry, build_telemetry
from imghost.telemetry.state import TelemetryState


class _RecordingService:
    def __init__(self) -> None:
        self.emitted: list[dict[str, object]] = []
        self.queries: list[dict[str, object]] = []
        self.audit_counts: list[object] = []
        self.audit_deletes: list[object] = []

    async def emit_event(self, **kwargs) -> None:
        self.emitted.append(kwargs)

    async def count_audit_events_older_than(self, before):
        self.audit_counts.append(before)
        return 7

    async def delete_audit_events_older_than(self, before):
        self.audit_deletes.append(before)
        return 5

    async def query_audit_log(self, **kwargs):
        self.queries.append(kwargs)
        return ["row-1"]


def _make_request() -> Request:
    app = FastAPI()
    app.state.imghost = SimpleNamespace(settings=None)
    scope = {
        "type": "http",
        "app": app,
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


def test_telemetry_facade_delegates_emit_and_query() -> None:
    service = _RecordingService()
    telemetry = Telemetry(service, TelemetryState())

    async def run() -> None:
        await telemetry.emit_event(event_type="test_event", action="test.action", result="success")
        rows = await telemetry.query_audit_log(limit=5)
        assert rows == ["row-1"]

    asyncio.run(run())

    assert service.emitted == [{"event_type": "test_event", "action": "test.action", "result": "success"}]
    assert service.queries == [
        {
            "event_type": None,
            "action": None,
            "result": None,
            "source": None,
            "actor_id": None,
            "user_id": None,
            "correlation_id": None,
            "request_id": None,
            "after": None,
            "before": None,
            "limit": 5,
            "offset": 0,
        }
    ]


def test_telemetry_facade_delegates_audit_retention_helpers() -> None:
    service = _RecordingService()
    telemetry = Telemetry(service, TelemetryState())

    async def run() -> None:
        import datetime as dt

        before = dt.datetime.now(dt.UTC)
        counted = await telemetry.count_audit_events_older_than(before)
        deleted = await telemetry.delete_audit_events_older_than(before)
        assert counted == 7
        assert deleted == 5
        assert service.audit_counts == [before]
        assert service.audit_deletes == [before]

    asyncio.run(run())


def test_telemetry_facade_delegates_state_helpers() -> None:
    telemetry = Telemetry(_RecordingService(), TelemetryState())

    telemetry.mark_subsystem_degraded("tasks", operation="enqueue", reason="redis_unavailable")
    degraded = telemetry.subsystem_snapshot("tasks", configured=True, default_mode="redis")
    assert degraded["degraded"] is True

    telemetry.mark_subsystem_recovered("tasks", operation="enqueue")
    recovered = telemetry.subsystem_snapshot("tasks", configured=True, default_mode="redis")
    assert recovered["degraded"] is False

    telemetry.record_task_failure(task_name="generate_thumbnail", details={"reason": "boom"})
    assert telemetry.last_task_failure == {"task_name": "generate_thumbnail", "reason": "boom"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("method_name", "helper_name", "kwargs"),
    [
        ("record_login_failed", "record_login_failed", {"login_identifier": "user@example.com", "reason": "invalid_password"}),
        ("record_login_succeeded", "record_login_succeeded", {"user": SimpleNamespace(id="user-1"), "remember_me": True}),
        ("record_auth_rate_limited", "record_auth_rate_limited", {"scope": "login", "method": "password"}),
        ("record_registration_denied", "record_registration_denied", {"username": "newuser", "email": "new@example.com", "reason": "registration_disabled"}),
        ("record_logout_succeeded", "record_logout_succeeded", {"user": SimpleNamespace(id="user-1")}),
        (
            "record_api_key_auth_failed",
            "record_api_key_auth_failed",
            {
                "actor": None,
                "object_type": "user_api",
                "object_id": "/api/v1/user/me",
                "reason": "invalid_key",
                "admin_denial": True,
            },
        ),
        ("record_api_key_authenticated", "record_api_key_authenticated", {"user": SimpleNamespace(id="user-1"), "api_key_id": "key-1"}),
        (
            "record_admin_access_denied",
            "record_admin_access_denied",
            {"actor": None, "object_type": "admin_page", "reason": "not_admin", "source": "web"},
        ),
        ("record_csrf_blocked", "record_csrf_blocked", {}),
        (
            "record_admin_api_read",
            "record_admin_api_read",
            {"admin": SimpleNamespace(id="admin-1"), "resource": "admin.users", "object_type": "admin_api", "object_id": "users", "metadata": {"count": 1}},
        ),
        (
            "record_admin_page_viewed",
            "record_admin_page_viewed",
            {"user": SimpleNamespace(id="admin-1"), "page_name": "admin.overview", "object_id": "overview"},
        ),
        ("record_oauth_denied", "record_oauth_denied", {"reason": "invalid_state", "actor": None, "object_id": "google"}),
        (
            "record_oauth_succeeded",
            "record_oauth_succeeded",
            {"user": SimpleNamespace(id="user-1"), "provider": "google", "provider_uid": "google-1", "outcome": "linked"},
        ),
        ("record_oauth_disconnected", "record_oauth_disconnected", {"user": SimpleNamespace(id="user-1"), "provider": "google"}),
    ],
)
async def test_telemetry_facade_delegates_request_methods(monkeypatch, method_name: str, helper_name: str, kwargs: dict[str, object]) -> None:
    telemetry = Telemetry(_RecordingService(), TelemetryState())
    request = _make_request()
    calls: list[tuple[object, object, dict[str, object]]] = []

    async def _fake_helper(service, req, **helper_kwargs) -> None:
        calls.append((service, req, helper_kwargs))

    monkeypatch.setattr(telemetry_api, helper_name, _fake_helper)

    await getattr(telemetry, method_name)(request, **kwargs)

    assert len(calls) == 1
    service, req, helper_kwargs = calls[0]
    assert service is telemetry._service
    assert req is request
    assert helper_kwargs == kwargs


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("method_name", "expected"),
    [
        ("record_system_startup", {"event_type": "system_startup", "action": "system.startup", "source": "system"}),
        ("record_system_shutdown", {"event_type": "system_shutdown", "action": "system.shutdown", "source": "system"}),
        ("record_worker_started_event", {"event_type": "worker_started", "action": "worker.start", "source": "worker"}),
        ("record_worker_stopped_event", {"event_type": "worker_stopped", "action": "worker.stop", "source": "worker"}),
    ],
)
async def test_telemetry_facade_delegates_system_event_methods(monkeypatch, method_name: str, expected: dict[str, str]) -> None:
    telemetry = Telemetry(_RecordingService(), TelemetryState())
    calls: list[tuple[object, dict[str, object]]] = []

    async def _fake_system_event(service, **kwargs) -> None:
        calls.append((service, kwargs))

    monkeypatch.setattr(telemetry_api, "record_system_event", _fake_system_event)

    await getattr(telemetry, method_name)(metadata={"ok": True})

    assert calls == [(telemetry._service, {**expected, "metadata": {"ok": True}})]


@pytest.mark.anyio
async def test_telemetry_facade_delegates_bootstrap_admin_promoted(monkeypatch) -> None:
    telemetry = Telemetry(_RecordingService(), TelemetryState())
    calls: list[tuple[object, dict[str, object]]] = []

    async def _fake_system_event(service, **kwargs) -> None:
        calls.append((service, kwargs))

    monkeypatch.setattr(telemetry_api, "record_system_event", _fake_system_event)

    await telemetry.record_bootstrap_admin_promoted(user_id="user-1", username="admin")

    assert calls == [
        (
            telemetry._service,
            {
                "event_type": "system_bootstrap_admin_promoted",
                "action": "system.bootstrap_admin.promote",
                "source": "system",
                "object_type": "user",
                "object_id": "user-1",
                "metadata": {"username": "admin"},
            },
        )
    ]


@pytest.mark.anyio
async def test_telemetry_facade_delegates_cli_command(monkeypatch) -> None:
    telemetry = Telemetry(_RecordingService(), TelemetryState())
    calls: list[tuple[object, dict[str, object]]] = []

    async def _fake_cli_command(service, **kwargs) -> None:
        calls.append((service, kwargs))

    monkeypatch.setattr(telemetry_api, "record_cli_command", _fake_cli_command)

    await telemetry.record_cli_command(
        action="cli.test",
        object_type="cli_command",
        object_id="test",
        metadata={"value": 1},
        argv=["test"],
    )

    assert calls == [
        (
            telemetry._service,
            {
                "action": "cli.test",
                "object_type": "cli_command",
                "object_id": "test",
                "metadata": {"value": 1},
                "argv": ["test"],
            },
        )
    ]


def test_telemetry_facade_delegates_thumbnail_failure(monkeypatch) -> None:
    telemetry = Telemetry(_RecordingService(), TelemetryState())
    media = SimpleNamespace(id="media-1")
    error = RuntimeError("boom")
    calls: list[dict[str, object]] = []

    def _fake_record_thumbnail_failure(**kwargs) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(telemetry_api, "record_thumbnail_failure", _fake_record_thumbnail_failure)

    telemetry.record_thumbnail_failure(media=media, correlation_id="corr-1", reason="failed", error=error)

    assert calls == [
        {
            "telemetry_state": telemetry._state,
            "media": media,
            "correlation_id": "corr-1",
            "reason": "failed",
            "error": error,
        }
    ]


def test_build_telemetry_wires_sinks_state_and_subscribers(monkeypatch) -> None:
    created = {}

    class _FakeDbSink:
        def __init__(self, database) -> None:
            created["db_sink_database"] = database

    class _FakeJsonSink:
        def __init__(self, logger) -> None:
            created["json_logger_name"] = logger.name

    class _FakeService:
        def __init__(self, sinks, *, query_backend=None) -> None:
            created["service_sinks"] = sinks
            created["query_backend"] = query_backend

    def _fake_register(event_bus, service) -> None:
        created["registered_bus"] = event_bus
        created["registered_service"] = service

    monkeypatch.setattr(telemetry_api, "PostgresTelemetrySink", _FakeDbSink)
    monkeypatch.setattr(telemetry_api, "JsonLogTelemetrySink", _FakeJsonSink)
    monkeypatch.setattr(telemetry_api, "TelemetryService", _FakeService)
    monkeypatch.setattr(telemetry_api, "register_telemetry_subscribers", _fake_register)

    database = object()
    event_bus = object()
    telemetry = build_telemetry(database, event_bus)

    assert isinstance(telemetry, Telemetry)
    assert isinstance(telemetry._state, TelemetryState)
    assert created["db_sink_database"] is database
    assert created["json_logger_name"] == "imghost.telemetry"
    assert created["service_sinks"] and len(created["service_sinks"]) == 2
    assert created["query_backend"] is created["service_sinks"][0]
    assert created["registered_bus"] is event_bus
    assert created["registered_service"] is telemetry._service
