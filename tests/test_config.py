import pytest
from fastapi.testclient import TestClient

from imghost.config import _resolve_redis_url, load_settings
from imghost.main import app


def test_load_settings_requires_cidrs_when_trusted_proxy_gate_enabled(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://fallback.example.com")
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS_ENABLED", "true")
    monkeypatch.delenv("TRUSTED_PROXY_CIDRS", raising=False)

    with pytest.raises(ValueError, match="TRUSTED_PROXY_CIDRS"):
        load_settings()


def test_app_startup_fails_when_trusted_proxy_gate_enabled_without_cidrs(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://fallback.example.com")
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
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://fallback.example.com")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")
    monkeypatch.setenv("REDIS_PASSWORD", "s3cret!")

    settings = load_settings()
    assert settings.redis_password == "s3cret!"
    assert settings.redis_url == "redis://:s3cret%21@redis:6379/0"
