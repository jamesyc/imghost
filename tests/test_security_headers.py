from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient

from imghost.main import app
from imghost.web.security_headers import CONTENT_SECURITY_POLICY

from .helpers import PNG_1X1


def _assert_baseline_headers(headers) -> None:
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert headers["x-frame-options"] == "DENY"
    assert headers["content-security-policy"] == CONTENT_SECURITY_POLICY


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


def test_redirect_response_includes_baseline_security_headers_without_hsts(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.get("/dashboard", follow_redirects=False)
        assert response.status_code == 303
        _assert_baseline_headers(response.headers)
        assert "strict-transport-security" not in response.headers


def test_not_found_response_includes_baseline_security_headers_without_hsts(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.get("/definitely-missing")
        assert response.status_code == 404
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


def test_https_json_api_gets_hsts_header(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/health/ready")
        assert response.status_code == 200
        _assert_baseline_headers(response.headers)
        assert response.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"


def test_https_redirect_response_gets_hsts_header(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/dashboard", follow_redirects=False)
        assert response.status_code == 303
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


def test_trusted_forwarded_https_media_request_gets_hsts_header(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://fallback.example.com")
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS_ENABLED", "true")
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "127.0.0.1/32")

    with TestClient(app, base_url="http://backend", client=("127.0.0.1", 50000)) as client:
        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Trusted proxy media"},
        )
        assert upload.status_code == 200
        media_id = upload.json()["media_id"]

        response = client.get(f"/i/{media_id}.png", headers={"X-Forwarded-Proto": "https"})
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


def test_untrusted_forwarded_https_media_request_does_not_get_hsts_header(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://fallback.example.com")
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS_ENABLED", "true")
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "127.0.0.1/32")

    with TestClient(app, base_url="http://backend") as client:
        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Untrusted proxy media"},
        )
        assert upload.status_code == 200
        media_id = upload.json()["media_id"]

        response = client.get(f"/i/{media_id}.png", headers={"X-Forwarded-Proto": "https"})
        assert response.status_code == 200
        _assert_baseline_headers(response.headers)
        assert "strict-transport-security" not in response.headers


def test_home_page_uses_external_theme_init_script(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert '<script src="/static/js/theme-init.js"></script>' in response.text
        assert "const storedTheme = window.localStorage.getItem" not in response.text


def test_base_template_removes_unused_inline_escape_hatches() -> None:
    template = Path("src/imghost/templates/base.html").read_text(encoding="utf-8")
    assert "body_html" not in template
    assert "script_html" not in template


def test_csp_explicitly_blocks_inline_script_and_style_attributes() -> None:
    assert "script-src-attr 'none'" in CONTENT_SECURITY_POLICY
    assert "style-src-attr 'none'" in CONTENT_SECURITY_POLICY
    assert "manifest-src 'self'" in CONTENT_SECURITY_POLICY
    assert "worker-src 'self'" in CONTENT_SECURITY_POLICY


def test_public_album_bootstrap_json_page_includes_csp_without_hsts(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Bootstrap Album"},
        )
        assert upload.status_code == 200
        album_id = upload.json()["album_id"]

        response = client.get(f"/a/{album_id}")
        assert response.status_code == 200
        _assert_baseline_headers(response.headers)
        assert "strict-transport-security" not in response.headers
        assert '<script id="public-album-bootstrap" type="application/json">' in response.text


def test_https_settings_bootstrap_json_page_includes_csp_and_hsts(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")

    with TestClient(app, base_url="https://testserver") as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "cspsettingsuser",
                "email": "cspsettings@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200

        response = client.get("/settings")
        assert response.status_code == 200
        _assert_baseline_headers(response.headers)
        assert response.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
        assert '<script id="settings-bootstrap" type="application/json">' in response.text


def test_public_album_page_has_no_inline_select_handler_markup(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "CSP Album"},
        )
        assert upload.status_code == 200
        album_id = upload.json()["album_id"]

        response = client.get(f"/a/{album_id}")
        assert response.status_code == 200
        assert 'onclick="this.select()"' not in response.text


def test_static_js_sources_do_not_emit_inline_select_handlers() -> None:
    for path in [
        "src/imghost/static/js/album-detail.js",
        "src/imghost/static/js/album-cards.js",
        "src/imghost/static/js/theme.js",
    ]:
        assert 'onclick="this.select()"' not in Path(path).read_text(encoding="utf-8")
