import bcrypt
from io import BytesIO

from fastapi.testclient import TestClient

from imghost.main import app
from imghost.models import utcnow

from .helpers import (
    PNG_1X1,
    browser_session_headers,
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


def test_admin_password_reset_rejects_short_new_password(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="pwshortadmin", email="pwshortadmin@example.com")
    user_id, _ = create_user_and_api_key(capsys, username="shortreset", email="shortreset@example.com")

    with TestClient(app) as client:
        reset = client.post(
            f"/api/v1/admin/users/{user_id}/reset-password",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"new_password": "short7!"},
        )
        assert reset.status_code == 400
        assert reset.json()["detail"] == "New password must be at least 8 characters."


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
        assert "SameSite=lax" in login.headers["set-cookie"]
        assert "HttpOnly" in login.headers["set-cookie"]
        assert login.json()["authenticated"] is True

        me = client.get("/api/v1/user/me")
        assert me.status_code == 200
        assert me.json()["id"] == user_id

        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            headers=browser_session_headers("https://testserver", "/dashboard"),
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

        logout = client.post("/api/v1/auth/logout", headers=browser_session_headers("https://testserver", "/"))
        assert logout.status_code == 200
        assert logout.json()["authenticated"] is False
        assert "Secure" in logout.headers["set-cookie"]
        assert "SameSite=lax" in logout.headers["set-cookie"]

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
        assert "Uploads do not expire when you're logged in." in page.text
        assert 'href="/dashboard"' in page.text
        assert "Signed in as <strong>devcookie</strong>." not in page.text

        logout = client.post("/api/v1/auth/logout", headers=browser_session_headers("http://testserver", "/"))
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
        assert payload["DeletionURL"] == "$json:delete_url$"

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


def test_login_rate_limit_locks_repeated_bad_password_attempts(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="loginlimitadmin", email="loginlimitadmin@example.com")
    user_id, _ = create_user_and_api_key(capsys, username="loginlimituser", email="loginlimituser@example.com")

    with TestClient(app) as client:
        set_user_password(client, user_id, "correct-pass")
        configured = client.patch(
            "/api/v1/admin/config",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={
                "auth_rate_limit_login_account_failures": 2,
                "auth_rate_limit_login_account_window_seconds": 300,
                "auth_rate_limit_login_lock_seconds": 300,
            },
        )
        assert configured.status_code == 200

        first = client.post("/api/v1/auth/login", json={"login": "loginlimituser", "password": "wrong-pass"})
        assert first.status_code == 401

        second = client.post("/api/v1/auth/login", json={"login": "loginlimituser", "password": "wrong-pass"})
        assert second.status_code == 401

        blocked = client.post("/api/v1/auth/login", json={"login": "loginlimituser", "password": "correct-pass"})
        assert blocked.status_code == 429
        assert blocked.json()["detail"] == "Too many authentication attempts. Try again later."


def test_successful_login_clears_account_rate_limit_failures(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="loginclearadmin", email="loginclearadmin@example.com")
    user_id, _ = create_user_and_api_key(capsys, username="loginclearuser", email="loginclearuser@example.com")

    with TestClient(app) as client:
        set_user_password(client, user_id, "correct-pass")
        configured = client.patch(
            "/api/v1/admin/config",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={
                "auth_rate_limit_login_account_failures": 2,
                "auth_rate_limit_login_account_window_seconds": 300,
                "auth_rate_limit_login_lock_seconds": 300,
            },
        )
        assert configured.status_code == 200

        bad = client.post("/api/v1/auth/login", json={"login": "loginclearuser", "password": "wrong-pass"})
        assert bad.status_code == 401

        good = client.post("/api/v1/auth/login", json={"login": "loginclearuser", "password": "correct-pass"})
        assert good.status_code == 200

        bad_again = client.post("/api/v1/auth/login", json={"login": "loginclearuser", "password": "wrong-pass"})
        assert bad_again.status_code == 401


def test_registration_rate_limit_blocks_second_attempt_at_threshold(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="registerlimitadmin", email="registerlimitadmin@example.com")

    with TestClient(app) as client:
        configured = client.patch(
            "/api/v1/admin/config",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"auth_rate_limit_registration_ip_rpm": 1},
        )
        assert configured.status_code == 200

        first = client.post(
            "/api/v1/auth/register",
            json={"username": "registerone", "email": "registerone@example.com", "password": "secret-pass"},
        )
        assert first.status_code == 200

        second = client.post(
            "/api/v1/auth/register",
            json={"username": "registertwo", "email": "registertwo@example.com", "password": "secret-pass"},
        )
        assert second.status_code == 429
        assert second.json()["detail"] == "Too many authentication attempts. Try again later."


def test_api_key_failures_lock_the_client_ip(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="apikeylimitadmin", email="apikeylimitadmin@example.com")
    _, valid_user_key = create_user_and_api_key(capsys, username="apikeylimituser", email="apikeylimituser@example.com")

    with TestClient(app) as client:
        configured = client.patch(
            "/api/v1/admin/config",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={
                "auth_rate_limit_api_key_ip_failures": 2,
                "auth_rate_limit_api_key_ip_window_seconds": 300,
                "auth_rate_limit_api_key_lock_seconds": 300,
            },
        )
        assert configured.status_code == 200

        first = client.get("/api/v1/user/me", headers={"Authorization": "Bearer invalid-key"})
        assert first.status_code == 401

        second = client.get("/api/v1/user/me", headers={"Authorization": "Bearer invalid-key"})
        assert second.status_code == 401

        blocked = client.get("/api/v1/user/me", headers={"Authorization": f"Bearer {valid_user_key}"})
        assert blocked.status_code == 429
        assert blocked.json()["detail"] == "Too many authentication attempts. Try again later."


def test_admin_denials_lock_the_client_ip(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="admindenyadmin", email="admindenyadmin@example.com")
    _, user_key = create_user_and_api_key(capsys, username="admindenyuser", email="admindenyuser@example.com")

    with TestClient(app) as client:
        configured = client.patch(
            "/api/v1/admin/config",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={
                "auth_rate_limit_admin_ip_failures": 2,
                "auth_rate_limit_admin_ip_window_seconds": 300,
                "auth_rate_limit_admin_lock_seconds": 300,
            },
        )
        assert configured.status_code == 200

        first = client.get("/api/v1/admin/stats", headers={"Authorization": f"Bearer {user_key}"})
        assert first.status_code == 403

        second = client.get("/api/v1/admin/stats", headers={"Authorization": f"Bearer {user_key}"})
        assert second.status_code == 403

        blocked = client.get("/api/v1/admin/stats", headers={"Authorization": f"Bearer {admin_key}"})
        assert blocked.status_code == 429
        assert blocked.json()["detail"] == "Too many authentication attempts. Try again later."


def test_failed_login_attempts_are_audited_with_coarse_reasons(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="auditwatcher", email="auditwatcher@example.com")
    user_id, _ = create_user_and_api_key(capsys, username="failedloginuser", email="failedlogin@example.com")

    with TestClient(app) as client:
        set_user_password(client, user_id, "correct-pass")

        bad_password = client.post(
            "/api/v1/auth/login",
            headers={"X-Correlation-ID": "bad-password-flow"},
            json={"login": "failedloginuser", "password": "wrong-pass"},
        )
        assert bad_password.status_code == 401

        missing_user = client.post(
            "/api/v1/auth/login",
            headers={"X-Correlation-ID": "missing-user-flow"},
            json={"login": "no-such-user", "password": "wrong-pass"},
        )
        assert missing_user.status_code == 401

        suspended = client.patch(
            f"/api/v1/admin/users/{user_id}",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"suspended": True},
        )
        assert suspended.status_code == 200

        suspended_login = client.post(
            "/api/v1/auth/login",
            headers={"X-Correlation-ID": "suspended-flow"},
            json={"login": "failedlogin@example.com", "password": "correct-pass"},
        )
        assert suspended_login.status_code == 403

        for correlation_id, identifier, reason in (
            ("bad-password-flow", "failedloginuser", "invalid_credentials"),
            ("missing-user-flow", "no-such-user", "invalid_credentials"),
            ("suspended-flow", "failedlogin@example.com", "suspended"),
        ):
            audit = client.get(
                "/api/v1/admin/audit",
                headers={"Authorization": f"Bearer {admin_key}"},
                params={"event_type": "login_failed", "correlation_id": correlation_id},
            )
            assert audit.status_code == 200
            payload = audit.json()
            assert len(payload) == 1
            assert payload[0]["target_type"] == "auth"
            assert payload[0]["target_id"] == identifier
            assert payload[0]["metadata"]["login_identifier"] == identifier
            assert payload[0]["metadata"]["reason"] == reason


def test_logout_writes_audit_event_for_authenticated_browser_session(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    admin_id, admin_key = create_admin_and_api_key(capsys, username="logoutauditadmin", email="logoutauditadmin@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, admin_id, "admin-pass")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "logoutauditadmin", "password": "admin-pass"},
        )
        assert login.status_code == 200

        logout = client.post(
            "/api/v1/auth/logout",
            headers={"X-Correlation-ID": "logout-flow", **browser_session_headers("https://testserver", "/")},
        )
        assert logout.status_code == 200

        audit = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "logout", "actor_id": admin_id, "correlation_id": "logout-flow"},
        )
        assert audit.status_code == 200
        payload = audit.json()
        assert len(payload) == 1
        assert payload[0]["target_id"] == admin_id
        assert payload[0]["metadata"]["target_user_id"] == admin_id


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
        assert "SameSite=lax" in registered.headers["set-cookie"]
        assert "HttpOnly" in registered.headers["set-cookie"]
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


def test_registration_rejects_password_shorter_than_eight_characters(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/api/v1/auth/register",
            json={
                "username": "shortregister",
                "email": "shortregister@example.com",
                "password": "short7!",
            },
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "Password must be at least 8 characters."


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
        assert "SameSite=lax" in login.headers["set-cookie"]


def test_admin_browser_session_can_mutate_user_and_album_routes_used_by_ui(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    admin_id, _ = create_admin_and_api_key(capsys, username="csrfadmin", email="csrfadmin@example.com")
    target_user_id, target_user_key = create_user_and_api_key(capsys, username="csrftarget", email="csrftarget@example.com")
    album_owner_id, album_owner_key = create_user_and_api_key(capsys, username="csrfalbum", email="csrfalbum@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, admin_id, "admin-pass")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "csrfadmin@example.com", "password": "admin-pass"},
        )
        assert login.status_code == 200

        patch_user = client.patch(
            f"/api/v1/admin/users/{target_user_id}",
            json={"is_admin": True, "quota_bytes": 1234, "rate_limit_rpm": 9},
            headers=browser_session_headers("https://testserver", f"/admin/users/{target_user_id}"),
        )
        assert patch_user.status_code == 200
        assert patch_user.json()["is_admin"] is True
        assert patch_user.json()["quota_bytes"] == 1234
        assert patch_user.json()["rate_limit_rpm"] == 9

        reset_password = client.post(
            f"/api/v1/admin/users/{target_user_id}/reset-password",
            json={"new_password": "new-target-pass"},
            headers=browser_session_headers("https://testserver", f"/admin/users/{target_user_id}"),
        )
        assert reset_password.status_code == 200
        assert reset_password.json() == {"reset": True, "user_id": target_user_id}

        target_login = client.post(
            "/api/v1/auth/login",
            json={"login": "csrftarget", "password": "new-target-pass"},
        )
        assert target_login.status_code == 200

        target_admin_runtime = client.get(
            "/api/v1/admin/runtime-status",
            headers=browser_session_headers("https://testserver", f"/admin/users/{target_user_id}"),
        )
        assert target_admin_runtime.status_code == 200

        client.post("/api/v1/auth/logout", headers=browser_session_headers("https://testserver", "/"))
        relogin = client.post(
            "/api/v1/auth/login",
            json={"login": "csrfadmin@example.com", "password": "admin-pass"},
        )
        assert relogin.status_code == 200

        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            headers={"Authorization": f"Bearer {album_owner_key}"},
            data={"title": "Admin Session Album"},
        )
        assert upload.status_code == 200
        album_id = upload.json()["album_id"]

        patch_album = client.patch(
            f"/api/v1/admin/albums/{album_id}",
            json={"expires_at": utcnow().replace(microsecond=0).isoformat()},
            headers=browser_session_headers("https://testserver", "/admin/albums"),
        )
        assert patch_album.status_code == 200
        assert patch_album.json()["id"] == album_id

        delete_album = client.delete(
            f"/api/v1/admin/albums/{album_id}",
            headers=browser_session_headers("https://testserver", "/admin/albums"),
        )
        assert delete_album.status_code == 200
        assert delete_album.json()["deleted"] is True


def test_browser_session_mutation_rejects_cross_origin_password_change(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, _ = create_user_and_api_key(capsys, username="csrfblocked", email="csrfblocked@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, user_id, "old-pass")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "csrfblocked@example.com", "password": "old-pass"},
        )
        assert login.status_code == 200

        blocked = client.patch(
            "/api/v1/user/me/password",
            headers={"Origin": "https://evil.example", "Referer": "https://evil.example/account"},
            json={"current_password": "old-pass", "new_password": "new-pass"},
        )
        assert blocked.status_code == 403
        assert blocked.json()["detail"] == "CSRF protection blocked the request."


def test_browser_session_mutation_allows_trusted_referer_without_origin(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, old_api_key = create_user_and_api_key(capsys, username="csrfreferer", email="csrfreferer@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, user_id, "open-sesame")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "csrfreferer@example.com", "password": "open-sesame"},
        )
        assert login.status_code == 200

        rotated = client.post(
            "/api/v1/user/me/api-key",
            headers={"Referer": "https://testserver/settings"},
        )
        assert rotated.status_code == 200
        assert rotated.json()["api_key"] != old_api_key


def test_browser_session_mutation_rejects_missing_origin_and_referer(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, _ = create_user_and_api_key(capsys, username="csrfmissing", email="csrfmissing@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, user_id, "open-sesame")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "csrfmissing@example.com", "password": "open-sesame"},
        )
        assert login.status_code == 200

        blocked = client.delete("/api/v1/user/me")
        assert blocked.status_code == 403
        assert blocked.json()["detail"] == "CSRF protection blocked the request."


def test_browser_session_mutation_allows_trusted_alternate_public_origin(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("TRUSTED_PUBLIC_ORIGINS", "https://testserver,https://cdn.testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, old_api_key = create_user_and_api_key(capsys, username="csrfaltorigin", email="csrfaltorigin@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, user_id, "open-sesame")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "csrfaltorigin@example.com", "password": "open-sesame"},
        )
        assert login.status_code == 200

        rotated = client.post(
            "/api/v1/user/me/api-key",
            headers=browser_session_headers("https://cdn.testserver", "/settings"),
        )
        assert rotated.status_code == 200
        assert rotated.json()["api_key"] != old_api_key


def test_browser_session_mutation_allows_direct_lan_origin_when_public_origin_mode_is_disabled(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://192.168.0.100:8000")
    monkeypatch.setenv("PUBLIC_ORIGIN_ENABLED", "false")
    monkeypatch.setenv("TRUSTED_PUBLIC_ORIGINS", "http://localhost:8000")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, old_api_key = create_user_and_api_key(capsys, username="csrflanorigin", email="csrflanorigin@example.com")

    with TestClient(app, base_url="http://192.168.0.55:8000") as client:
        set_user_password(client, user_id, "open-sesame")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "csrflanorigin@example.com", "password": "open-sesame"},
        )
        assert login.status_code == 200

        rotated = client.post(
            "/api/v1/user/me/api-key",
            headers=browser_session_headers("http://192.168.0.55:8000", "/settings"),
        )
        assert rotated.status_code == 200
        assert rotated.json()["api_key"] != old_api_key


def test_browser_session_mutation_allows_trusted_forwarded_origin_in_local_mode(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://192.168.0.100:8000")
    monkeypatch.setenv("PUBLIC_ORIGIN_ENABLED", "false")
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS_ENABLED", "true")
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "127.0.0.1/32")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, old_api_key = create_user_and_api_key(capsys, username="csrfproxylocal", email="csrfproxylocal@example.com")

    with TestClient(app, base_url="http://backend:8000", client=("127.0.0.1", 50000)) as client:
        forwarded_headers = {
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "albums.lan.example",
        }
        set_user_password(client, user_id, "open-sesame")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "csrfproxylocal@example.com", "password": "open-sesame"},
            headers=forwarded_headers,
        )
        assert login.status_code == 200

        rotated = client.post(
            "/api/v1/user/me/api-key",
            headers={
                **forwarded_headers,
                **browser_session_headers("https://albums.lan.example", "/settings"),
            },
        )
        assert rotated.status_code == 200
        assert rotated.json()["api_key"] != old_api_key


def test_browser_session_mutation_rejects_mismatched_forwarded_origin_in_local_mode(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://192.168.0.100:8000")
    monkeypatch.setenv("PUBLIC_ORIGIN_ENABLED", "false")
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS_ENABLED", "true")
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "127.0.0.1/32")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, _ = create_user_and_api_key(capsys, username="csrfproxymismatch", email="csrfproxymismatch@example.com")

    with TestClient(app, base_url="http://backend:8000", client=("127.0.0.1", 50000)) as client:
        forwarded_headers = {
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "albums.lan.example",
        }
        set_user_password(client, user_id, "open-sesame")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "csrfproxymismatch@example.com", "password": "open-sesame"},
            headers=forwarded_headers,
        )
        assert login.status_code == 200

        blocked = client.post(
            "/api/v1/user/me/api-key",
            headers={
                **forwarded_headers,
                **browser_session_headers("https://other.lan.example", "/settings"),
            },
        )
        assert blocked.status_code == 403
        assert blocked.json()["detail"] == "CSRF protection blocked the request."


def test_browser_session_mutation_rejects_malformed_referer_without_origin(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, _ = create_user_and_api_key(capsys, username="csrfbadreferer", email="csrfbadreferer@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, user_id, "open-sesame")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "csrfbadreferer@example.com", "password": "open-sesame"},
        )
        assert login.status_code == 200

        blocked = client.post("/api/v1/user/me/api-key", headers={"Referer": "/settings"})
        assert blocked.status_code == 403
        assert blocked.json()["detail"] == "CSRF protection blocked the request."


def test_bearer_mutation_bypasses_session_csrf_even_with_session_cookie_present(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    session_user_id, _ = create_user_and_api_key(capsys, username="csrfsession", email="csrfsession@example.com")
    bearer_user_id, bearer_key = create_user_and_api_key(capsys, username="csrfbearer", email="csrfbearer@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, session_user_id, "open-sesame")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "csrfsession@example.com", "password": "open-sesame"},
        )
        assert login.status_code == 200

        rotated = client.post(
            "/api/v1/user/me/api-key",
            headers={
                "Authorization": f"Bearer {bearer_key}",
                "Origin": "https://evil.example",
                "Referer": "https://evil.example/settings",
            },
        )
        assert rotated.status_code == 200

        me = client.get("/api/v1/user/me", headers={"Authorization": f"Bearer {rotated.json()['api_key']}"})
        assert me.status_code == 200
        assert me.json()["id"] == bearer_user_id


def test_login_and_register_mutations_remain_exempt_without_csrf_headers(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    with TestClient(app, base_url="https://testserver") as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "csrfexempt",
                "email": "csrfexempt@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200

        logout = client.post("/api/v1/auth/logout", headers=browser_session_headers("https://testserver", "/"))
        assert logout.status_code == 200

        login = client.post(
            "/api/v1/auth/login",
            json={"login": "csrfexempt@example.com", "password": "secret-pass"},
        )
        assert login.status_code == 200


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


def test_registration_respects_allow_registration_env_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ALLOW_REGISTRATION", "false")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/register",
            json={
                "username": "blockedenv",
                "email": "blockedenv@example.com",
                "password": "secret-pass",
            },
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "Registration is disabled."
