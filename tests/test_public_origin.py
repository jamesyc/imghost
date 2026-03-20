from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request

from imghost.config import Settings
from imghost.public_origin import public_base_url, request_uses_trusted_proxy_headers


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
