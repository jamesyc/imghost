from datetime import timedelta
from io import BytesIO

from fastapi.testclient import TestClient
import pytest

from imghost.main import app
from imghost.models import utcnow

from .helpers import (
    PNG_1X1,
    create_admin_and_api_key,
    create_user_and_api_key,
    set_user_password,
    wait_for_thumbnail,
)


def test_static_base_css_is_served(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.get("/static/css/base.css")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/css")
        assert ":root {" in response.text
        assert ".site-nav {" in response.text
        assert ".auth-layout {" in response.text


def test_home_page_shows_upload_and_auth_entry_points(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert '<link rel="stylesheet" href="/static/css/base.css">' in response.text
        assert '<script src="/static/js/home.js" defer></script>' in response.text
        assert 'href="/login"' in response.text
        assert 'href="/register"' in response.text
        assert 'id="login-form"' not in response.text
        assert 'id="register-form"' not in response.text
        assert 'id="upload-form"' in response.text
        assert "Anonymous uploads currently expire after 24 hour(s)." in response.text


def test_home_page_reflects_runtime_config_disabled_states(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="indexadmin", email="indexadmin@example.com")

    with TestClient(app) as client:
        updated = client.patch(
            "/api/v1/admin/config",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"allow_registration": False, "anon_upload_enabled": False},
        )
        assert updated.status_code == 200

        response = client.get("/")
        assert response.status_code == 200
        assert "Registration is currently disabled." in response.text
        assert "Anonymous uploads are currently disabled. Sign in to upload." in response.text
        assert 'href="/register"' not in response.text
        assert 'id="upload-form"' not in response.text


def test_home_page_shows_session_state_when_logged_in(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")

    with TestClient(app, base_url="https://testserver") as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "browseruser",
                "email": "browser@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200

        page = client.get("/")
        assert page.status_code == 200
        assert "Signed in as <strong>browseruser</strong>." in page.text
        assert 'href="/dashboard"' in page.text
        assert 'id="logout-form"' in page.text
        assert "Authenticated uploads do not expire by default." in page.text
        assert 'id="upload-form"' in page.text


def test_login_page_renders_form_and_register_link(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.get("/login")
        assert response.status_code == 200
        assert 'id="login-form"' in response.text
        assert 'data-auth-form' in response.text
        assert 'href="/register"' in response.text
        assert '<script src="/static/js/auth.js" defer></script>' in response.text


def test_register_page_renders_form_when_enabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.get("/register")
        assert response.status_code == 200
        assert 'id="register-form"' in response.text
        assert 'data-auth-form' in response.text
        assert 'href="/login"' in response.text


def test_register_page_shows_disabled_state_when_registration_is_off(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="regadmin", email="regadmin@example.com")

    with TestClient(app) as client:
        updated = client.patch(
            "/api/v1/admin/config",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"allow_registration": False},
        )
        assert updated.status_code == 200

        response = client.get("/register")
        assert response.status_code == 200
        assert "Registration is currently disabled." in response.text
        assert 'id="register-form"' not in response.text
        assert 'href="/login"' in response.text


def test_login_and_register_redirect_authenticated_users_to_dashboard(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")

    with TestClient(app, base_url="https://testserver") as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "redirectuser",
                "email": "redirect@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200

        login_page = client.get("/login", follow_redirects=False)
        register_page = client.get("/register", follow_redirects=False)
        assert login_page.status_code == 303
        assert login_page.headers["location"] == "/dashboard"
        assert register_page.status_code == 303
        assert register_page.headers["location"] == "/dashboard"


@pytest.mark.parametrize(
    ("path", "expected_next"),
    [
        ("/dashboard", "/login?next=%2Fdashboard"),
        ("/settings", "/login?next=%2Fsettings"),
        ("/admin", "/login?next=%2Fadmin"),
    ],
)
def test_private_pages_redirect_logged_out_users_to_login(tmp_path, monkeypatch, path: str, expected_next: str) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == expected_next


def test_non_admin_user_gets_forbidden_on_admin_page(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")

    with TestClient(app, base_url="https://testserver") as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "plainuser",
                "email": "plain@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200

        response = client.get("/admin")
        assert response.status_code == 403
        assert response.json()["detail"] == "Admin access required."


def test_template_shell_wraps_phase_three_pages_and_private_pages(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, _ = create_user_and_api_key(capsys, username="dashuser", email="dash@example.com")
    admin_id, _ = create_admin_and_api_key(capsys, username="uiadmin", email="uiadmin@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        for public_path in ("/", "/login", "/register", "/album-tools"):
            response = client.get(public_path)
            assert response.status_code == 200
            assert '<link rel="stylesheet" href="/static/css/base.css">' in response.text
            assert '<nav class="site-nav" aria-label="Primary">' in response.text

        set_user_password(client, user_id, "open-sesame")
        login = client.post("/api/v1/auth/login", json={"login": "dash@example.com", "password": "open-sesame"})
        assert login.status_code == 200

        dashboard = client.get("/dashboard")
        settings = client.get("/settings")
        assert dashboard.status_code == 200
        assert settings.status_code == 200
        assert "User Dashboard" in dashboard.text
        assert "Settings" in settings.text
        assert 'href="/settings"' in dashboard.text
        assert 'href="/dashboard"' in settings.text
        assert 'href="/admin"' not in dashboard.text

        client.post("/api/v1/auth/logout")
        set_user_password(client, admin_id, "admin-pass")
        admin_login = client.post("/api/v1/auth/login", json={"login": "uiadmin@example.com", "password": "admin-pass"})
        assert admin_login.status_code == 200

        admin_page = client.get("/admin")
        assert admin_page.status_code == 200
        assert "Admin Dashboard" in admin_page.text
        assert 'href="/admin"' in admin_page.text


def test_dashboard_page_focuses_on_uploads_albums_and_links_to_settings(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, _ = create_user_and_api_key(capsys, username="dashuser2", email="dash2@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, user_id, "open-sesame")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "dash2@example.com", "password": "open-sesame"},
        )
        assert login.status_code == 200

        page = client.get("/dashboard")
        assert page.status_code == 200
        assert "User Dashboard" in page.text
        assert "API Key Mode" in page.text
        assert 'id="dashboard-upload-form"' in page.text
        assert 'id="owned-albums"' in page.text
        assert 'href="/settings"' in page.text
        assert 'id="change-password-form"' not in page.text


def test_settings_page_includes_account_api_key_password_and_delete_ui(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, _ = create_user_and_api_key(capsys, username="settingsuser", email="settings@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, user_id, "open-sesame")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "settings@example.com", "password": "open-sesame"},
        )
        assert login.status_code == 200

        page = client.get("/settings")
        assert page.status_code == 200
        assert "Settings" in page.text
        assert 'id="settings-account-summary"' in page.text
        assert 'id="reveal-api-key"' in page.text
        assert 'id="download-sharex-settings"' in page.text
        assert 'id="settings-password-form"' in page.text
        assert 'id="settings-delete-account-form"' in page.text


def test_admin_page_includes_admin_tools_ui(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    admin_id, _ = create_admin_and_api_key(capsys, username="pageadmin", email="pageadmin@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, admin_id, "admin-pass")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "pageadmin@example.com", "password": "admin-pass"},
        )
        assert login.status_code == 200

        page = client.get("/admin")
        assert page.status_code == 200
        assert "Admin Dashboard" in page.text
        assert "Create User" in page.text
        assert "Runtime Config" in page.text
        assert "Audit Log" in page.text
        assert 'id="admin-users"' in page.text
        assert 'id="admin-albums"' in page.text


def test_album_tools_page_includes_manual_album_controls(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        page = client.get("/album-tools")
        assert page.status_code == 200
        assert "Album Tools" in page.text
        assert "Load Album" in page.text
        assert 'name="album_id"' in page.text
        assert 'name="delete_token"' in page.text


def test_public_user_album_list_page_shows_owned_albums_sorted_by_recent_update(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, api_key = create_user_and_api_key(capsys, username="showcase", email="showcase@example.com")

    with TestClient(app) as client:
        first = client.post(
            "/api/v1/upload",
            files=[("file", ("first.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Older Album"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert first.status_code == 200

        second = client.post(
            "/api/v1/upload",
            files=[("file", ("second.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Newer Album"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert second.status_code == 200
        wait_for_thumbnail(client, first.json()["media_id"])
        wait_for_thumbnail(client, second.json()["media_id"])

        page = client.get("/u/showcase")
        assert page.status_code == 200
        assert "Public user album list." in page.text
        assert "Older Album" in page.text
        assert "Newer Album" in page.text
        assert f'/a/{first.json()["album_id"]}' in page.text
        assert f'/a/{second.json()["album_id"]}' in page.text
        assert page.text.index("Newer Album") < page.text.index("Older Album")


def test_public_user_album_list_page_hides_expired_albums_and_404s_for_missing_user(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="hidadmin", email="hidadmin@example.com")
    _, api_key = create_user_and_api_key(capsys, username="hidden", email="hidden@example.com")

    with TestClient(app) as client:
        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("expired.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Expired Album"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert upload.status_code == 200

        expired = client.patch(
            f"/api/v1/admin/albums/{upload.json()['album_id']}",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"expires_at": (utcnow() - timedelta(hours=1)).isoformat()},
        )
        assert expired.status_code == 200

        page = client.get("/u/hidden")
        assert page.status_code == 200
        assert "Expired Album" not in page.text
        assert "This user has no public albums yet." in page.text

        missing = client.get("/u/does-not-exist")
        assert missing.status_code == 404


def test_home_page_clears_stale_session_cookie_and_renders_anonymous_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("TRUSTED_PUBLIC_ORIGINS", "https://testserver")

    with TestClient(app, base_url="https://testserver") as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "stalesession",
                "email": "stalesession@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200

        state = client.app.state.imghost
        user = client.portal.call(state.repository.get_user_by_username, "stalesession")
        assert user is not None
        client.portal.call(state.repository.delete_user, user.id)

        page = client.get("/")
        assert page.status_code == 200
        assert 'href="/login"' in page.text
        assert 'id="login-form"' not in page.text
        assert "Invalid session." not in page.text
        assert "imghost_session=" in page.headers["set-cookie"]
