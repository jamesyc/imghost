from io import BytesIO

from fastapi.testclient import TestClient

from imghost.main import app

from .helpers import PNG_1X1


def _assert_baseline_headers(headers) -> None:
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert headers["x-frame-options"] == "DENY"
    assert headers["content-security-policy"] == "frame-ancestors 'none'"


def test_html_page_includes_baseline_security_headers(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        _assert_baseline_headers(response.headers)
        assert "strict-transport-security" not in response.headers


def test_json_api_includes_baseline_security_headers(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.get("/health/ready")
        assert response.status_code == 200
        _assert_baseline_headers(response.headers)
        assert "strict-transport-security" not in response.headers


def test_media_response_includes_baseline_security_headers(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Headers Album"},
        )
        assert upload.status_code == 200
        media_id = upload.json()["media_id"]

        response = client.get(f"/i/{media_id}.png")
        assert response.status_code == 200
        _assert_baseline_headers(response.headers)
        assert "strict-transport-security" not in response.headers


def test_https_request_gets_hsts_header(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/")
        assert response.status_code == 200
        _assert_baseline_headers(response.headers)
        assert response.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"


def test_trusted_forwarded_https_request_gets_hsts_header(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://fallback.example.com")
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS_ENABLED", "true")
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "127.0.0.1/32")

    with TestClient(app, base_url="http://backend", client=("127.0.0.1", 50000)) as client:
        response = client.get("/", headers={"X-Forwarded-Proto": "https"})
        assert response.status_code == 200
        _assert_baseline_headers(response.headers)
        assert response.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"


def test_untrusted_forwarded_https_request_does_not_get_hsts_header(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://fallback.example.com")
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS_ENABLED", "true")
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "127.0.0.1/32")

    with TestClient(app, base_url="http://backend") as client:
        response = client.get("/", headers={"X-Forwarded-Proto": "https"})
        assert response.status_code == 200
        _assert_baseline_headers(response.headers)
        assert "strict-transport-security" not in response.headers
