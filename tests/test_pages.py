from datetime import timedelta
from io import BytesIO
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from imghost.main import app
from imghost.models import User, utcnow

from .helpers import (
    PNG_1X1,
    browser_session_headers,
    create_admin_and_api_key,
    create_user_and_api_key,
    set_user_password,
    wait_for_thumbnail,
)

HOSTILE_TITLE = '</script><img src=x onerror=alert(1)>'
HOSTILE_FILENAME = '<svg onload=alert(1)>.png'
HOSTILE_USERNAME = '</script><img src=x onerror=alert(1)>'


def assert_no_active_markup(text: str) -> None:
    assert "</script><img" not in text
    assert "<img src=x onerror=alert(1)>" not in text
    assert "<svg onload=alert(1)>" not in text


def create_raw_user(client: TestClient, *, username: str, email: str, is_admin: bool = False) -> User:
    now = utcnow()
    user = User(
        id=str(uuid4()),
        username=username,
        email=email,
        password_hash=None,
        is_admin=is_admin,
        suspended=False,
        quota_bytes=None,
        rate_limit_rpm=None,
        rate_limit_bph=None,
        created_at=now,
        updated_at=now,
    )
    return client.portal.call(client.app.state.imghost.repository.create_user, user)


def test_static_base_css_is_served(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.get("/static/css/base.css")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/css")
        assert '@import url("/static/css/base-tokens.css");' in response.text
        assert '@import url("/static/css/base-components.css");' in response.text
        assert '@import url("/static/css/base-responsive.css");' in response.text


def test_home_page_shows_upload_and_auth_entry_points(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert '<link rel="stylesheet" href="/static/css/base.css">' in response.text
        assert '<script src="/static/js/upload-box.js" defer></script>' in response.text
        assert '<script src="/static/js/home.js" defer></script>' in response.text
        assert 'href="/login"' in response.text
        assert 'href="/register"' in response.text
        assert '<a class="nav-brand" href="/">ImgHost</a>' in response.text
        assert '>Home<' not in response.text
        assert 'id="login-form"' not in response.text
        assert 'id="register-form"' not in response.text
        assert 'id="upload-form"' in response.text
        assert 'id="upload-form" class="upload-form-modern"' in response.text
        assert 'data-upload-feedback' in response.text
        assert 'id="flash"' not in response.text
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
        assert "Hello browseruser" in page.text
        assert "Signed in as <strong>browseruser</strong>." not in page.text
        assert "Open dashboard" not in page.text
        assert 'id="logout-form"' not in page.text
        assert "Authenticated uploads do not expire by default." not in page.text
        assert "Uploads do not expire when you're logged in." in page.text
        assert "Anonymous uploads currently expire after 24 hour(s)." not in page.text
        assert 'data-logout-form' in page.text
        assert 'id="upload-form"' in page.text
        assert 'data-upload-feedback' in page.text
        assert 'id="flash"' not in page.text
        assert 'name="album_id"' not in page.text


def test_dashboard_page_uses_local_upload_feedback_anchor(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")

    with TestClient(app, base_url="https://testserver") as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "dashuser",
                "email": "dash@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200

        page = client.get("/dashboard")
        assert page.status_code == 200
        assert 'id="dashboard-upload-form"' in page.text
        assert 'data-upload-feedback' in page.text


def test_album_detail_page_uses_local_upload_feedback_anchor(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")

    with TestClient(app, base_url="https://testserver") as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "albumowner",
                "email": "albumowner@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200

        created = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Album detail"},
            headers=browser_session_headers(),
        )
        assert created.status_code == 200
        album_id = created.json()["album_id"]
        media_id = created.json()["items"][0]["media_id"]
        wait_for_thumbnail(client, media_id)

        page = client.get(f"/albums/{album_id}")
        assert page.status_code == 200
        assert 'id="album-upload-form"' in page.text
        assert 'data-upload-feedback' in page.text


def test_login_page_renders_form_and_register_link(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.get("/login")
        assert response.status_code == 200
        assert 'id="login-form"' in response.text
        assert 'data-auth-form' in response.text
        assert 'class="form-error hidden"' in response.text
        assert 'id="flash"' not in response.text
        assert 'href="/register"' in response.text
        assert '<script src="/static/js/auth.js" defer></script>' in response.text


def test_login_and_register_pages_show_google_button_when_enabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    with TestClient(app, base_url="https://testserver") as client:
        login = client.get("/login")
        register = client.get("/register")
        assert '/auth/google/start?mode=login' in login.text
        assert '/auth/google/start?mode=login' in register.text


def test_login_page_normalizes_next_to_internal_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        valid = client.get("/login", params={"next": "/albums?view=recent"})
        assert valid.status_code == 200
        assert 'data-success-url="/albums?view=recent"' in valid.text

        absolute = client.get("/login", params={"next": "https://evil.example/phish"})
        assert absolute.status_code == 200
        assert 'data-success-url="/dashboard"' in absolute.text

        protocol_relative = client.get("/login", params={"next": "//evil.example/phish"})
        assert protocol_relative.status_code == 200
        assert 'data-success-url="/dashboard"' in protocol_relative.text


def test_register_page_renders_form_when_enabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.get("/register")
        assert response.status_code == 200
        assert 'id="register-form"' in response.text
        assert 'data-auth-form' in response.text
        assert 'class="form-error hidden"' in response.text
        assert 'id="flash"' not in response.text
        assert 'href="/login"' in response.text


def test_register_page_normalizes_next_to_internal_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        valid = client.get("/register", params={"next": "/settings"})
        assert valid.status_code == 200
        assert 'data-success-url="/settings"' in valid.text

        external = client.get("/register", params={"next": "https://evil.example/register"})
        assert external.status_code == 200
        assert 'data-success-url="/dashboard"' in external.text


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
        ("/albums", "/login?next=%2Falbums"),
        ("/settings", "/login?next=%2Fsettings"),
        ("/admin", "/login?next=%2Fadmin"),
        ("/admin/users", "/login?next=%2Fadmin%2Fusers"),
        ("/admin/users/new", "/login?next=%2Fadmin%2Fusers%2Fnew"),
        ("/admin/albums", "/login?next=%2Fadmin%2Falbums"),
        ("/admin/config", "/login?next=%2Fadmin%2Fconfig"),
        ("/admin/ops", "/login?next=%2Fadmin%2Fops"),
    ],
)
def test_private_pages_redirect_logged_out_users_to_login(tmp_path, monkeypatch, path: str, expected_next: str) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == expected_next


@pytest.mark.parametrize("path", ["/admin", "/admin/users", "/admin/users/new", "/admin/albums", "/admin/config", "/admin/ops"])
def test_non_admin_user_gets_forbidden_on_admin_page(tmp_path, monkeypatch, path: str) -> None:
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

        response = client.get(path)
        assert response.status_code == 403
        assert response.json()["detail"] == "Admin access required."


def test_template_shell_wraps_phase_three_pages_and_private_pages(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, _ = create_user_and_api_key(capsys, username="dashuser", email="dash@example.com")
    admin_id, _ = create_admin_and_api_key(capsys, username="uiadmin", email="uiadmin@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        for public_path in ("/", "/login", "/register"):
            response = client.get(public_path)
            assert response.status_code == 200
            assert '<link rel="stylesheet" href="/static/css/base.css">' in response.text
            assert '<nav class="site-nav" aria-label="Primary">' in response.text

        set_user_password(client, user_id, "open-sesame")
        login = client.post("/api/v1/auth/login", json={"login": "dash@example.com", "password": "open-sesame"})
        assert login.status_code == 200

        dashboard = client.get("/dashboard")
        albums = client.get("/albums")
        settings = client.get("/settings")
        assert dashboard.status_code == 200
        assert albums.status_code == 200
        assert settings.status_code == 200
        assert "Your dashboard" in dashboard.text
        assert '<script src="/static/js/upload-box.js" defer></script>' in dashboard.text
        assert '<script src="/static/js/album-cards.js" defer></script>' in dashboard.text
        assert "All owned albums" in albums.text
        assert "Settings" in settings.text
        assert 'href="/albums"' in dashboard.text
        assert 'href="/settings"' in dashboard.text
        assert 'href="/dashboard"' in settings.text
        assert 'href="/admin"' not in dashboard.text

        client.post("/api/v1/auth/logout", headers=browser_session_headers("https://testserver", "/"))
        set_user_password(client, admin_id, "admin-pass")
        admin_login = client.post("/api/v1/auth/login", json={"login": "uiadmin@example.com", "password": "admin-pass"})
        assert admin_login.status_code == 200

        admin_page = client.get("/admin")
        assert admin_page.status_code == 200
        assert "Admin overview" in admin_page.text
        assert 'href="/admin"' in admin_page.text


def test_dashboard_page_focuses_on_uploads_recent_albums_and_links_to_settings(tmp_path, monkeypatch, capsys) -> None:
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
        assert "Your dashboard" in page.text
        assert "Signed-in home" in page.text
        assert 'id="dashboard-upload-form"' in page.text
        assert 'name="album_id"' not in page.text
        assert 'id="dashboard-recent-albums"' in page.text
        assert 'id="dashboard-recent-albums-status"' in page.text
        assert 'id="flash"' not in page.text
        assert 'href="/albums"' in page.text
        assert 'href="/settings"' in page.text
        assert "This is a quick resume view. Use the albums page for the full list." not in page.text
        assert "Keep moving" not in page.text
        assert 'id="change-password-form"' not in page.text


def test_albums_page_focuses_on_album_list_and_has_no_primary_upload_box(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, _ = create_user_and_api_key(capsys, username="albumlistuser", email="albumlist@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, user_id, "open-sesame")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "albumlist@example.com", "password": "open-sesame"},
        )
        assert login.status_code == 200

        page = client.get("/albums")
        assert page.status_code == 200
        assert "Your albums" not in page.text
        assert "Recent albums appear first. Open an album to continue managing it." not in page.text
        assert '<script src="/static/js/album-cards.js" defer></script>' in page.text
        assert 'id="owned-albums"' in page.text
        assert 'id="owned-albums-status"' in page.text
        assert 'id="owned-albums-pagination-status"' in page.text
        assert 'id="flash"' not in page.text
        assert 'id="albums-upload-form"' not in page.text
        assert 'href="/dashboard"' in page.text
        assert 'href="/settings"' in page.text


def test_private_album_page_renders_owner_workspace_shell(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, api_key = create_user_and_api_key(capsys, username="albumowner", email="albumowner@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, user_id, "secret-pass")
        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("detail.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Owner Album"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert upload.status_code == 200

        login = client.post(
            "/api/v1/auth/login",
            json={"login": "albumowner@example.com", "password": "secret-pass"},
        )
        assert login.status_code == 200

        page = client.get(f'/albums/{upload.json()["album_id"]}')
        assert page.status_code == 200
        assert "Manage album" not in page.text
        assert "Private album" not in page.text
        assert 'id="album-detail-bootstrap"' in page.text
        assert '"access_mode": "owner"' in page.text
        assert '"workspace_label": "Owner view"' in page.text
        assert '"post_delete_url": "/albums"' in page.text
        assert '"delete_token": null' in page.text
        assert 'id="album-detail-title"' in page.text
        assert 'id="album-detail-title-input"' in page.text
        assert 'id="album-detail-status"' in page.text
        assert 'id="flash"' not in page.text
        assert 'id="album-detail-add-images-button"' in page.text
        assert 'id="album-upload-form"' in page.text
        assert '<script src="/static/js/upload-box.js" defer></script>' in page.text
        assert '<script src="/static/js/album-detail-core.js" defer></script>' in page.text
        assert '<script src="/static/js/album-detail-render.js" defer></script>' in page.text
        assert '<script src="/static/js/album-detail-actions.js" defer></script>' in page.text
        assert '<script src="/static/js/album-detail.js" defer></script>' in page.text


def test_public_album_page_uses_template_shell_and_shows_owner_edit_link_only_for_owner(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, api_key = create_user_and_api_key(capsys, username="publicowner", email="publicowner@example.com")
    stranger_id, _ = create_user_and_api_key(capsys, username="publicstranger", email="publicstranger@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("public.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Public Album"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert upload.status_code == 200

        anonymous_page = client.get(f'/a/{upload.json()["album_id"]}')
        assert anonymous_page.status_code == 200
        assert '<link rel="stylesheet" href="/static/css/base.css">' in anonymous_page.text
        assert '<nav class="site-nav" aria-label="Primary">' in anonymous_page.text
        assert 'id="public-album-bootstrap"' in anonymous_page.text
        assert '"total_size_display"' in anonymous_page.text
        assert '"file_size_display"' in anonymous_page.text
        assert '<script src="/static/js/public-album.js" defer></script>' in anonymous_page.text
        assert "Public album" in anonymous_page.text
        assert 'id="flash"' not in anonymous_page.text
        assert "Edit Album" not in anonymous_page.text
        assert "delete_token=" not in anonymous_page.text

        set_user_password(client, user_id, "secret-pass")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "publicowner@example.com", "password": "secret-pass"},
        )
        assert login.status_code == 200

        owner_page = client.get(f'/a/{upload.json()["album_id"]}')
        assert owner_page.status_code == 200
        assert f'href="/albums/{upload.json()["album_id"]}"' in owner_page.text
        assert "Edit Album" in owner_page.text
        client.post("/api/v1/auth/logout", headers=browser_session_headers("https://testserver", "/"))

        set_user_password(client, stranger_id, "stranger-pass")
        stranger_login = client.post(
            "/api/v1/auth/login",
            json={"login": "publicstranger@example.com", "password": "stranger-pass"},
        )
        assert stranger_login.status_code == 200

        stranger_page = client.get(f'/a/{upload.json()["album_id"]}')
        assert stranger_page.status_code == 200
        assert "Edit Album" not in stranger_page.text


def test_public_user_albums_page_does_not_render_flash_markup(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")

    user_id, api_key = create_user_and_api_key(capsys, username="galleryowner", email="galleryowner@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("gallery.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Gallery Album"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert upload.status_code == 200

        page = client.get("/u/galleryowner")
        assert page.status_code == 200
        assert "galleryowner" in page.text
        assert 'id="flash"' not in page.text


def test_anonymous_manage_page_reuses_album_workspace_shell(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("anon.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Anon Manage Album"},
        )
        assert upload.status_code == 200
        payload = upload.json()
        token = payload["manage_url"].split("token=")[1]

        page = client.get(f"/manage/{payload['album_id']}", params={"token": token})
        assert page.status_code == 200
        assert 'id="album-detail-bootstrap"' in page.text
        assert '"access_mode": "token"' in page.text
        assert '"workspace_label": "Manage view"' in page.text
        assert '"post_delete_url": "/"' in page.text
        assert '"delete_token": "' in page.text
        assert "Manage view" in page.text
        assert 'id="album-detail-status"' in page.text
        assert 'id="flash"' not in page.text
        assert 'id="album-detail-add-images-button"' in page.text
        assert 'id="album-upload-form"' in page.text
        assert '<script src="/static/js/upload-box.js" defer></script>' in page.text
        assert '<script src="/static/js/album-detail-core.js" defer></script>' in page.text
        assert '<script src="/static/js/album-detail-render.js" defer></script>' in page.text
        assert '<script src="/static/js/album-detail-actions.js" defer></script>' in page.text
        assert '<script src="/static/js/album-detail.js" defer></script>' in page.text


def test_anonymous_manage_page_accepts_path_scoped_cookie_after_url_scrub(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("anon.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Anon Manage Cookie Album"},
        )
        assert upload.status_code == 200
        payload = upload.json()
        token = payload["manage_url"].split("token=")[1]

        client.cookies.set(
            f"imghost_manage_{payload['album_id']}",
            token,
            path=f"/manage/{payload['album_id']}",
        )

        page = client.get(f"/manage/{payload['album_id']}")
        assert page.status_code == 200
        assert '"access_mode": "token"' in page.text
        assert f'"album_id": "{payload["album_id"]}"' in page.text


def test_anonymous_manage_page_requires_valid_token(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("anon.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Anon Manage Album"},
        )
        assert upload.status_code == 200
        album_id = upload.json()["album_id"]

        missing = client.get(f"/manage/{album_id}")
        assert missing.status_code == 403

        invalid = client.get(f"/manage/{album_id}", params={"token": "wrong-token"})
        assert invalid.status_code == 403


def test_public_user_album_list_page_uses_template_shell(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    _, api_key = create_user_and_api_key(capsys, username="showcase2", email="showcase2@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("showcase.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Showcase Album"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert upload.status_code == 200
        wait_for_thumbnail(client, upload.json()["media_id"])

        page = client.get("/u/showcase2")
        assert page.status_code == 200
        assert '<link rel="stylesheet" href="/static/css/base.css">' in page.text
        assert '<nav class="site-nav" aria-label="Primary">' in page.text
        assert "Public user album list." in page.text
        assert "Showcase Album" in page.text
        assert "Created " in page.text
        assert f'/a/{upload.json()["album_id"]}' in page.text


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
        assert "Profile summary" in page.text
        assert "External tools" in page.text
        assert "Change password" in page.text
        assert "Danger Zone" in page.text
        assert 'id="settings-api-warning"' in page.text
        assert 'id="settings-api-warning" class="settings-warning-bubble hidden"' in page.text
        assert 'id="settings-account-summary"' in page.text
        assert 'id="reveal-api-key"' in page.text
        assert 'id="download-sharex-settings"' in page.text
        assert 'id="settings-password-form"' in page.text
        assert 'id="settings-password-status"' in page.text
        assert 'name="confirm_new_password"' in page.text
        assert 'id="settings-delete-account-form"' in page.text
        assert 'id="settings-delete-status"' in page.text


def test_settings_page_does_not_issue_api_key_just_by_loading(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    with TestClient(app, base_url="https://testserver") as client:
        user = create_raw_user(
            client,
            username="nokeysettingsuser",
            email="nokeysettings@example.com",
        )
        set_user_password(client, user.id, "open-sesame")
        assert client.portal.call(client.app.state.imghost.repository.get_api_key_for_user, user.id) is None

        login = client.post(
            "/api/v1/auth/login",
            json={"login": "nokeysettings@example.com", "password": "open-sesame"},
        )
        assert login.status_code == 200

        page = client.get("/settings")
        assert page.status_code == 200
        assert "Settings" in page.text
        assert client.portal.call(client.app.state.imghost.repository.get_api_key_for_user, user.id) is None


def test_static_settings_js_does_not_auto_issue_api_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/static/js/settings.js")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(("text/javascript", "application/javascript"))
        assert "No API key existed, so one was issued automatically." not in response.text
        assert "if (state.user && !state.user.has_api_key)" not in response.text


def test_settings_page_renders_google_oauth_controls_when_enabled(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    user_id, _ = create_user_and_api_key(capsys, username="oauthsettingsuser", email="oauthsettings@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, user_id, "open-sesame")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "oauthsettings@example.com", "password": "open-sesame"},
        )
        assert login.status_code == 200

        page = client.get("/settings")
        assert page.status_code == 200
        assert 'id="settings-google-connect"' in page.text
        assert 'id="settings-google-disconnect"' in page.text
        assert 'id="settings-oauth-status"' in page.text


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

        overview = client.get("/admin")
        assert overview.status_code == 200
        assert "Admin overview" in overview.text
        assert 'href="/admin/users"' in overview.text
        assert '<script src="/static/js/admin-common.js" defer></script>' in overview.text
        assert '<script src="/static/js/admin-index.js" defer></script>' in overview.text
        assert 'id="admin-overview-stats"' in overview.text
        assert 'id="admin-overview-stats-status"' in overview.text
        assert 'id="admin-overview-runtime"' in overview.text
        assert 'id="admin-overview-network-trust"' in overview.text
        assert 'id="admin-overview-runtime-status-text"' in overview.text
        assert "<h2>Network trust</h2>" in overview.text

        users = client.get("/admin/users")
        assert users.status_code == 200
        assert "User management" in users.text
        assert "open a detail page to review stats, albums, and account actions." in users.text
        assert 'id="admin-user-search-form"' in users.text
        assert 'id="admin-users"' in users.text
        assert 'href="/admin/users/new"' in users.text

        detail = client.get(f"/admin/users/{admin_id}")
        assert detail.status_code == 200
        assert "Admin user detail" in detail.text
        assert 'id="admin-user-detail-root"' in detail.text
        assert 'id="admin-user-detail-summary"' in detail.text
        assert 'id="admin-user-detail-stats"' in detail.text
        assert 'id="admin-user-albums"' in detail.text
        assert 'id="admin-user-albums-prev"' in detail.text
        assert 'id="admin-user-albums-next"' in detail.text
        assert '<script src="/static/js/album-cards.js" defer></script>' in detail.text
        assert '<script src="/static/js/admin-user-detail.js" defer></script>' in detail.text

        new_user = client.get("/admin/users/new")
        assert new_user.status_code == 200
        assert "Create user" in new_user.text
        assert 'id="admin-create-user-form"' in new_user.text
        assert 'href="/admin/users/new"' in new_user.text

        albums = client.get("/admin/albums")
        assert albums.status_code == 200
        assert "Album operations" in albums.text
        assert 'id="admin-album-search-form"' in albums.text
        assert 'id="admin-albums"' in albums.text
        assert 'href="/admin/users/new"' in albums.text

        config = client.get("/admin/config")
        assert config.status_code == 200
        assert "Runtime config" in config.text
        assert 'id="admin-config-form"' in config.text
        assert "Debug payload" in config.text
        assert 'href="/admin/users/new"' in config.text

        ops = client.get("/admin/ops")
        assert ops.status_code == 200
        assert "Operations" in ops.text
        assert '<script src="/static/js/admin-common.js" defer></script>' in ops.text
        assert '<script src="/static/js/admin-ops.js" defer></script>' in ops.text
        assert 'id="admin-runtime-status"' in ops.text
        assert 'id="admin-runtime-details"' in ops.text
        assert 'id="admin-network-trust"' in ops.text
        assert "Admin-only runtime details for queue, Redis, worker, and network trust." in ops.text
        assert "<h2>Admin-only runtime details</h2>" in ops.text
        assert "<h2>Network trust</h2>" in ops.text
        assert 'id="admin-audit-form"' in ops.text
        assert 'name="action"' in ops.text
        assert 'name="result"' in ops.text
        assert 'name="source"' in ops.text
        assert 'name="request_id"' in ops.text
        assert 'value="25"' in ops.text
        assert 'class="row row-actions section-gap-top"' in ops.text
        assert 'id="admin-audit-prev"' in ops.text
        assert 'id="admin-audit-next"' in ops.text
        assert 'href="/admin/users/new"' in ops.text


def test_album_pages_include_pagination_controls(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, _ = create_user_and_api_key(capsys, username="pagealbums", email="pagealbums@example.com")
    admin_id, _ = create_admin_and_api_key(capsys, username="pagealbumsadmin", email="pagealbumsadmin@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, user_id, "open-sesame")
        login = client.post("/api/v1/auth/login", json={"login": "pagealbums@example.com", "password": "open-sesame"})
        assert login.status_code == 200

        albums = client.get("/albums")
        assert albums.status_code == 200
        assert 'id="owned-albums-prev"' in albums.text
        assert 'id="owned-albums-next"' in albums.text
        assert 'id="owned-albums-summary"' in albums.text

        client.post("/api/v1/auth/logout", headers=browser_session_headers("https://testserver", "/"))
        set_user_password(client, admin_id, "admin-pass")
        admin_login = client.post("/api/v1/auth/login", json={"login": "pagealbumsadmin@example.com", "password": "admin-pass"})
        assert admin_login.status_code == 200

        admin_albums = client.get("/admin/albums")
        assert admin_albums.status_code == 200
        assert 'id="admin-albums-prev"' in admin_albums.text
        assert 'id="admin-albums-next"' in admin_albums.text
        assert 'id="admin-albums-summary"' in admin_albums.text


def test_admin_users_page_links_to_detail_page_and_detail_requires_real_user(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    admin_id, _ = create_admin_and_api_key(capsys, username="detailadmin", email="detailadmin@example.com")
    user_id, _ = create_user_and_api_key(capsys, username="detailtarget", email="detailtarget@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, admin_id, "admin-pass")
        login = client.post("/api/v1/auth/login", json={"login": "detailadmin@example.com", "password": "admin-pass"})
        assert login.status_code == 200

        users = client.get("/admin/users")
        assert users.status_code == 200
        assert '<script src="/static/js/admin-users.js" defer></script>' in users.text
        assert "Patch User" not in users.text
        assert "Reset Password" not in users.text
        assert "Delete User" not in users.text

        detail = client.get(f"/admin/users/{user_id}")
        assert detail.status_code == 200
        assert 'id="admin-user-detail-patch-form"' in detail.text
        assert 'id="admin-user-detail-is-admin"' in detail.text
        assert 'id="admin-user-detail-reset-form"' in detail.text
        assert 'id="admin-user-detail-delete"' in detail.text

        missing = client.get("/admin/users/00000000-0000-0000-0000-000000000000")
        assert missing.status_code == 404


def test_admin_user_detail_page_renders_for_user_with_no_albums(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    admin_id, _ = create_admin_and_api_key(capsys, username="emptydetailadmin", email="emptydetailadmin@example.com")
    user_id, _ = create_user_and_api_key(capsys, username="emptydetailuser", email="emptydetailuser@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, admin_id, "admin-pass")
        login = client.post("/api/v1/auth/login", json={"login": "emptydetailadmin@example.com", "password": "admin-pass"})
        assert login.status_code == 200

        detail = client.get(f"/admin/users/{user_id}")
        assert detail.status_code == 200
        assert 'id="admin-user-albums"' in detail.text
        assert 'id="admin-user-albums-summary"' in detail.text
        assert 'href="/admin/users"' in detail.text
        assert '<script src="/static/js/album-cards.js" defer></script>' in detail.text

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


def test_public_album_page_escapes_hostile_title_filename_and_bootstrap_json(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, api_key = create_user_and_api_key(capsys, username="hostilealbum", email="hostilealbum@example.com")

    with TestClient(app) as client:
        upload = client.post(
            "/api/v1/upload",
            files=[("file", (HOSTILE_FILENAME, BytesIO(PNG_1X1), "image/png"))],
            data={"title": HOSTILE_TITLE},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert upload.status_code == 200
        wait_for_thumbnail(client, upload.json()["media_id"])

        page = client.get(f"/a/{upload.json()['album_id']}")
        assert page.status_code == 200
        assert_no_active_markup(page.text)
        assert "\\u003c/script\\u003e\\u003cimg src=x onerror=alert(1)\\u003e" in page.text
        assert "&lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;" in page.text
        assert "&lt;svg onload=alert(1)&gt;.png" in page.text


def test_public_user_album_list_page_escapes_hostile_album_title(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, api_key = create_user_and_api_key(capsys, username="hostilelist", email="hostilelist@example.com")

    with TestClient(app) as client:
        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("safe.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": HOSTILE_TITLE},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert upload.status_code == 200
        wait_for_thumbnail(client, upload.json()["media_id"])

        page = client.get("/u/hostilelist")
        assert page.status_code == 200
        assert_no_active_markup(page.text)
        assert "&lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;" in page.text


def test_admin_user_detail_page_escapes_hostile_username_and_bootstrap_json(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    admin_id, _ = create_admin_and_api_key(capsys, username="xssadmin", email="xssadmin@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        hostile_user = create_raw_user(
            client,
            username=HOSTILE_USERNAME,
            email="hostile-bootstrap@example.com",
        )

        set_user_password(client, admin_id, "admin-pass")
        login = client.post("/api/v1/auth/login", json={"login": "xssadmin@example.com", "password": "admin-pass"})
        assert login.status_code == 200

        detail = client.get(f"/admin/users/{hostile_user.id}")
        assert detail.status_code == 200
        assert_no_active_markup(detail.text)
        assert "\\u003c/script\\u003e\\u003cimg src=x onerror=alert(1)\\u003e" in detail.text
        assert "&lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;" in detail.text


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
