from __future__ import annotations

import pytest

from imghost.config import load_settings
from imghost.storage import LocalFilesystemBackend, S3StorageBackend, build_storage_backend


def test_build_storage_backend_defaults_to_filesystem(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)

    backend = build_storage_backend(load_settings())

    assert isinstance(backend, LocalFilesystemBackend)


def test_build_storage_backend_supports_garage_alias(monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "garage")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://garage:3900")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "imghost")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("S3_BUCKET", "imghost")
    monkeypatch.setenv("S3_REGION", "garage")

    backend = build_storage_backend(load_settings())

    assert isinstance(backend, S3StorageBackend)


def test_build_storage_backend_requires_s3_settings(monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "garage")
    monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("S3_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("S3_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)

    with pytest.raises(RuntimeError, match="S3_ENDPOINT_URL"):
        build_storage_backend(load_settings())
