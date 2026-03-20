from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request

from imghost.config import Settings
from imghost.main import app
from imghost.public_origin import public_base_url, request_uses_trusted_proxy_headers

from .helpers import PNG_1X1, create_user_and_api_key


def _settings(*, enabled: bool, cidrs: tuple[str, ...] = ()) -> Settings:
    return Settings(
        base_url="https://fallback.example.com",
        trusted_public_origins=("https://trusted.example",),
        trusted_proxy_cidrs_enabled=enabled,
        trusted_proxy_cidrs=cidrs,
        database_url="postgresql://imghost:imghost@localhost:5432/imghost_test",
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
        secret_key="test-secret",
        session_cookie_name="imghost_session",
        session_cookie_secure=True,
        session_remember_days=30,
        max_upload_bytes=1024,
        anon_expiry_hours=24,
        max_pixel_megapixels=50,
        default_user_quota_bytes=1024,
        server_quota_bytes=0,
        video_thumb_frames=10,
        task_queue_mode="async",
        task_worker_enabled=True,
        thumbnail_worker_count=1,
    )


def _request(
    *,
    host: str,
    scheme: str = "http",
    headers: list[tuple[bytes, bytes]] | None = None,
    client_host: str = "127.0.0.1",
) -> Request:
    app = Starlette()
    scope = {
        "app": app,
        "type": "http",
        "method": "GET",
        "path": "/probe",
        "raw_path": b"/probe",
        "scheme": scheme,
        "query_string": b"",
        "headers": headers or [],
        "client": (client_host, 12345),
        "server": (host, 80),
    }
    return Request(scope)


def test_trusted_proxy_headers_are_permissive_when_gate_disabled() -> None:
    request = _request(
        host="backend",
        headers=[
            (b"host", b"backend"),
            (b"x-forwarded-proto", b"https"),
            (b"x-forwarded-host", b"trusted.example"),
        ],
        client_host="203.0.113.10",
    )

    assert request_uses_trusted_proxy_headers(request, _settings(enabled=False)) is True
    assert public_base_url(request, _settings(enabled=False)) == "https://trusted.example"


def test_trusted_proxy_headers_require_matching_peer_when_gate_enabled() -> None:
    request = _request(
        host="backend",
        headers=[
            (b"host", b"backend"),
            (b"x-forwarded-proto", b"https"),
            (b"x-forwarded-host", b"trusted.example"),
        ],
        client_host="203.0.113.10",
    )

    settings = _settings(enabled=True, cidrs=("127.0.0.1/32", "172.16.0.0/12"))
    assert request_uses_trusted_proxy_headers(request, settings) is False
    assert public_base_url(request, settings) == "https://fallback.example.com"


def test_trusted_proxy_headers_are_used_when_peer_matches_configured_cidr() -> None:
    request = _request(
        host="backend",
        headers=[
            (b"host", b"backend"),
            (b"x-forwarded-proto", b"https"),
            (b"x-forwarded-host", b"trusted.example"),
        ],
        client_host="172.16.5.10",
    )

    settings = _settings(enabled=True, cidrs=("127.0.0.1/32", "172.16.0.0/12"))
    assert request_uses_trusted_proxy_headers(request, settings) is True
    assert public_base_url(request, settings) == "https://trusted.example"


def test_direct_request_origin_still_works_when_proxy_gate_enabled() -> None:
    request = _request(
        host="trusted.example",
        scheme="https",
        headers=[(b"host", b"trusted.example")],
        client_host="203.0.113.10",
    )

    settings = _settings(enabled=True, cidrs=("127.0.0.1/32",))
    assert public_base_url(request, settings) == "https://trusted.example"


def test_upload_uses_forwarded_public_origin_for_generated_urls(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://fallback.example.com")
    monkeypatch.setenv("TRUSTED_PUBLIC_ORIGINS", "https://imghost.b.example")

    with TestClient(app, base_url="http://backend") as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Forwarded Album"},
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "imghost.b.example",
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["album_url"].startswith("https://imghost.b.example/")
        assert payload["media_url"].startswith("https://imghost.b.example/")
        assert payload["thumb_url"].startswith("https://imghost.b.example/")
        assert payload["delete_url"].startswith("https://imghost.b.example/")


def test_upload_rejects_untrusted_forwarded_public_origin_and_falls_back_to_base_url(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://fallback.example.com")
    monkeypatch.setenv("TRUSTED_PUBLIC_ORIGINS", "https://trusted.example")

    with TestClient(app, base_url="http://backend") as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Fallback Album"},
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "evil.example",
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["album_url"].startswith("https://fallback.example.com/")
        assert payload["media_url"].startswith("https://fallback.example.com/")
        assert payload["thumb_url"].startswith("https://fallback.example.com/")
        assert payload["delete_url"].startswith("https://fallback.example.com/")


def test_upload_rejects_malformed_forwarded_public_origin_and_falls_back_to_base_url(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://fallback.example.com")
    monkeypatch.setenv("TRUSTED_PUBLIC_ORIGINS", "https://trusted.example")

    with TestClient(app, base_url="http://backend") as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Malformed Album"},
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "bad/path.example/evil",
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["album_url"].startswith("https://fallback.example.com/")


def test_upload_uses_direct_request_origin_when_trusted(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://fallback.example.com")
    monkeypatch.setenv("TRUSTED_PUBLIC_ORIGINS", "https://imghost.002015.xyz")

    with TestClient(app, base_url="https://imghost.002015.xyz") as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Direct Trusted Album"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["album_url"].startswith("https://imghost.002015.xyz/")


def test_upload_falls_back_to_base_url_when_direct_request_origin_is_untrusted(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://fallback.example.com")
    monkeypatch.setenv("TRUSTED_PUBLIC_ORIGINS", "https://trusted.example")

    with TestClient(app, base_url="https://imghost.002015.xyz") as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Direct Untrusted Album"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["album_url"].startswith("https://fallback.example.com/")


def test_sharex_config_uses_forwarded_public_origin(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://fallback.example.com")
    monkeypatch.setenv("TRUSTED_PUBLIC_ORIGINS", "https://imghost.a.example,https://imghost.b.example")

    _, api_key = create_user_and_api_key(capsys, username="sharexforward", email="sharexforward@example.com")

    with TestClient(app, base_url="http://backend") as client:
        response = client.get(
            "/api/v1/user/me/sharex-config",
            headers={
                "Authorization": f"Bearer {api_key}",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "imghost.a.example",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["RequestURL"] == "https://imghost.a.example/api/v1/upload"


def test_sharex_config_rejects_untrusted_forwarded_public_origin(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://fallback.example.com")
    monkeypatch.setenv("TRUSTED_PUBLIC_ORIGINS", "https://imghost.a.example")

    _, api_key = create_user_and_api_key(capsys, username="sharexfallback", email="sharexfallback@example.com")

    with TestClient(app, base_url="http://backend") as client:
        response = client.get(
            "/api/v1/user/me/sharex-config",
            headers={
                "Authorization": f"Bearer {api_key}",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "evil.example",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["RequestURL"] == "https://fallback.example.com/api/v1/upload"
