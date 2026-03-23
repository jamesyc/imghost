from __future__ import annotations

import asyncio
import logging
import os

import asyncpg
from fastapi.testclient import TestClient
import pytest

from imghost.app_state import AppState
from imghost.audit.context import anonymous_actor
from imghost.audit.models import AuditObject
from imghost.audit.service import AuditService
from imghost.audit.sinks.jsonlog import JsonLogAuditSink
from imghost.config import load_settings
from imghost.main import app

from .helpers import browser_session_headers, create_admin_and_api_key, create_user_and_api_key, set_user_password


class _RecordingSink:
    def __init__(self) -> None:
        self.records = []

    async def write(self, record) -> None:
        self.records.append(record)


class _FailingSink:
    async def write(self, record) -> None:
        raise RuntimeError("sink failed")


class _QueryBackend:
    def __init__(self, rows) -> None:
        self.rows = rows
        self.calls = []

    async def query_audit_log(self, **kwargs):
        self.calls.append(kwargs)
        return list(self.rows)


def test_cli_create_user_and_issue_api_key_are_audited(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    user_id, admin_key = create_admin_and_api_key(capsys, username="cliauditadmin", email="cliauditadmin@example.com")

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "cli_command_executed", "user_id": user_id},
        )
        assert response.status_code == 200
        payload = response.json()
        commands = {item["metadata"]["command"] for item in payload}
        assert {"create-user", "issue-api-key"} <= commands
        create_user_event = next(item for item in payload if item["metadata"]["command"] == "create-user")
        issue_key_event = next(item for item in payload if item["metadata"]["command"] == "issue-api-key")
        assert create_user_event["metadata"]["process"]["source"] == "cli"
        assert create_user_event["target_id"] == user_id
        assert issue_key_event["target_id"] == user_id
        assert "api_key" not in issue_key_event["metadata"]


def test_invalid_api_key_is_audited_with_request_context(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="invalidkeyadmin", email="invalidkeyadmin@example.com")

    with TestClient(app) as client:
        denied = client.get(
            "/api/v1/user/me",
            headers={"Authorization": "Bearer not-a-real-key", "X-Correlation-ID": "invalid-key-flow"},
        )
        assert denied.status_code == 401

        audit = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "api_key_invalid", "correlation_id": "invalid-key-flow"},
        )
        assert audit.status_code == 200
        payload = audit.json()
        assert len(payload) == 1
        assert payload[0]["result"] == "denied"
        assert payload[0]["method"] == "GET"
        assert payload[0]["route"] == "/api/v1/user/me"
        assert payload[0]["request_id"] is not None
        assert payload[0]["source"] == "api"
        assert payload[0]["actor_type"] == "anonymous"


def test_admin_reads_are_audited_for_api_and_pages(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")

    admin_id, admin_key = create_admin_and_api_key(capsys, username="readadmin", email="readadmin@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, admin_id, "admin-pass")
        login = client.post(
            "/api/v1/auth/login",
            headers={"X-Correlation-ID": "admin-read-login"},
            json={"login": "readadmin", "password": "admin-pass"},
        )
        assert login.status_code == 200

        api_read = client.get(
            "/api/v1/admin/users",
            headers={"Authorization": f"Bearer {admin_key}", "X-Correlation-ID": "admin-api-read"},
        )
        assert api_read.status_code == 200

        page_read = client.get(
            "/admin",
            headers={"X-Correlation-ID": "admin-page-read", **browser_session_headers("https://testserver", "/admin")},
        )
        assert page_read.status_code == 200

        api_audit = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "admin_api_read", "correlation_id": "admin-api-read"},
        )
        assert api_audit.status_code == 200
        api_payload = api_audit.json()
        assert len(api_payload) == 1
        assert api_payload[0]["metadata"]["resource"] == "admin.users"
        assert api_payload[0]["route"] == "/api/v1/admin/users"

        page_audit = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "admin_page_viewed", "correlation_id": "admin-page-read"},
        )
        assert page_audit.status_code == 200
        page_payload = page_audit.json()
        assert len(page_payload) == 1
        assert page_payload[0]["metadata"]["page"] == "admin.overview"
        assert page_payload[0]["route"] == "/admin"


def test_csrf_denial_is_audited(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "audit-secret")

    _, admin_key = create_admin_and_api_key(capsys, username="csrfwatchadmin", email="csrfwatchadmin@example.com")
    user_id, _ = create_user_and_api_key(capsys, username="csrfsubject", email="csrfsubject@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, user_id, "user-pass")
        login = client.post(
            "/api/v1/auth/login",
            headers={"X-Correlation-ID": "csrf-login"},
            json={"login": "csrfsubject", "password": "user-pass"},
        )
        assert login.status_code == 200

        blocked = client.post("/api/v1/auth/logout", headers={"X-Correlation-ID": "csrf-blocked"})
        assert blocked.status_code == 403

        audit = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "csrf_blocked", "correlation_id": "csrf-blocked"},
        )
        assert audit.status_code == 200
        payload = audit.json()
        assert len(payload) == 1
        assert payload[0]["reason"] == "untrusted_csrf_source"
        assert payload[0]["route"] == "/api/v1/auth/logout"


def test_json_log_sink_redacts_secret_fields(caplog) -> None:
    service = AuditService([JsonLogAuditSink(logging.getLogger("imghost.audit.test"))])

    with caplog.at_level(logging.INFO, logger="imghost.audit.test"):
        asyncio.run(
            service.emit_action(
                event_type="secret_test",
                action="secret.test",
                result="success",
                actor=anonymous_actor(),
                object=AuditObject(type="test", id="secret"),
                metadata={
                    "password": "super-secret",
                    "api_key": "raw-key",
                    "nested": {"current_password": "still-secret"},
                },
            )
        )

    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert "[REDACTED]" in combined
    assert "super-secret" not in combined
    assert "raw-key" not in combined


def test_audit_service_continues_when_one_sink_fails(caplog) -> None:
    recording_sink = _RecordingSink()
    service = AuditService([_FailingSink(), recording_sink])

    with caplog.at_level(logging.ERROR):
        asyncio.run(
            service.emit_action(
                event_type="sink_test",
                action="sink.test",
                result="success",
                actor=anonymous_actor(),
                object=AuditObject(type="test", id="sink"),
                metadata={"source": "system"},
            )
        )

    assert len(recording_sink.records) == 1
    assert recording_sink.records[0].event_type == "sink_test"
    assert any(record.message == "audit_sink_write_failed" for record in caplog.records)


def test_audit_service_query_requires_query_backend() -> None:
    service = AuditService([])

    async def run() -> None:
        await service.query_audit_log()

    with pytest.raises(RuntimeError, match="Audit query backend is not configured."):
        asyncio.run(run())


def test_audit_service_still_returns_query_results_when_non_query_sink_fails(caplog) -> None:
    backend = _QueryBackend(rows=["ok-row"])
    service = AuditService([_FailingSink(), _RecordingSink()], query_backend=backend)

    with caplog.at_level(logging.ERROR):
        asyncio.run(
            service.emit_action(
                event_type="queryable_sink_test",
                action="sink.queryable",
                result="success",
                actor=anonymous_actor(),
                object=AuditObject(type="test", id="queryable"),
            )
        )
        rows = asyncio.run(service.query_audit_log(limit=5))

    assert rows == ["ok-row"]
    assert backend.calls == [
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
    assert any(record.message == "audit_sink_write_failed" for record in caplog.records)


def test_audit_service_swallows_all_sink_failures(caplog) -> None:
    service = AuditService([_FailingSink(), _FailingSink()])

    with caplog.at_level(logging.ERROR):
        asyncio.run(
            service.emit_action(
                event_type="all_sinks_fail",
                action="sink.all_fail",
                result="error",
                actor=anonymous_actor(),
                object=AuditObject(type="test", id="all-fail"),
            )
        )

    assert len([record for record in caplog.records if record.message == "audit_sink_write_failed"]) == 2


def test_audit_query_reads_legacy_metadata_only_rows(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="legacyauditadmin", email="legacyauditadmin@example.com")

    async def insert_legacy_row() -> None:
        conn = await asyncpg.connect("postgresql://imghost:imghost@localhost:5432/imghost_test")
        try:
            await conn.execute(
                """
                INSERT INTO audit_log (
                  id, event_type, actor_id, actor_ip_hash, target_type, target_id, correlation_id, metadata, created_at
                ) VALUES (
                  '12345678-1234-5678-1234-567812345678'::uuid,
                  'legacy_event',
                  NULL,
                  NULL,
                  'legacy',
                  'row-1',
                  'legacy-flow',
                  '{"action":"legacy.action","result":"denied","source":"worker","actor_type":"system","request_id":"legacy-request","reason":"legacy-reason","request":{"route":"/legacy/path","method":"PATCH"}}'::jsonb,
                  now()
                )
                """
            )
        finally:
            await conn.close()

    asyncio.run(insert_legacy_row())

    with TestClient(app) as client:
        audit = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "legacy_event", "correlation_id": "legacy-flow"},
        )
        assert audit.status_code == 200
        payload = audit.json()
        assert len(payload) == 1
        assert payload[0]["action"] == "legacy.action"
        assert payload[0]["result"] == "denied"
        assert payload[0]["source"] == "worker"
        assert payload[0]["actor_type"] == "system"
        assert payload[0]["request_id"] == "legacy-request"
        assert payload[0]["route"] == "/legacy/path"
        assert payload[0]["method"] == "PATCH"
        assert payload[0]["reason"] == "legacy-reason"


def test_app_lifespan_writes_system_startup_and_shutdown_audit(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("TASK_WORKER_ENABLED", "false")

    _, admin_key = create_admin_and_api_key(capsys, username="startupauditadmin", email="startupauditadmin@example.com")

    with TestClient(app) as client:
        startup = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "system_startup"},
        )
        assert startup.status_code == 200
        payload = startup.json()
        assert len(payload) == 1
        assert payload[0]["action"] == "system.startup"
        assert payload[0]["source"] == "system"
        assert payload[0]["metadata"]["run_task_worker"] is False
        assert payload[0]["metadata"]["task_queue_mode"] is not None

    async def fetch_shutdown_events() -> list[asyncpg.Record]:
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            return await conn.fetch("SELECT event_type, action, source FROM audit_log WHERE event_type = 'system_shutdown'")
        finally:
            await conn.close()

    shutdown_rows = asyncio.run(fetch_shutdown_events())
    assert len(shutdown_rows) == 1
    assert shutdown_rows[0]["action"] == "system.shutdown"
    assert shutdown_rows[0]["source"] == "system"


def test_startup_promotes_configured_existing_user_to_admin_and_audits_it(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    user_id, user_key = create_user_and_api_key(capsys, username="bootstrapuser", email="bootstrapuser@example.com")
    monkeypatch.setenv("PROMOTE_USERNAME_TO_ADMIN", "bootstrapuser")

    with TestClient(app) as client:
        me = client.get("/api/v1/user/me", headers={"Authorization": f"Bearer {user_key}"})
        assert me.status_code == 200
        assert me.json()["is_admin"] is True

        audit = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {user_key}"},
            params={"event_type": "system_bootstrap_admin_promoted"},
        )
        assert audit.status_code == 200
        payload = audit.json()
        assert len(payload) == 1
        assert payload[0]["action"] == "system.bootstrap_admin.promote"
        assert payload[0]["target_type"] == "user"
        assert payload[0]["target_id"] == user_id
        assert payload[0]["metadata"]["username"] == "bootstrapuser"


def test_bootstrap_admin_promotion_is_only_audited_once_per_startup(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    user_id, user_key = create_user_and_api_key(capsys, username="bootstraponce", email="bootstraponce@example.com")
    monkeypatch.setenv("PROMOTE_USERNAME_TO_ADMIN", "bootstraponce")

    with TestClient(app) as client:
        first_status = client.get("/api/v1/admin/runtime-status", headers={"Authorization": f"Bearer {user_key}"})
        second_status = client.get("/api/v1/admin/runtime-status", headers={"Authorization": f"Bearer {user_key}"})
        assert first_status.status_code == 200
        assert second_status.status_code == 200
        assert first_status.json()["bootstrap_admin"]["promoted"] is True
        assert second_status.json()["bootstrap_admin"]["promoted"] is True

        audit = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {user_key}"},
            params={"event_type": "system_bootstrap_admin_promoted"},
        )
        assert audit.status_code == 200
        payload = audit.json()
        assert len(payload) == 1
        assert payload[0]["target_id"] == user_id


def test_startup_does_not_audit_bootstrap_admin_when_user_already_admin(tmp_path, monkeypatch, capsys, caplog) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="bootstrapalreadyadmin", email="bootstrapalreadyadmin@example.com")
    monkeypatch.setenv("PROMOTE_USERNAME_TO_ADMIN", "bootstrapalreadyadmin")

    with caplog.at_level(logging.INFO):
        with TestClient(app) as client:
            runtime = client.get(
                "/api/v1/admin/runtime-status",
                headers={"Authorization": f"Bearer {admin_key}"},
            )
            assert runtime.status_code == 200
            assert runtime.json()["bootstrap_admin"]["enabled"] is True
            assert runtime.json()["bootstrap_admin"]["configured_username"] == "bootstrapalreadyadmin"
            assert runtime.json()["bootstrap_admin"]["matched"] is True
            assert runtime.json()["bootstrap_admin"]["already_admin"] is True
            assert runtime.json()["bootstrap_admin"]["promoted"] is False
            assert runtime.json()["bootstrap_admin"]["user_id"] is not None
            assert runtime.json()["bootstrap_admin"]["warning"] is None

            audit = client.get(
                "/api/v1/admin/audit",
                headers={"Authorization": f"Bearer {admin_key}"},
                params={"event_type": "system_bootstrap_admin_promoted"},
            )
            assert audit.status_code == 200
            assert audit.json() == []

    assert any(record.message == "bootstrap_admin_user_already_admin" for record in caplog.records)


def test_startup_warns_when_bootstrap_admin_username_is_missing(tmp_path, monkeypatch, capsys, caplog) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("PROMOTE_USERNAME_TO_ADMIN", "not-a-real-user")

    _, admin_key = create_admin_and_api_key(capsys, username="bootstrapwarnadmin", email="bootstrapwarnadmin@example.com")

    with caplog.at_level(logging.WARNING):
        with TestClient(app) as client:
            runtime = client.get(
                "/api/v1/admin/runtime-status",
                headers={"Authorization": f"Bearer {admin_key}"},
            )
            assert runtime.status_code == 200
            assert runtime.json()["bootstrap_admin"]["enabled"] is True
            assert runtime.json()["bootstrap_admin"]["configured_username"] == "not-a-real-user"
            assert runtime.json()["bootstrap_admin"]["matched"] is False
            assert runtime.json()["bootstrap_admin"]["already_admin"] is False
            assert runtime.json()["bootstrap_admin"]["promoted"] is False
            assert runtime.json()["bootstrap_admin"]["user_id"] is None
            assert runtime.json()["bootstrap_admin"]["warning"] == (
                "PROMOTE_USERNAME_TO_ADMIN set to 'not-a-real-user', but no matching user exists."
            )

            audit = client.get(
                "/api/v1/admin/audit",
                headers={"Authorization": f"Bearer {admin_key}"},
                params={"event_type": "system_bootstrap_admin_promoted"},
            )
            assert audit.status_code == 200
            assert audit.json() == []

    assert any(record.message == "bootstrap_admin_user_missing" for record in caplog.records)


def test_app_state_worker_lifecycle_is_audited_and_suppressed_when_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.delenv("REDIS_URL", raising=False)

    async def run_state(run_worker: bool) -> tuple[int, int]:
        settings = load_settings()
        state = AppState(settings, run_task_worker=run_worker)
        await state.start()
        await state.stop()
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            worker_started = await conn.fetchval("SELECT count(*) FROM audit_log WHERE event_type = 'worker_started'")
            worker_stopped = await conn.fetchval("SELECT count(*) FROM audit_log WHERE event_type = 'worker_stopped'")
            return int(worker_started), int(worker_stopped)
        finally:
            await conn.close()

    started, stopped = asyncio.run(run_state(True))
    assert started == 1
    assert stopped == 1

    async def truncate_audit() -> None:
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            await conn.execute("TRUNCATE TABLE audit_log")
        finally:
            await conn.close()

    asyncio.run(truncate_audit())

    started_disabled, stopped_disabled = asyncio.run(run_state(False))
    assert started_disabled == 0
    assert stopped_disabled == 0


def test_non_admin_admin_api_denial_is_audited(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="denialwatchadmin", email="denialwatchadmin@example.com")
    user_id, user_key = create_user_and_api_key(capsys, username="nonadminreader", email="nonadminreader@example.com")

    with TestClient(app) as client:
        denied = client.get(
            "/api/v1/admin/users",
            headers={"Authorization": f"Bearer {user_key}", "X-Correlation-ID": "admin-denied-flow"},
        )
        assert denied.status_code == 403

        audit = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "admin_access_denied", "correlation_id": "admin-denied-flow"},
        )
        assert audit.status_code == 200
        payload = audit.json()
        assert len(payload) == 1
        assert payload[0]["actor_id"] == user_id
        assert payload[0]["reason"] == "admin_required"


def test_registration_disabled_denial_is_audited(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="regwatchadmin", email="regwatchadmin@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        disabled = client.patch(
            "/api/v1/admin/config",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"allow_registration": False},
        )
        assert disabled.status_code == 200

        denied = client.post(
            "/api/v1/auth/register",
            headers={"X-Correlation-ID": "register-denied"},
            json={"username": "nope", "email": "nope@example.com", "password": "secret-pass"},
        )
        assert denied.status_code == 403

        audit = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "registration_denied", "correlation_id": "register-denied"},
        )
        assert audit.status_code == 200
        payload = audit.json()
        assert len(payload) == 1
        assert payload[0]["reason"] == "registration_disabled"
        assert payload[0]["metadata"]["email"] == "nope@example.com"
