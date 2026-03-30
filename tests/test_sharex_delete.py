from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from imghost.sharex_delete import (
    SHAREX_DELETE_CAPABILITY_DAYS,
    SHAREX_DELETE_CONFIRM_SECONDS,
    ShareXDeleteConfirmationPayload,
    ShareXDeleteTokenManager,
)


def test_issue_capability_returns_selector_and_hashed_secret() -> None:
    manager = ShareXDeleteTokenManager("test-secret")

    capability, raw_token = manager.issue_capability("album12345", "user-123")
    selector, secret = manager.split_capability_token(raw_token)

    assert selector == capability.selector
    assert capability.album_id == "album12345"
    assert capability.user_id == "user-123"
    assert capability.secret_hash == sha256(secret.encode("utf-8")).hexdigest()
    assert manager.verify_capability_secret(capability, secret) is True
    assert manager.verify_capability_secret(capability, f"{secret}x") is False
    assert capability.expires_at - capability.created_at == timedelta(days=90)


def test_issue_capability_uses_ninety_day_ttl() -> None:
    manager = ShareXDeleteTokenManager("test-secret")

    capability, _ = manager.issue_capability("album12345", "user-123")

    assert SHAREX_DELETE_CAPABILITY_DAYS == 90
    assert capability.expires_at - capability.created_at == timedelta(days=90)


def test_confirmation_round_trip() -> None:
    manager = ShareXDeleteTokenManager("test-secret")

    token = manager.dumps_confirmation(
        ShareXDeleteConfirmationPayload(
            selector="selector-123",
            album_id="album12345",
            user_id="user-123",
        )
    )

    payload = manager.loads_confirmation(token)
    assert payload.selector == "selector-123"
    assert payload.album_id == "album12345"
    assert payload.user_id == "user-123"
    assert payload.created_at is not None


def test_confirmation_rejects_expired_payload(monkeypatch) -> None:
    fixed_now = datetime(2026, 3, 28, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("imghost.sharex_delete.utcnow", lambda: fixed_now)
    manager = ShareXDeleteTokenManager("test-secret")

    token = manager.dumps_confirmation(
        ShareXDeleteConfirmationPayload(
            selector="selector-123",
            album_id="album12345",
            user_id="user-123",
        )
    )

    monkeypatch.setattr("imghost.sharex_delete.utcnow", lambda: fixed_now + timedelta(seconds=301))
    with pytest.raises(ValueError, match="invalid_sharex_delete_confirmation"):
        manager.loads_confirmation(token)


def test_confirmation_accepts_payload_exactly_at_max_age_boundary(monkeypatch) -> None:
    fixed_now = datetime(2026, 3, 28, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("imghost.sharex_delete.utcnow", lambda: fixed_now)
    manager = ShareXDeleteTokenManager("test-secret")

    token = manager.dumps_confirmation(
        ShareXDeleteConfirmationPayload(
            selector="selector-123",
            album_id="album12345",
            user_id="user-123",
        )
    )

    monkeypatch.setattr("imghost.sharex_delete.utcnow", lambda: fixed_now + timedelta(seconds=SHAREX_DELETE_CONFIRM_SECONDS))
    payload = manager.loads_confirmation(token)
    assert payload.selector == "selector-123"


def test_confirmation_rejects_payload_past_max_age_boundary(monkeypatch) -> None:
    fixed_now = datetime(2026, 3, 28, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("imghost.sharex_delete.utcnow", lambda: fixed_now)
    manager = ShareXDeleteTokenManager("test-secret")

    token = manager.dumps_confirmation(
        ShareXDeleteConfirmationPayload(
            selector="selector-123",
            album_id="album12345",
            user_id="user-123",
        )
    )

    monkeypatch.setattr("imghost.sharex_delete.utcnow", lambda: fixed_now + timedelta(seconds=SHAREX_DELETE_CONFIRM_SECONDS + 1))
    with pytest.raises(ValueError, match="invalid_sharex_delete_confirmation"):
        manager.loads_confirmation(token)


def test_confirmation_rejects_tampered_cookie_payload() -> None:
    manager = ShareXDeleteTokenManager("test-secret")

    token = manager.dumps_confirmation(
        ShareXDeleteConfirmationPayload(
            selector="selector-123",
            album_id="album12345",
            user_id="user-123",
        )
    )
    tampered = f"{token[:-1]}{'0' if token[-1] != '0' else '1'}"

    with pytest.raises(ValueError, match="invalid_sharex_delete_confirmation"):
        manager.loads_confirmation(tampered)
