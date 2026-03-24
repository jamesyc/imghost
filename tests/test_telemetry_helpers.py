from __future__ import annotations

import logging
from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from imghost.config import Settings
from imghost.models import Media, utcnow
from imghost.telemetry.context import anonymous_actor
from imghost.telemetry.helpers import (
    emit_request_action,
    record_api_key_auth_failed,
    record_admin_api_read,
    record_cli_command,
    record_oauth_succeeded,
    record_system_event,
    record_thumbnail_failure,
)
from imghost.telemetry.context import build_request_context, hash_client_ip
from imghost.telemetry.models import TelemetryObject
from imghost.telemetry.state import TelemetryState


class DummyTelemetry:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def emit_event(self, **kwargs) -> None:
        self.calls.append(kwargs)


def make_settings() -> Settings:
    return Settings(
        base_url="http://localhost:8000",
        public_origin_enabled=False,
        trusted_public_origins=(),
        trusted_proxy_cidrs_enabled=False,
        trusted_proxy_cidrs=(),
        database_url="postgresql://imghost:imghost@localhost:5432/imghost",
        data_dir=Path("/tmp/imghost-test"),
        redis_url=None,
        redis_password=None,
        redis_mode="auto",
        redis_prefix="imghost",
        storage_backend="filesystem",
        s3_endpoint_url=None,
        s3_access_key_id=None,
        s3_secret_access_key=None,
        s3_bucket=None,
        s3_region="garage",
        secret_key="dev-secret-key",
        session_cookie_name="imghost_session",
        session_cookie_secure=False,
        session_redis_fail_closed=False,
        session_remember_days=30,
        max_upload_bytes=50 * 1024 * 1024,
        anon_expiry_hours=24,
        max_pixel_megapixels=50,
        default_user_quota_bytes=2 * 1024 * 1024 * 1024,
        server_quota_bytes=0,
        video_thumb_frames=10,
        task_queue_mode="async",
        task_worker_enabled=True,
        thumbnail_worker_count=1,
        google_oauth_enabled=False,
        google_client_id=None,
        google_client_secret=None,
    )


def make_request() -> Request:
    app = FastAPI()
    app.state.imghost = SimpleNamespace(settings=make_settings())
    scope = {
        "type": "http",
        "app": app,
        "method": "POST",
        "path": "/api/v1/test",
        "headers": [
            (b"host", b"localhost:8000"),
            (b"user-agent", b"pytest"),
            (b"x-request-id", b"req-1"),
            (b"x-correlation-id", b"corr-1"),
        ],
        "client": ("127.0.0.1", 12345),
        "scheme": "http",
        "query_string": b"",
    }
    request = Request(scope)
    request.state.telemetry_auth_method = None
    return request


def make_request_without_settings() -> Request:
    app = FastAPI()
    scope = {
        "type": "http",
        "app": app,
        "method": "GET",
        "path": "/public",
        "headers": [(b"host", b"localhost:8000")],
        "client": ("127.0.0.9", 5000),
        "scheme": "http",
        "query_string": b"",
    }
    request = Request(scope)
    request.state.telemetry_auth_method = "session"
    return request


@pytest.mark.anyio
async def test_emit_request_action_populates_context() -> None:
    telemetry = DummyTelemetry()
    request = make_request()

    await emit_request_action(
        telemetry,
        request,
        event_type="test_event",
        action="test.action",
        result="success",
        actor=anonymous_actor(),
        object=TelemetryObject(type="test", id="obj-1"),
        metadata={"hello": "world"},
        auth_method="password",
    )

    assert len(telemetry.calls) == 1
    call = telemetry.calls[0]
    assert call["metadata"] == {"hello": "world", "correlation_id": "corr-1", "source": "web"}
    assert call["reason"] is None
    request_context = call["request"]
    assert request_context is not None
    assert request_context.request_id == "req-1"
    assert request_context.correlation_id == "corr-1"
    assert request_context.auth_method == "password"
    assert call["process"] is not None
    assert call["process"].source == "web"
    assert call["actor_ip_hash"]


@pytest.mark.anyio
async def test_emit_request_action_uses_request_state_auth_method_when_not_overridden() -> None:
    telemetry = DummyTelemetry()
    request = make_request()
    request.state.telemetry_auth_method = "session"

    await emit_request_action(
        telemetry,
        request,
        event_type="test_event",
        action="test.action",
        result="success",
        actor=anonymous_actor(),
        object=TelemetryObject(type="test", id="obj-1"),
    )

    assert telemetry.calls[0]["request"].auth_method == "session"


@pytest.mark.anyio
async def test_emit_request_action_preserves_explicit_source_and_correlation_id() -> None:
    telemetry = DummyTelemetry()
    request = make_request()

    await emit_request_action(
        telemetry,
        request,
        event_type="test_event",
        action="test.action",
        result="success",
        actor=anonymous_actor(),
        object=TelemetryObject(type="test", id="obj-1"),
        metadata={"correlation_id": "manual-corr", "source": "manual"},
    )

    assert telemetry.calls[0]["metadata"]["correlation_id"] == "manual-corr"
    assert telemetry.calls[0]["metadata"]["source"] == "manual"


def test_build_request_context_falls_back_to_request_client_without_settings() -> None:
    request = make_request_without_settings()

    context = build_request_context(request)

    assert context.client_ip == "127.0.0.9"
    assert context.auth_method == "session"
    assert context.route is None


def test_hash_client_ip_handles_empty_and_nonempty_values() -> None:
    assert hash_client_ip(None) is None
    assert hash_client_ip("") is None
    assert hash_client_ip("127.0.0.1")


@pytest.mark.anyio
async def test_record_admin_api_read_uses_semantic_defaults() -> None:
    telemetry = DummyTelemetry()
    request = make_request()
    admin = SimpleNamespace(id="user-1", username="admin", is_admin=True)

    await record_admin_api_read(
        telemetry,
        request,
        admin=admin,
        resource="admin.users",
        metadata={"result_count": 3},
    )

    call = telemetry.calls[0]
    assert call["event_type"] == "admin_api_read"
    assert call["action"] == "admin.users.read"
    assert call["metadata"]["resource"] == "admin.users"
    assert call["metadata"]["result_count"] == 3
    assert call["metadata"]["source"] == "api"
    assert call["metadata"]["correlation_id"] == "corr-1"
    assert call["object"].type == "admin_api"
    assert call["process"].source == "api"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("admin_denial", "expected_event_type"),
    [(False, "api_key_invalid"), (True, "admin_access_denied")],
)
async def test_record_api_key_auth_failed_uses_expected_event_type(admin_denial: bool, expected_event_type: str) -> None:
    telemetry = DummyTelemetry()
    request = make_request()

    await record_api_key_auth_failed(
        telemetry,
        request,
        actor=None,
        object_type="user_api",
        object_id="/api/v1/user/me",
        reason="invalid_api_key",
        admin_denial=admin_denial,
    )

    call = telemetry.calls[0]
    assert call["event_type"] == expected_event_type
    assert call["reason"] == "invalid_api_key"
    assert call["metadata"]["source"] == "api"
    assert call["request"].auth_method == "api_key"


@pytest.mark.anyio
async def test_record_system_action_emits_runtime_event_without_request() -> None:
    telemetry = DummyTelemetry()

    await record_system_event(
        telemetry,
        event_type="system_startup",
        action="system.startup",
        source="system",
        metadata={"redis_enabled": True},
    )

    call = telemetry.calls[0]
    assert call["event_type"] == "system_startup"
    assert call["action"] == "system.startup"
    assert call["actor"].type == "system"
    assert call["object"].type == "system"
    assert call["metadata"]["source"] == "system"
    assert call["metadata"]["redis_enabled"] is True
    assert call.get("request") is None
    assert call["process"].source == "system"


@pytest.mark.anyio
async def test_record_cli_command_executed_uses_cli_actor() -> None:
    telemetry = DummyTelemetry()

    await record_cli_command(
        telemetry,
        action="cli.init_storage",
        object_type="cli_command",
        object_id="init-storage",
        metadata={"command": "init-storage"},
        argv=["init-storage"],
    )

    call = telemetry.calls[0]
    assert call["event_type"] == "cli_command_executed"
    assert call["action"] == "cli.init_storage"
    assert call["actor"].type == "cli"
    assert call["metadata"]["source"] == "cli"
    assert call["metadata"]["command"] == "init-storage"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("outcome", "expected_event_type", "expected_action"),
    [
        ("linked", "oauth_linked", "oauth.linked"),
        ("login", "oauth_login", "oauth.login.success"),
    ],
)
async def test_record_oauth_succeeded_handles_link_and_login_outcomes(
    outcome: str,
    expected_event_type: str,
    expected_action: str,
) -> None:
    telemetry = DummyTelemetry()
    request = make_request()
    user = SimpleNamespace(id="user-1", username="demo", is_admin=False)

    await record_oauth_succeeded(
        telemetry,
        request,
        user=user,
        provider="google",
        provider_uid="google-1",
        outcome=outcome,
    )

    call = telemetry.calls[0]
    assert call["event_type"] == expected_event_type
    assert call["action"] == expected_action
    assert call["metadata"]["provider"] == "google"
    assert call["metadata"]["provider_uid"] == "google-1"
    assert call["metadata"]["outcome"] == outcome


def test_record_thumbnail_failure_logs_and_updates_state(caplog) -> None:
    telemetry_state = TelemetryState()
    media = Media(
        id="media-1",
        album_id="album-1",
        user_id=None,
        filename_orig="test.png",
        media_type="image",
        format="png",
        mime_type="image/png",
        storage_key="media/original/test.png",
        thumb_key=None,
        thumb_is_orig=False,
        thumb_status="processing",
        file_size=123,
        thumb_size=None,
        width=100,
        height=100,
        duration_secs=None,
        is_animated=False,
        codec_hint=None,
        position=0,
        created_at=utcnow(),
    )

    with caplog.at_level(logging.WARNING):
        record_thumbnail_failure(
            telemetry_state=telemetry_state,
            media=media,
            correlation_id="corr-1",
            reason="thumbnail_generate_failed",
            error=RuntimeError("boom"),
        )

    assert any(record.message == "thumbnail_generation_failed" for record in caplog.records)
    assert telemetry_state.last_task_failure is not None
    assert telemetry_state.last_task_failure["task_name"] == "generate_thumbnail"
    assert telemetry_state.last_task_failure["reason"] == "thumbnail_generate_failed"
    assert telemetry_state.last_task_failure["media_id"] == "media-1"


def test_record_thumbnail_failure_without_state_still_logs(caplog) -> None:
    media = Media(
        id="media-2",
        album_id="album-1",
        user_id=None,
        filename_orig="test.png",
        media_type="image",
        format="png",
        mime_type="image/png",
        storage_key="media/original/test.png",
        thumb_key=None,
        thumb_is_orig=False,
        thumb_status="processing",
        file_size=123,
        thumb_size=None,
        width=100,
        height=100,
        duration_secs=None,
        is_animated=False,
        codec_hint=None,
        position=0,
        created_at=utcnow(),
    )

    with caplog.at_level(logging.WARNING):
        record_thumbnail_failure(
            telemetry_state=None,
            media=media,
            correlation_id="corr-2",
            reason="thumbnail_generate_failed",
            error=RuntimeError("boom"),
        )

    assert any(record.message == "thumbnail_generation_failed" for record in caplog.records)
