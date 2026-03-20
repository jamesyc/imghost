import bcrypt
from io import BytesIO

from fastapi.testclient import TestClient

from imghost.main import app

from .helpers import (
    PNG_1X1,
    create_admin_and_api_key,
    create_user_and_api_key,
    get_user_record,
    set_user_password,
    wait_for_thumbnail,
)


def test_admin_password_reset_requires_dedicated_endpoint_and_allows_new_login(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="pwadmin", email="pwadmin@example.com")
    user_id, _ = create_user_and_api_key(capsys, username="resetme", email="resetme@example.com")

    with TestClient(app) as client:
        rejected = client.patch(
            f"/api/v1/admin/users/{user_id}",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"password": "new-admin-pass"},
        )
        assert rejected.status_code == 400
        assert "dedicated admin password reset endpoint" in rejected.json()["detail"]

        reset = client.post(
            f"/api/v1/admin/users/{user_id}/reset-password",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"new_password": "new-admin-pass"},
        )
        assert reset.status_code == 200
        assert reset.json() == {"reset": True, "user_id": user_id}

        login = client.post(
            "/api/v1/auth/login",
            json={"login": "resetme", "password": "new-admin-pass"},
        )
        assert login.status_code == 200
        assert login.json()["authenticated"] is True


def test_user_can_change_password_with_current_password(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    user_id, api_key = create_user_and_api_key(capsys, username="iris", email="iris@example.com")

    with TestClient(app) as client:
        set_user_password(client, user_id, "old-pass")
        bad = client.patch(
            "/api/v1/user/me/password",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"current_password": "wrong", "new_password": "new-pass"},
        )
        assert bad.status_code == 403

        good = client.patch(
            "/api/v1/user/me/password",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"current_password": "old-pass", "new_password": "new-pass"},
        )
        assert good.status_code == 200
        assert good.json()["updated"] is True

        updated_user = get_user_record(client, user_id)
        assert updated_user is not None
        assert updated_user.password_hash != "new-pass"
        assert bcrypt.checkpw(b"new-pass", updated_user.password_hash.encode("utf-8"))


def test_local_login_sets_session_cookie_and_authenticates_browser_flow(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, _ = create_user_and_api_key(capsys, username="kira", email="kira@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, user_id, "open-sesame")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "kira@example.com", "password": "open-sesame"},
        )
        assert login.status_code == 200
        assert "imghost_session=" in login.headers["set-cookie"]
        assert "Secure" in login.headers["set-cookie"]
        assert "Max-Age=" in login.headers["set-cookie"]
        assert login.json()["authenticated"] is True

        me = client.get("/api/v1/user/me")
        assert me.status_code == 200
        assert me.json()["id"] == user_id

        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
        )
        assert upload.status_code == 200
        payload = upload.json()
        wait_for_thumbnail(client, payload["media_id"])

        state = client.app.state.imghost
        album = client.portal.call(state.repository.get_album, payload["album_id"])
        assert album is not None
        assert album.user_id == user_id
        assert album.expires_at is None
        assert album.delete_token is None

        logout = client.post("/api/v1/auth/logout")
        assert logout.status_code == 200
        assert logout.json()["authenticated"] is False
        assert "Secure" in logout.headers["set-cookie"]

        after_logout = client.get("/api/v1/user/me")
        assert after_logout.status_code == 401


def test_local_http_login_uses_insecure_cookie_for_dev_refreshes(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, _ = create_user_and_api_key(capsys, username="devcookie", email="devcookie@example.com")

    with TestClient(app) as client:
        set_user_password(client, user_id, "open-sesame")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "devcookie@example.com", "password": "open-sesame"},
        )
        assert login.status_code == 200
        assert "imghost_session=" in login.headers["set-cookie"]
        assert "Secure" not in login.headers["set-cookie"]

        me = client.get("/api/v1/user/me")
        assert me.status_code == 200
        assert me.json()["id"] == user_id

        page = client.get("/")
        assert page.status_code == 200
        assert "Signed in as <strong>devcookie</strong>." in page.text

        logout = client.post("/api/v1/auth/logout")
        assert logout.status_code == 200
        assert "Secure" not in logout.headers["set-cookie"]


def test_local_login_supports_username_session_cookie_and_browser_sharex_download_rotates_key(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, old_api_key = create_user_and_api_key(capsys, username="lena", email="lena@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, user_id, "letmein")
        bad = client.post(
            "/api/v1/auth/login",
            json={"login": "lena", "password": "wrong"},
        )
        assert bad.status_code == 401

        login = client.post(
            "/api/v1/auth/login",
            json={"login": "lena", "password": "letmein", "remember_me": False},
        )
        assert login.status_code == 200
        assert "imghost_session=" in login.headers["set-cookie"]
        assert "Secure" in login.headers["set-cookie"]
        assert "Max-Age=" not in login.headers["set-cookie"]

        sharex = client.get("/api/v1/user/me/sharex-config")
        assert sharex.status_code == 200
        payload = sharex.json()
        new_auth_header = payload["Headers"]["Authorization"]
        assert new_auth_header.startswith("Bearer ")
        new_api_key = new_auth_header.removeprefix("Bearer ")
        assert new_api_key != old_api_key

        old_me = client.get("/api/v1/user/me", headers={"Authorization": f"Bearer {old_api_key}"})
        assert old_me.status_code == 401

        new_me = client.get("/api/v1/user/me", headers={"Authorization": f"Bearer {new_api_key}"})
        assert new_me.status_code == 200
        assert new_me.json()["username"] == "lena"


def test_admin_local_login_writes_admin_login_audit_event(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    admin_id, admin_key = create_admin_and_api_key(capsys, username="auditloginadmin", email="auditloginadmin@example.com")

    with TestClient(app) as client:
        set_user_password(client, admin_id, "admin-pass")
        login = client.post(
            "/api/v1/auth/login",
            headers={"X-Correlation-ID": "admin-login-flow"},
            json={"login": "auditloginadmin", "password": "admin-pass"},
        )
        assert login.status_code == 200

        audit = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "admin_login", "actor_id": admin_id, "correlation_id": "admin-login-flow"},
        )
        assert audit.status_code == 200
        payload = audit.json()
        assert len(payload) == 1
        assert payload[0]["target_type"] == "user"
        assert payload[0]["target_id"] == admin_id
        assert payload[0]["metadata"]["source"] == "web"


def test_non_admin_local_login_does_not_write_admin_login_audit_event(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="watcheradmin", email="watcheradmin@example.com")
    user_id, _ = create_user_and_api_key(capsys, username="plainuser", email="plainuser@example.com")

    with TestClient(app) as client:
        set_user_password(client, user_id, "user-pass")
        login = client.post(
            "/api/v1/auth/login",
            headers={"X-Correlation-ID": "plain-login-flow"},
            json={"login": "plainuser", "password": "user-pass"},
        )
        assert login.status_code == 200

        audit = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "admin_login", "correlation_id": "plain-login-flow"},
        )
        assert audit.status_code == 200
        assert audit.json() == []


def test_registration_creates_user_session_and_audit_entry(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="regadmin", email="regadmin@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        registered = client.post(
            "/api/v1/auth/register",
            headers={"X-Correlation-ID": "register-flow"},
            json={
                "username": "newuser",
                "email": "newuser@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200
        assert "imghost_session=" in registered.headers["set-cookie"]
        assert "Secure" in registered.headers["set-cookie"]
        payload = registered.json()
        assert payload["authenticated"] is True
        user_id = payload["user"]["id"]
        assert payload["user"]["username"] == "newuser"

        me = client.get("/api/v1/user/me")
        assert me.status_code == 200
        assert me.json()["id"] == user_id

        audit = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "user_created", "correlation_id": "register-flow"},
        )
        assert audit.status_code == 200
        audit_payload = audit.json()
        assert len(audit_payload) == 1
        assert audit_payload[0]["actor_id"] == user_id
        assert audit_payload[0]["metadata"]["method"] == "registration"
        assert audit_payload[0]["target_id"] == user_id


def test_session_cookie_secure_can_be_overridden_for_http_deployments(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, _ = create_user_and_api_key(capsys, username="forcedsecure", email="forcedsecure@example.com")

    with TestClient(app) as client:
        set_user_password(client, user_id, "open-sesame")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "forcedsecure@example.com", "password": "open-sesame"},
        )
        assert login.status_code == 200
        assert "Secure" in login.headers["set-cookie"]


def test_registration_respects_allow_registration_runtime_config(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="regcfgadmin", email="regcfgadmin@example.com")

    with TestClient(app) as client:
        disabled = client.patch(
            "/api/v1/admin/config",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"allow_registration": False},
        )
        assert disabled.status_code == 200

        response = client.post(
            "/api/v1/auth/register",
            json={
                "username": "blocked",
                "email": "blocked@example.com",
                "password": "secret-pass",
            },
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "Registration is disabled."
