from types import SimpleNamespace

from fastapi.testclient import TestClient

from imghost.main import AppState, app


def _runtime_status_payload(
    *,
    database_ok: bool = True,
    storage_ok: bool = True,
    redis_configured: bool = True,
    redis_reachable: bool = True,
    task_mode: str = "async",
) -> dict[str, object]:
    return {
        "process_role": "app",
        "database": {"ok": database_ok},
        "storage": {"ok": storage_ok},
        "redis": {
            "configured": redis_configured,
            "reachable": redis_reachable,
            "session_fail_closed": False,
            "subsystems": {
                "sessions": {"configured": redis_configured, "mode": "redis" if redis_configured else "disabled"},
                "rate_limits": {"configured": redis_configured, "mode": "redis" if redis_configured else "disabled"},
                "tasks": {"configured": redis_configured, "mode": "redis" if redis_configured else "fallback"},
            },
        },
        "services": {
            "app": {"enabled_in_this_process": True},
            "worker": {
                "enabled_in_this_process": False,
                "queues": [],
                "last_started_at": None,
                "last_stopped_at": None,
                "last_task_failure_at": None,
                "last_task_failure": None,
            },
            "scheduler": {"enabled_in_this_process": False},
        },
        "tasks": {"mode": task_mode, "queues": {"default": 0, "thumbnails": 0}, "worker_queues": []},
        "public_origin_enabled": True,
        "public_origin_mode": "strict",
        "trusted_public_origins": [],
        "forwarded_headers_policy": "permissive",
        "trusted_proxy_cidrs_enabled": False,
        "trusted_proxy_cidrs": [],
    }


def _readiness_payload(
    *,
    ok: bool = True,
    degraded: bool = False,
    database_ok: bool = True,
    storage_ok: bool = True,
    redis_configured: bool = True,
    redis_reachable: bool = True,
    task_mode: str = "async",
) -> dict[str, object]:
    return {
        "ok": ok,
        "degraded": degraded,
        "database": {"ok": database_ok},
        "storage": {"ok": storage_ok},
        "redis": {
            "configured": redis_configured,
            "reachable": redis_reachable,
        },
        "tasks": {"mode": task_mode},
    }


def test_health_live_returns_plain_ok(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        assert response.text == "ok"


def test_health_live_does_not_depend_on_runtime_status(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    async def exploding_runtime_status(self: AppState) -> dict[str, object]:
        raise AssertionError("runtime_status should not be called by /health/live")

    monkeypatch.setattr(AppState, "runtime_status", exploding_runtime_status)

    with TestClient(app) as client:
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.text == "ok"


def test_health_ready_returns_ok_when_core_dependencies_are_healthy(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    payload = _readiness_payload()

    async def fake_readiness_status(self: AppState) -> dict[str, object]:
        return payload.copy()

    monkeypatch.setattr(AppState, "readiness_status", fake_readiness_status)

    with TestClient(app) as client:
        response = client.get("/health/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["degraded"] is False
        assert body["database"]["ok"] is True
        assert body["storage"]["ok"] is True
        assert body["redis"]["configured"] is True
        assert body["redis"]["reachable"] is True
        assert body["tasks"]["mode"] == "async"
        assert "worker" not in body
        assert "trusted_public_origins" not in body
        assert "trusted_proxy_cidrs" not in body
        assert "proxy_trust_warning" not in body
        assert "subsystems" not in body["redis"]


def test_health_ready_returns_503_when_database_is_down(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    async def fake_readiness_status(self: AppState) -> dict[str, object]:
        return _readiness_payload(ok=False, degraded=True, database_ok=False)

    monkeypatch.setattr(AppState, "readiness_status", fake_readiness_status)

    with TestClient(app) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["ok"] is False
        assert body["degraded"] is True
        assert body["database"]["ok"] is False
        assert "storage" in body
        assert "redis" in body


def test_health_ready_returns_503_when_storage_is_down(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    async def fake_readiness_status(self: AppState) -> dict[str, object]:
        return _readiness_payload(ok=False, degraded=True, storage_ok=False)

    monkeypatch.setattr(AppState, "readiness_status", fake_readiness_status)

    with TestClient(app) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["ok"] is False
        assert body["degraded"] is True
        assert body["storage"]["ok"] is False


def test_health_ready_allows_optional_redis_outage(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("REDIS_MODE", "auto")

    async def fake_readiness_status(self: AppState) -> dict[str, object]:
        return _readiness_payload(
            ok=True,
            degraded=False,
            redis_configured=True,
            redis_reachable=False,
            task_mode="async",
        )

    monkeypatch.setattr(AppState, "readiness_status", fake_readiness_status)

    with TestClient(app) as client:
        response = client.get("/health/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["degraded"] is False
        assert body["redis"]["configured"] is True
        assert body["redis"]["reachable"] is False


def test_health_ready_fails_when_required_redis_is_down(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("REDIS_MODE", "required")

    async def fake_readiness_status(self: AppState) -> dict[str, object]:
        return _readiness_payload(
            ok=False,
            degraded=True,
            redis_configured=True,
            redis_reachable=False,
            task_mode="redis",
        )

    monkeypatch.setattr(AppState, "readiness_status", fake_readiness_status)

    with TestClient(app) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["ok"] is False
        assert body["degraded"] is True
        assert body["redis"]["configured"] is True
        assert body["redis"]["reachable"] is False


def test_health_ready_exposes_dependency_snapshot_when_not_ready(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("REDIS_MODE", "required")

    payload = _readiness_payload(
        ok=False,
        degraded=True,
        database_ok=False,
        storage_ok=True,
        redis_configured=True,
        redis_reachable=False,
    )

    async def fake_readiness_status(self: AppState) -> dict[str, object]:
        return payload.copy()

    monkeypatch.setattr(AppState, "readiness_status", fake_readiness_status)

    with TestClient(app) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["ok"] is False
        assert body["database"] == {"ok": False}
        assert body["storage"] == {"ok": True}
        assert body["redis"]["configured"] is True
        assert body["redis"]["reachable"] is False
        assert body["tasks"] == {"mode": "async"}
        assert "worker" not in body
        assert "trusted_public_origins" not in body


def test_health_ready_does_not_expose_admin_only_runtime_details(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    payload = _readiness_payload()

    async def fake_readiness_status(self: AppState) -> dict[str, object]:
        return payload.copy()

    monkeypatch.setattr(AppState, "readiness_status", fake_readiness_status)

    with TestClient(app) as client:
        response = client.get("/health/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert "session_fail_closed" not in body["redis"]
        assert "subsystems" not in body["redis"]
        assert "worker" not in body
        assert "trusted_proxy_cidrs" not in body
