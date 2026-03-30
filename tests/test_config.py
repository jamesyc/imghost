import pytest
from fastapi.testclient import TestClient

from imghost.config import _resolve_redis_url, load_settings
from imghost.main import app


def _set_required_base_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://fallback.example.com")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://imghost:imghost@localhost:5432/imghost_test")


def test_load_settings_requires_secret_key(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://fallback.example.com")
    monkeypatch.setenv("DATABASE_URL", "postgresql://imghost:imghost@localhost:5432/imghost_test")
    monkeypatch.delenv("SECRET_KEY", raising=False)

    with pytest.raises(ValueError, match="SECRET_KEY"):
        load_settings()


def test_load_settings_requires_database_url(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://fallback.example.com")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValueError, match="DATABASE_URL"):
        load_settings()


def test_load_settings_requires_cidrs_when_trusted_proxy_gate_enabled(monkeypatch, tmp_path) -> None:
    _set_required_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS_ENABLED", "true")
    monkeypatch.delenv("TRUSTED_PROXY_CIDRS", raising=False)

    with pytest.raises(ValueError, match="TRUSTED_PROXY_CIDRS"):
        load_settings()


def test_app_startup_fails_when_trusted_proxy_gate_enabled_without_cidrs(monkeypatch, tmp_path) -> None:
    _set_required_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS_ENABLED", "true")
    monkeypatch.delenv("TRUSTED_PROXY_CIDRS", raising=False)

    with pytest.raises(ValueError, match="TRUSTED_PROXY_CIDRS"):
        with TestClient(app):
            pass


def test_resolve_redis_url_injects_password_when_url_has_no_credentials() -> None:
    assert _resolve_redis_url("redis://redis:6379/0", "s3cret!") == "redis://:s3cret%21@redis:6379/0"


def test_resolve_redis_url_preserves_existing_credentials() -> None:
    assert _resolve_redis_url("redis://:already@redis:6379/0", "ignored") == "redis://:already@redis:6379/0"


def test_load_settings_uses_redis_password_when_present(monkeypatch, tmp_path) -> None:
    _set_required_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("REDIS_PASSWORD", "s3cret!")

    settings = load_settings()
    assert settings.redis_password == "s3cret!"
    assert settings.redis_url == "redis://:s3cret%21@redis:6379/0"


def test_load_settings_defaults_database_use_pgbouncer_to_false(monkeypatch, tmp_path) -> None:
    _set_required_base_env(monkeypatch, tmp_path)
    monkeypatch.delenv("DATABASE_USE_PGBOUNCER", raising=False)

    settings = load_settings()
    assert settings.database_use_pgbouncer is False


def test_load_settings_parses_database_use_pgbouncer(monkeypatch, tmp_path) -> None:
    _set_required_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("DATABASE_USE_PGBOUNCER", "true")

    settings = load_settings()
    assert settings.database_use_pgbouncer is True


def test_load_settings_defaults_session_redis_fail_closed_to_false(monkeypatch, tmp_path) -> None:
    _set_required_base_env(monkeypatch, tmp_path)
    monkeypatch.delenv("SESSION_REDIS_FAIL_CLOSED", raising=False)

    settings = load_settings()
    assert settings.session_redis_fail_closed is False


def test_load_settings_parses_session_redis_fail_closed(monkeypatch, tmp_path) -> None:
    _set_required_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SESSION_REDIS_FAIL_CLOSED", "true")

    settings = load_settings()
    assert settings.session_redis_fail_closed is True


def test_load_settings_defaults_task_worker_queues(monkeypatch, tmp_path) -> None:
    _set_required_base_env(monkeypatch, tmp_path)
    monkeypatch.delenv("TASK_WORKER_QUEUES", raising=False)

    settings = load_settings()
    assert settings.task_worker_queues == ("default", "thumbnails")


def test_load_settings_defaults_video_thumb_frames_to_fifteen(monkeypatch, tmp_path) -> None:
    _set_required_base_env(monkeypatch, tmp_path)
    monkeypatch.delenv("VIDEO_THUMB_FRAMES", raising=False)

    settings = load_settings()
    assert settings.video_thumb_frames == 15


def test_load_settings_parses_task_worker_queues(monkeypatch, tmp_path) -> None:
    _set_required_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("TASK_WORKER_QUEUES", " thumbnails,cleanup,thumbnails ,,default ")

    settings = load_settings()
    assert settings.task_worker_queues == ("thumbnails", "cleanup", "default")


def test_load_settings_defaults_scheduler_settings(monkeypatch, tmp_path) -> None:
    _set_required_base_env(monkeypatch, tmp_path)
    monkeypatch.delenv("SCHEDULER_ENABLED", raising=False)
    monkeypatch.delenv("APP_SCHEDULER_ENABLED", raising=False)
    monkeypatch.delenv("SCHEDULER_POLL_SECONDS", raising=False)
    monkeypatch.delenv("SCHEDULER_LEASE_SECONDS", raising=False)
    monkeypatch.delenv("CLEANUP_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("AUDIT_RETENTION_DAYS", raising=False)

    settings = load_settings()
    assert settings.scheduler_enabled is False
    assert settings.app_scheduler_enabled is False
    assert settings.scheduler_poll_seconds == 30
    assert settings.scheduler_lease_seconds == 900
    assert settings.cleanup_interval_seconds == 900
    assert settings.audit_retention_days == 90


def test_load_settings_parses_scheduler_settings(monkeypatch, tmp_path) -> None:
    _set_required_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("APP_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("SCHEDULER_POLL_SECONDS", "15")
    monkeypatch.setenv("SCHEDULER_LEASE_SECONDS", "120")
    monkeypatch.setenv("CLEANUP_INTERVAL_SECONDS", "600")
    monkeypatch.setenv("AUDIT_RETENTION_DAYS", "45")

    settings = load_settings()
    assert settings.scheduler_enabled is True
    assert settings.app_scheduler_enabled is True
    assert settings.scheduler_poll_seconds == 15
    assert settings.scheduler_lease_seconds == 120
    assert settings.cleanup_interval_seconds == 600
    assert settings.audit_retention_days == 45


def test_runtime_config_allow_registration_defaults_from_env(monkeypatch, tmp_path) -> None:
    _set_required_base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("ALLOW_REGISTRATION", "false")

    with TestClient(app) as client:
        value = client.portal.call(client.app.state.imghost.runtime_config.get_value, "allow_registration")
        assert value is False
