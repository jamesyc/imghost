from io import BytesIO

from fastapi.testclient import TestClient
import pytest

from imghost.config import load_settings
from imghost.main import app
from imghost.web.security_headers import CONTENT_SECURITY_POLICY

from .helpers import PNG_1X1, create_admin_and_api_key

COLLECTOR = "https://stats.example.com"
WEBSITE_ID = "d7be0b95-4982-4e95-940e-65ba634ee073"


def configure_analytics(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("UMAMI_WEBSITE_ID", WEBSITE_ID)
    monkeypatch.setenv("UMAMI_HOST_URL", COLLECTOR)
    monkeypatch.setenv("UMAMI_DOMAINS", "testserver,images.example.com")


def assert_tracked(response) -> None:
    assert response.status_code == 200
    assert response.text.count(f'{COLLECTOR}/init.js') == 1
    assert f'{COLLECTOR}/script.js' not in response.text
    assert f'data-website-id="{WEBSITE_ID}"' in response.text
    assert 'data-domains="testserver,images.example.com"' in response.text
    assert 'data-before-send="umamiBeforeSend"' in response.text
    assert 'data-exclude-search="true" data-exclude-hash="true"' in response.text
    policy = response.headers["content-security-policy"]
    assert f"script-src 'self' {COLLECTOR};" in policy
    assert f"connect-src 'self' {COLLECTOR};" in policy
    assert COLLECTOR not in policy.replace(f"script-src 'self' {COLLECTOR};", "").replace(
        f"connect-src 'self' {COLLECTOR};", ""
    )
    assert "unsafe-inline" not in policy


def assert_untracked(response) -> None:
    assert response.status_code == 200
    assert COLLECTOR not in response.text
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY


def test_tracking_is_disabled_by_default(tmp_path, monkeypatch) -> None:
    configure_analytics(monkeypatch, tmp_path)
    monkeypatch.delenv("UMAMI_WEBSITE_ID")
    monkeypatch.delenv("UMAMI_HOST_URL")
    with TestClient(app) as client:
        assert_untracked(client.get("/"))


def test_public_pages_track_but_auth_and_capability_pages_do_not(tmp_path, monkeypatch) -> None:
    configure_analytics(monkeypatch, tmp_path)
    with TestClient(app, base_url="https://testserver") as client:
        assert_tracked(client.get("/"))
        for path in ("/login", "/register"):
            assert_untracked(client.get(path))
        upload = client.post(
            "/api/v1/upload", files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))]
        )
        assert upload.status_code == 200
        payload = upload.json()
        assert_tracked(client.get(f"/a/{payload['album_id']}"))
        assert_untracked(client.get(payload["manage_url"]))


def test_signed_in_public_visits_count_but_account_and_admin_pages_do_not(tmp_path, monkeypatch, capsys) -> None:
    configure_analytics(monkeypatch, tmp_path)
    user_id, _ = create_admin_and_api_key(capsys, username="analyst", email="analyst@example.com")
    with TestClient(app, base_url="https://testserver") as client:
        login = client.post(
            "/api/v1/auth/login", json={"login": "analyst@example.com", "password": "test-pass-123"}
        )
        assert login.status_code == 200
        assert_tracked(client.get("/"))
        assert_tracked(client.get("/u/analyst"))
        for path in ("/dashboard", "/albums", "/settings", "/admin", "/admin/config", f"/admin/users/{user_id}"):
            assert_untracked(client.get(path))


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("UMAMI_WEBSITE_ID", "not-a-uuid"),
        ("UMAMI_HOST_URL", ""),
        ("UMAMI_HOST_URL", "http://stats.example.com"),
        ("UMAMI_HOST_URL", "https://stats.example.com/path"),
        ("UMAMI_HOST_URL", "https://user:pass@stats.example.com"),
        ("UMAMI_HOST_URL", "https://stats.example.com;script-src *"),
        ("UMAMI_HOST_URL", "https://stats.example.com?query=1"),
        ("UMAMI_DOMAINS", "https://images.example.com"),
    ],
)
def test_analytics_settings_reject_unsafe_configuration(tmp_path, monkeypatch, setting, value) -> None:
    configure_analytics(monkeypatch, tmp_path)
    monkeypatch.setenv(setting, value)
    with pytest.raises(ValueError, match="UMAMI"):
        load_settings()


def test_analytics_domains_default_to_canonical_hostname(tmp_path, monkeypatch) -> None:
    configure_analytics(monkeypatch, tmp_path)
    monkeypatch.delenv("UMAMI_DOMAINS")
    assert load_settings().umami_domains == ("testserver",)
