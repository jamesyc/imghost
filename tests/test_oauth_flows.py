from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient

from imghost.main import app
from imghost.models import User, UserSsoLink, utcnow

from .helpers import browser_session_headers, create_admin_and_api_key, set_user_password


@dataclass
class FakeGoogleIdentity:
    provider: str = "google"
    provider_uid: str = "google-user-1"
    email: str = "googleuser@example.com"
    email_verified: bool = True
    display_name: str | None = "Google User"
    avatar_url: str | None = None


class FakeGoogleProvider:
    name = "google"

    def __init__(self, identity: FakeGoogleIdentity | None = None, error: Exception | None = None) -> None:
        self.identity = identity or FakeGoogleIdentity()
        self.error = error
        self.last_state: str | None = None
        self.last_redirect_uri: str | None = None
        self.last_code: str | None = None

    def authorization_url(self, *, redirect_uri: str, state: str) -> str:
        self.last_state = state
        self.last_redirect_uri = redirect_uri
        return f"https://fake.google/auth?state={state}"

    async def exchange_code(self, *, code: str, redirect_uri: str):
        self.last_code = code
        self.last_redirect_uri = redirect_uri
        if self.error is not None:
            raise self.error
        return self.identity


def _create_raw_user(client: TestClient, *, username: str, email: str, suspended: bool = False) -> User:
    now = utcnow()
    user = User(
        id=str(uuid4()),
        username=username,
        email=email,
        password_hash=None,
        is_admin=False,
        suspended=suspended,
        quota_bytes=None,
        rate_limit_rpm=None,
        rate_limit_bph=None,
        created_at=now,
        updated_at=now,
    )
    return client.portal.call(client.app.state.imghost.repository.create_user, user)


def _link_google_account(client: TestClient, *, user_id: str, provider_uid: str) -> None:
    client.portal.call(
        client.app.state.imghost.repository.create_user_sso_link,
        UserSsoLink(
            id=str(uuid4()),
            user_id=user_id,
            provider="google",
            provider_uid=provider_uid,
            linked_at=datetime.now(UTC),
        ),
    )


def _install_fake_google_provider(client: TestClient, provider: FakeGoogleProvider) -> FakeGoogleProvider:
    client.app.state.imghost.oauth_providers["google"] = provider
    return provider


def _oauth_state_from_redirect(response) -> str:
    location = response.headers["location"]
    query = parse_qs(urlparse(location).query)
    return query["state"][0]


def _set_browser_session(client: TestClient, user: User) -> None:
    token, _ = client.portal.call(
        lambda: client.app.state.imghost.session_backend.create_session(user, remember_me=True)
    )
    client.cookies.set(client.app.state.imghost.settings.session_cookie_name, token)


def test_google_oauth_pages_hide_buttons_when_provider_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")

    with TestClient(app, base_url="https://testserver") as client:
        login = client.get("/login")
        register = client.get("/register")
        assert "/auth/google/start" not in login.text
        assert "/auth/google/start" not in register.text
        assert client.get("/auth/google/start").status_code == 404


def test_google_oauth_pages_show_buttons_when_provider_enabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    with TestClient(app, base_url="https://testserver") as client:
        login = client.get("/login")
        register = client.get("/register")
        assert "/auth/google/start?mode=login" in login.text
        assert "/auth/google/start?mode=login" in register.text


def test_google_oauth_start_redirects_to_provider_with_signed_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    with TestClient(app, base_url="https://testserver") as client:
        provider = _install_fake_google_provider(client, FakeGoogleProvider())
        response = client.get("/auth/google/start", params={"next": "/albums?view=recent"}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("https://fake.google/auth?")
        assert provider.last_redirect_uri == "https://testserver/auth/google/callback"
        assert provider.last_state


def test_google_oauth_link_mode_requires_signed_in_user(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/auth/google/start", params={"mode": "link"}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login?next=%2Fsettings"


def test_google_oauth_callback_rejects_invalid_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/auth/google/callback", params={"state": "bad", "code": "abc"}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login?")
        assert "oauth_error=Google+sign-in+could+not+be+verified." in response.headers["location"]


def test_google_oauth_callback_rejects_provider_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    with TestClient(app, base_url="https://testserver") as client:
        provider = _install_fake_google_provider(client, FakeGoogleProvider())
        started = client.get("/auth/google/start", follow_redirects=False)
        state = _oauth_state_from_redirect(started)
        response = client.get("/auth/google/callback", params={"state": state, "error": "access_denied"}, follow_redirects=False)
        assert response.status_code == 303
        assert "oauth_error=Google+sign-in+was+cancelled+or+denied." in response.headers["location"]
        assert provider.last_code is None


def test_google_oauth_existing_link_logs_user_in(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    with TestClient(app, base_url="https://testserver") as client:
        user = _create_raw_user(client, username="linkeduser", email="linked@example.com")
        _link_google_account(client, user_id=user.id, provider_uid="google-linked")
        provider = _install_fake_google_provider(
            client,
            FakeGoogleProvider(identity=FakeGoogleIdentity(provider_uid="google-linked", email="linked@example.com")),
        )
        started = client.get("/auth/google/start", params={"next": "/albums"}, follow_redirects=False)
        state = _oauth_state_from_redirect(started)
        response = client.get("/auth/google/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/albums"
        assert "imghost_session=" in response.headers["set-cookie"]
        assert provider.last_code == "abc"


def test_google_oauth_signed_in_user_can_link_account(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("SECRET_KEY", "oauth-link-secret")

    with TestClient(app, base_url="https://testserver") as client:
        user = _create_raw_user(client, username="localuser", email="local@example.com")
        set_user_password(client, user.id, "secret-pass")
        _set_browser_session(client, user)
        provider = _install_fake_google_provider(
            client,
            FakeGoogleProvider(identity=FakeGoogleIdentity(provider_uid="google-local", email="googlelocal@example.com")),
        )
        started = client.get("/auth/google/start", params={"mode": "link", "next": "/settings"}, follow_redirects=False)
        state = _oauth_state_from_redirect(started)
        response = client.get("/auth/google/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/settings?oauth_status=Google+account+connected.&oauth_tone=success"
        links = client.portal.call(client.app.state.imghost.repository.list_user_sso_links, user.id)
        assert len(links) == 1
        assert links[0].provider == "google"


def test_google_oauth_linking_fails_if_identity_linked_elsewhere(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    with TestClient(app, base_url="https://testserver") as client:
        owner = _create_raw_user(client, username="owner", email="owner@example.com")
        _link_google_account(client, user_id=owner.id, provider_uid="taken-google")

        user = _create_raw_user(client, username="otheruser", email="other@example.com")
        set_user_password(client, user.id, "secret-pass")
        _set_browser_session(client, user)

        _install_fake_google_provider(
            client,
            FakeGoogleProvider(identity=FakeGoogleIdentity(provider_uid="taken-google", email="taken@example.com")),
        )
        started = client.get("/auth/google/start", params={"mode": "link", "next": "/settings"}, follow_redirects=False)
        state = _oauth_state_from_redirect(started)
        response = client.get("/auth/google/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        assert response.status_code == 303
        assert "oauth_tone=error" in response.headers["location"]
        assert "already+linked+to+a+different+user" in response.headers["location"]


def test_google_oauth_creates_new_user_when_allowed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    with TestClient(app, base_url="https://testserver") as client:
        _install_fake_google_provider(
            client,
            FakeGoogleProvider(identity=FakeGoogleIdentity(provider_uid="new-google", email="newgoogle@example.com")),
        )
        started = client.get("/auth/google/start", follow_redirects=False)
        state = _oauth_state_from_redirect(started)
        response = client.get("/auth/google/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard"
        me = client.get("/api/v1/user/me")
        assert me.status_code == 200
        payload = me.json()
        assert payload["email"] == "newgoogle@example.com"
        assert payload["has_password"] is False
        assert any(provider["provider"] == "google" for provider in payload["sso_providers"])


def test_google_oauth_created_user_can_set_local_password_without_current_password(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    with TestClient(app, base_url="https://testserver") as client:
        _install_fake_google_provider(
            client,
            FakeGoogleProvider(identity=FakeGoogleIdentity(provider_uid="pw-google", email="pwgoogle@example.com")),
        )
        started = client.get("/auth/google/start", follow_redirects=False)
        state = _oauth_state_from_redirect(started)
        callback = client.get("/auth/google/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        assert callback.status_code == 303

        changed = client.patch(
            "/api/v1/user/me/password",
            headers={"Content-Type": "application/json", **browser_session_headers("https://testserver", "/settings")},
            json={"current_password": "", "new_password": "new-secret-pass"},
        )
        assert changed.status_code == 200
        logout = client.post("/api/v1/auth/logout", headers=browser_session_headers("https://testserver", "/"))
        assert logout.status_code == 200

        login = client.post("/api/v1/auth/login", json={"login": "pwgoogle@example.com", "password": "new-secret-pass"})
        assert login.status_code == 200


def test_google_oauth_new_account_creation_blocked_when_registration_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    with TestClient(app, base_url="https://testserver") as client:
        client.portal.call(client.app.state.imghost.runtime_config.update_values, {"allow_registration": False})
        _install_fake_google_provider(
            client,
            FakeGoogleProvider(identity=FakeGoogleIdentity(provider_uid="blocked-google", email="blocked@example.com")),
        )
        started = client.get("/auth/google/start", follow_redirects=False)
        state = _oauth_state_from_redirect(started)
        response = client.get("/auth/google/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        assert response.status_code == 303
        assert "oauth_error=Registration+is+disabled." in response.headers["location"]


def test_google_oauth_unverified_email_is_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    with TestClient(app, base_url="https://testserver") as client:
        _install_fake_google_provider(
            client,
            FakeGoogleProvider(identity=FakeGoogleIdentity(provider_uid="unverified", email="uv@example.com", email_verified=False)),
        )
        started = client.get("/auth/google/start", follow_redirects=False)
        state = _oauth_state_from_redirect(started)
        response = client.get("/auth/google/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        assert response.status_code == 303
        assert "verified+email+address" in response.headers["location"]


def test_google_oauth_existing_link_still_signs_in_when_registration_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    with TestClient(app, base_url="https://testserver") as client:
        user = _create_raw_user(client, username="existingoauth", email="existingoauth@example.com")
        _link_google_account(client, user_id=user.id, provider_uid="existing-google")
        client.portal.call(client.app.state.imghost.runtime_config.update_values, {"allow_registration": False})
        _install_fake_google_provider(
            client,
            FakeGoogleProvider(identity=FakeGoogleIdentity(provider_uid="existing-google", email="existingoauth@example.com")),
        )
        started = client.get("/auth/google/start", follow_redirects=False)
        state = _oauth_state_from_redirect(started)
        response = client.get("/auth/google/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard"


def test_google_oauth_email_collision_requires_local_sign_in_first(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    with TestClient(app, base_url="https://testserver") as client:
        _create_raw_user(client, username="localonly", email="collision@example.com")
        _install_fake_google_provider(
            client,
            FakeGoogleProvider(identity=FakeGoogleIdentity(provider_uid="collision-google", email="collision@example.com")),
        )
        started = client.get("/auth/google/start", follow_redirects=False)
        state = _oauth_state_from_redirect(started)
        response = client.get("/auth/google/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        assert response.status_code == 303
        assert "oauth_error=An+account+with+this+email+already+exists." in response.headers["location"]


def test_google_oauth_normalizes_external_next_path_to_dashboard(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    with TestClient(app, base_url="https://testserver") as client:
        _install_fake_google_provider(
            client,
            FakeGoogleProvider(identity=FakeGoogleIdentity(provider_uid="safe-next", email="safenext@example.com")),
        )
        started = client.get("/auth/google/start", params={"next": "https://evil.example/steal"}, follow_redirects=False)
        state = _oauth_state_from_redirect(started)
        response = client.get("/auth/google/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard"


def test_google_oauth_disconnect_requires_another_login_method(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("SECRET_KEY", "oauth-disconnect-secret")

    with TestClient(app, base_url="https://testserver") as client:
        user = _create_raw_user(client, username="oauthonly", email="oauthonly@example.com")
        _link_google_account(client, user_id=user.id, provider_uid="oauth-only-google")
        _set_browser_session(client, user)
        response = client.post(
            "/api/v1/user/me/oauth/google/disconnect",
            headers=browser_session_headers("https://testserver", "/settings"),
        )
        assert response.status_code == 400
        assert "Set a password before disconnecting Google" in response.json()["detail"]


def test_google_oauth_disconnect_succeeds_when_local_password_exists(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("SECRET_KEY", "oauth-disconnect-secret")

    with TestClient(app, base_url="https://testserver") as client:
        user = _create_raw_user(client, username="hybriduser", email="hybrid@example.com")
        set_user_password(client, user.id, "secret-pass")
        _link_google_account(client, user_id=user.id, provider_uid="hybrid-google")
        _set_browser_session(client, user)
        response = client.post(
            "/api/v1/user/me/oauth/google/disconnect",
            headers=browser_session_headers("https://testserver", "/settings"),
        )
        assert response.status_code == 200
        assert response.json()["disconnected"] is True
        links = client.portal.call(client.app.state.imghost.repository.list_user_sso_links, user.id)
        assert links == []


def test_google_oauth_suspended_linked_user_cannot_sign_in(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    with TestClient(app, base_url="https://testserver") as client:
        user = _create_raw_user(client, username="suspendedoauth", email="suspended@example.com", suspended=True)
        _link_google_account(client, user_id=user.id, provider_uid="suspended-google")
        _install_fake_google_provider(
            client,
            FakeGoogleProvider(identity=FakeGoogleIdentity(provider_uid="suspended-google", email="suspended@example.com")),
        )
        started = client.get("/auth/google/start", follow_redirects=False)
        state = _oauth_state_from_redirect(started)
        response = client.get("/auth/google/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        assert response.status_code == 303
        assert "User+is+not+allowed+to+authenticate." in response.headers["location"]


def test_google_oauth_login_audits_success_and_denial_cases(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    _, admin_key = create_admin_and_api_key(capsys, username="oauthauditadmin", email="oauthauditadmin@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        _install_fake_google_provider(
            client,
            FakeGoogleProvider(identity=FakeGoogleIdentity(provider_uid="audit-google", email="audit@example.com")),
        )
        started = client.get("/auth/google/start", follow_redirects=False)
        state = _oauth_state_from_redirect(started)
        client.get("/auth/google/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        client.get("/auth/google/callback", params={"state": "bad", "code": "abc"}, follow_redirects=False)

        audit = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"action": "oauth.login.success"},
        )
        assert audit.status_code == 200
        payload = audit.json()
        assert payload
        assert payload[0]["metadata"]["provider"] == "google"

        denial = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"action": "oauth.denied"},
        )
        assert denial.status_code == 200
        assert denial.json()
