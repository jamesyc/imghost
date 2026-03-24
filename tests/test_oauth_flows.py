from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import bcrypt
from fastapi import HTTPException
from fastapi.testclient import TestClient

from imghost.main import app
from imghost.models import User, UserSsoLink, utcnow
from imghost.oauth.pkce import build_code_challenge
from imghost.sessions import _decode_signed_token

from .helpers import browser_session_headers, create_admin_and_api_key, set_user_password
from .test_redis_features import FakeRedis


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
        self.last_code_challenge: str | None = None
        self.last_code_challenge_method: str | None = None
        self.last_code_verifier: str | None = None

    def authorization_url(
        self,
        *,
        redirect_uri: str,
        state: str,
        code_challenge: str,
        code_challenge_method: str,
    ) -> str:
        self.last_state = state
        self.last_redirect_uri = redirect_uri
        self.last_code_challenge = code_challenge
        self.last_code_challenge_method = code_challenge_method
        return f"https://fake.google/auth?state={state}"

    async def exchange_code(self, *, code: str, redirect_uri: str, code_verifier: str):
        self.last_code = code
        self.last_redirect_uri = redirect_uri
        self.last_code_verifier = code_verifier
        if self.error is not None:
            raise self.error
        return self.identity


class FakeGenericProvider(FakeGoogleProvider):
    def __init__(self, *, name: str, identity: FakeGoogleIdentity | None = None, error: Exception | None = None) -> None:
        super().__init__(identity=identity, error=error)
        self.name = name


def _create_raw_user(
    client: TestClient,
    *,
    username: str,
    email: str,
    suspended: bool = False,
    password_hash: str | None = None,
) -> User:
    now = utcnow()
    user = User(
        id=str(uuid4()),
        username=username,
        email=email,
        password_hash=password_hash,
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


def _oauth_jti_from_state(client: TestClient, value: str) -> str:
    payload = client.app.state.imghost.oauth_state.loads(value)
    return payload.jti


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
        assert provider.last_code_challenge_method == "S256"
        nonce = client.portal.call(
            client.app.state.imghost.repository.get_oauth_state_nonce,
            _oauth_jti_from_state(client, provider.last_state),
        )
        assert nonce is not None
        assert nonce.code_verifier
        assert provider.last_code_challenge == build_code_challenge(nonce.code_verifier)


def test_google_oauth_start_uses_trusted_forwarded_public_origin_for_callback_url(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://fallback.example.com")
    monkeypatch.setenv("TRUSTED_PUBLIC_ORIGINS", "https://imghost.public.example")
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS_ENABLED", "true")
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "127.0.0.1/32")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    with TestClient(app, base_url="http://backend", client=("127.0.0.1", 50000)) as client:
        provider = _install_fake_google_provider(client, FakeGoogleProvider())
        response = client.get(
            "/auth/google/start",
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "imghost.public.example"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert provider.last_redirect_uri == "https://imghost.public.example/auth/google/callback"


def test_google_oauth_start_falls_back_to_base_url_for_untrusted_forwarded_public_origin(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://fallback.example.com")
    monkeypatch.setenv("TRUSTED_PUBLIC_ORIGINS", "https://trusted.example")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    with TestClient(app, base_url="http://backend") as client:
        provider = _install_fake_google_provider(client, FakeGoogleProvider())
        response = client.get(
            "/auth/google/start",
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "evil.example"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert provider.last_redirect_uri == "https://fallback.example.com/auth/google/callback"


def test_google_oauth_start_falls_back_to_base_url_for_malformed_forwarded_public_origin(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://fallback.example.com")
    monkeypatch.setenv("TRUSTED_PUBLIC_ORIGINS", "https://trusted.example")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    with TestClient(app, base_url="http://backend") as client:
        provider = _install_fake_google_provider(client, FakeGoogleProvider())
        response = client.get(
            "/auth/google/start",
            headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "bad/path.example/evil"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert provider.last_redirect_uri == "https://fallback.example.com/auth/google/callback"


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
        nonce = client.portal.call(client.app.state.imghost.repository.get_oauth_state_nonce, _oauth_jti_from_state(client, state))
        assert nonce is not None
        response = client.get("/auth/google/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/albums"
        assert "imghost_session=" in response.headers["set-cookie"]
        assert provider.last_code == "abc"
        assert provider.last_code_verifier == nonce.code_verifier
        assert client.portal.call(client.app.state.imghost.repository.get_oauth_state_nonce, _oauth_jti_from_state(client, state)) is None


def test_google_oauth_link_start_uses_pkce_and_stores_code_verifier(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("SECRET_KEY", "oauth-link-secret")

    with TestClient(app, base_url="https://testserver") as client:
        user = _create_raw_user(client, username="pkcelink", email="pkcelink@example.com")
        _set_browser_session(client, user)
        provider = _install_fake_google_provider(client, FakeGoogleProvider())
        started = client.get("/auth/google/start", params={"mode": "link", "next": "/settings"}, follow_redirects=False)
        assert started.status_code == 303
        assert provider.last_code_challenge_method == "S256"
        nonce = client.portal.call(
            client.app.state.imghost.repository.get_oauth_state_nonce,
            _oauth_jti_from_state(client, provider.last_state),
        )
        assert nonce is not None
        assert nonce.code_verifier
        assert provider.last_code_challenge == build_code_challenge(nonce.code_verifier)


def test_google_oauth_login_state_is_single_use(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    with TestClient(app, base_url="https://testserver") as client:
        _install_fake_google_provider(
            client,
            FakeGoogleProvider(identity=FakeGoogleIdentity(provider_uid="replay-google", email="replay@example.com")),
        )
        started = client.get("/auth/google/start", params={"next": "/albums"}, follow_redirects=False)
        state = _oauth_state_from_redirect(started)
        first = client.get("/auth/google/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        assert first.status_code == 303
        assert first.headers["location"] == "/albums"
        second = client.get("/auth/google/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        assert second.status_code == 303
        assert second.headers["location"].startswith("/login?")
        assert "oauth_error=Google+sign-in+could+not+be+verified." in second.headers["location"]


def test_google_oauth_signed_in_user_can_link_account(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("SECRET_KEY", "oauth-link-secret")

    with TestClient(app, base_url="https://testserver") as client:
        user = _create_raw_user(client, username="localuser", email="local@example.com")
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


def test_google_oauth_link_state_is_single_use(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("SECRET_KEY", "oauth-link-secret")

    with TestClient(app, base_url="https://testserver") as client:
        user = _create_raw_user(client, username="relinkuser", email="relink@example.com")
        _set_browser_session(client, user)
        _install_fake_google_provider(
            client,
            FakeGoogleProvider(identity=FakeGoogleIdentity(provider_uid="link-replay-google", email="linkreplay@example.com")),
        )
        started = client.get("/auth/google/start", params={"mode": "link", "next": "/settings"}, follow_redirects=False)
        state = _oauth_state_from_redirect(started)
        first = client.get("/auth/google/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        assert first.status_code == 303
        assert "oauth_status=Google+account+connected." in first.headers["location"]
        second = client.get("/auth/google/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        assert second.status_code == 303
        assert second.headers["location"].startswith("/login?")
        assert "oauth_error=Google+sign-in+could+not+be+verified." in second.headers["location"]


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


def test_google_oauth_callback_rejects_missing_nonce(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    with TestClient(app, base_url="https://testserver") as client:
        _install_fake_google_provider(
            client,
            FakeGoogleProvider(identity=FakeGoogleIdentity(provider_uid="missing-nonce-google", email="missingnonce@example.com")),
        )
        started = client.get("/auth/google/start", follow_redirects=False)
        state = _oauth_state_from_redirect(started)
        jti = _oauth_jti_from_state(client, state)
        client.portal.call(client.app.state.imghost.repository.consume_oauth_state_nonce, jti)
        response = client.get("/auth/google/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login?")
        assert "oauth_error=Google+sign-in+could+not+be+verified." in response.headers["location"]


def test_google_oauth_callback_rejects_expired_nonce(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    with TestClient(app, base_url="https://testserver") as client:
        _install_fake_google_provider(
            client,
            FakeGoogleProvider(identity=FakeGoogleIdentity(provider_uid="expired-nonce-google", email="expirednonce@example.com")),
        )
        started = client.get("/auth/google/start", follow_redirects=False)
        state = _oauth_state_from_redirect(started)
        jti = _oauth_jti_from_state(client, state)
        nonce = client.portal.call(client.app.state.imghost.repository.get_oauth_state_nonce, jti)
        assert nonce is not None
        pool = client.app.state.imghost.database.require_pool()

        async def expire_nonce() -> None:
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE oauth_state_nonces SET expires_at = $2 WHERE jti = $1",
                    jti,
                    utcnow() - timedelta(seconds=1),
                )

        client.portal.call(expire_nonce)
        response = client.get("/auth/google/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login?")
        assert "oauth_error=Google+sign-in+could+not+be+verified." in response.headers["location"]


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


def test_google_oauth_empty_provider_uid_is_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    with TestClient(app, base_url="https://testserver") as client:
        _install_fake_google_provider(
            client,
            FakeGoogleProvider(identity=FakeGoogleIdentity(provider_uid="", email="emptyuid@example.com")),
        )
        started = client.get("/auth/google/start", follow_redirects=False)
        state = _oauth_state_from_redirect(started)
        response = client.get("/auth/google/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        assert response.status_code == 303
        assert "oauth_error=Google+sign-in+could+not+be+verified." in response.headers["location"]


def test_google_oauth_empty_email_is_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    with TestClient(app, base_url="https://testserver") as client:
        _install_fake_google_provider(
            client,
            FakeGoogleProvider(identity=FakeGoogleIdentity(provider_uid="empty-email", email="")),
        )
        started = client.get("/auth/google/start", follow_redirects=False)
        state = _oauth_state_from_redirect(started)
        response = client.get("/auth/google/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        assert response.status_code == 303
        assert "oauth_error=Google+sign-in+could+not+be+verified." in response.headers["location"]


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
        user = _create_raw_user(
            client,
            username="hybriduser",
            email="hybrid@example.com",
            password_hash=bcrypt.hashpw(b"secret-pass", bcrypt.gensalt()).decode("utf-8"),
        )
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


def test_google_oauth_delete_account_mode_issues_reauth_token_for_account_deletion(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("SECRET_KEY", "oauth-delete-secret")

    with TestClient(app, base_url="https://testserver") as client:
        user = _create_raw_user(client, username="oauthdelete", email="oauthdelete@example.com")
        _link_google_account(client, user_id=user.id, provider_uid="oauth-delete-google")
        _set_browser_session(client, user)
        _install_fake_google_provider(
            client,
            FakeGoogleProvider(identity=FakeGoogleIdentity(provider_uid="oauth-delete-google", email="oauthdelete@example.com")),
        )

        started = client.get("/auth/google/start", params={"mode": "delete_account"}, follow_redirects=False)
        assert started.status_code == 303
        state = _oauth_state_from_redirect(started)

        callback = client.get("/auth/google/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        assert callback.status_code == 303
        query = parse_qs(urlparse(callback.headers["location"]).query)
        assert query["delete_reauth_tone"] == ["success"]
        assert "delete_reauth_token" in query
        reauth_token = query["delete_reauth_token"][0]

        deleted = client.request(
            "DELETE",
            "/api/v1/user/me",
            headers=browser_session_headers("https://testserver", "/settings"),
            json={"method": "oauth_reauth", "reauth_token": reauth_token},
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
        assert client.portal.call(client.app.state.imghost.repository.get_user, user.id) is None


def test_generic_provider_delete_account_mode_uses_provider_agnostic_routes(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "oauth-delete-secret")

    with TestClient(app, base_url="https://testserver") as client:
        user = _create_raw_user(client, username="githubdelete", email="githubdelete@example.com")
        client.portal.call(
            client.app.state.imghost.repository.create_user_sso_link,
            UserSsoLink(
                id=str(uuid4()),
                user_id=user.id,
                provider="github",
                provider_uid="github-user-1",
                linked_at=datetime.now(UTC),
            ),
        )
        _set_browser_session(client, user)
        provider = FakeGenericProvider(
            name="github",
            identity=FakeGoogleIdentity(provider="github", provider_uid="github-user-1", email="githubdelete@example.com"),
        )
        client.app.state.imghost.oauth_providers["github"] = provider

        started = client.get("/auth/github/start", params={"mode": "delete_account"}, follow_redirects=False)
        assert started.status_code == 303
        assert started.headers["location"].startswith("https://fake.google/auth?")
        assert provider.last_redirect_uri == "https://testserver/auth/github/callback"
        state = _oauth_state_from_redirect(started)

        callback = client.get("/auth/github/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        assert callback.status_code == 303
        query = parse_qs(urlparse(callback.headers["location"]).query)
        assert query["delete_reauth_tone"] == ["success"]
        assert "GitHub re-authentication confirmed." in query["delete_reauth_status"][0]


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


def test_google_oauth_login_works_without_redis_configured(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("REDIS_MODE", "disabled")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")

    with TestClient(app, base_url="https://testserver") as client:
        _install_fake_google_provider(
            client,
            FakeGoogleProvider(identity=FakeGoogleIdentity(provider_uid="noredis-google", email="noredis@example.com")),
        )
        started = client.get("/auth/google/start", follow_redirects=False)
        state = _oauth_state_from_redirect(started)
        response = client.get("/auth/google/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        assert response.status_code == 303
        token = response.cookies.get(client.app.state.imghost.settings.session_cookie_name)
        assert token is not None
        payload = _decode_signed_token(client.app.state.imghost.settings, token)
        assert payload is not None
        assert payload.store == "cookie"
        me = client.get("/api/v1/user/me")
        assert me.status_code == 200


def test_google_oauth_login_uses_redis_session_when_redis_is_available(tmp_path, monkeypatch) -> None:
    fake = FakeRedis()
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setenv("REDIS_MODE", "auto")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr("imghost.redis_support.redis_async", SimpleNamespace(from_url=lambda *args, **kwargs: fake))

    with TestClient(app, base_url="https://testserver") as client:
        _install_fake_google_provider(
            client,
            FakeGoogleProvider(identity=FakeGoogleIdentity(provider_uid="redis-google", email="redis@example.com")),
        )
        started = client.get("/auth/google/start", follow_redirects=False)
        state = _oauth_state_from_redirect(started)
        response = client.get("/auth/google/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        assert response.status_code == 303
        token = response.cookies.get(client.app.state.imghost.settings.session_cookie_name)
        assert token is not None
        payload = _decode_signed_token(client.app.state.imghost.settings, token)
        assert payload is not None
        assert payload.store == "redis"
        assert any(":session:" in key for key in fake.values)
        me = client.get("/api/v1/user/me")
        assert me.status_code == 200


def test_google_oauth_login_falls_back_to_cookie_when_redis_is_down_and_sessions_fail_open(tmp_path, monkeypatch) -> None:
    fake = FakeRedis()
    fake.fail = True
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setenv("REDIS_MODE", "auto")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr("imghost.redis_support.redis_async", SimpleNamespace(from_url=lambda *args, **kwargs: fake))

    with TestClient(app, base_url="https://testserver") as client:
        _install_fake_google_provider(
            client,
            FakeGoogleProvider(identity=FakeGoogleIdentity(provider_uid="fallback-google", email="fallbackoauth@example.com")),
        )
        started = client.get("/auth/google/start", follow_redirects=False)
        state = _oauth_state_from_redirect(started)
        response = client.get("/auth/google/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        assert response.status_code == 303
        token = response.cookies.get(client.app.state.imghost.settings.session_cookie_name)
        assert token is not None
        payload = _decode_signed_token(client.app.state.imghost.settings, token)
        assert payload is not None
        assert payload.store == "cookie"
        me = client.get("/api/v1/user/me")
        assert me.status_code == 200
        assert me.json()["email"] == "fallbackoauth@example.com"


def test_google_oauth_login_redirects_back_to_login_when_redis_sessions_fail_closed(tmp_path, monkeypatch) -> None:
    fake = FakeRedis()
    fake.fail = True
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setenv("REDIS_MODE", "auto")
    monkeypatch.setenv("SESSION_REDIS_FAIL_CLOSED", "true")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr("imghost.redis_support.redis_async", SimpleNamespace(from_url=lambda *args, **kwargs: fake))

    with TestClient(app, base_url="https://testserver") as client:
        _install_fake_google_provider(
            client,
            FakeGoogleProvider(identity=FakeGoogleIdentity(provider_uid="strict-google", email="strictoauth@example.com")),
        )
        started = client.get("/auth/google/start", follow_redirects=False)
        state = _oauth_state_from_redirect(started)
        response = client.get("/auth/google/callback", params={"state": state, "code": "abc"}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login?")
        assert "oauth_error=Google+sign-in+is+temporarily+unavailable." in response.headers["location"]


def test_google_oauth_link_start_requires_fresh_session_when_redis_sessions_fail_closed(tmp_path, monkeypatch) -> None:
    fake = FakeRedis()
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setenv("REDIS_MODE", "auto")
    monkeypatch.setenv("SESSION_REDIS_FAIL_CLOSED", "true")
    monkeypatch.setenv("GOOGLE_OAUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr("imghost.redis_support.redis_async", SimpleNamespace(from_url=lambda *args, **kwargs: fake))

    with TestClient(app, base_url="https://testserver") as client:
        user = _create_raw_user(client, username="strictlink", email="strictlink@example.com")
        token, _ = client.portal.call(lambda: client.app.state.imghost.session_backend.create_session(user, remember_me=True))
        client.cookies.set(client.app.state.imghost.settings.session_cookie_name, token)
        fake.fail = True
        response = client.get("/auth/google/start", params={"mode": "link"}, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login?next=%2Fsettings"
