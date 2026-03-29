from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from imghost.sharex_delete import ShareXDeleteConfirmationPayload, ShareXDeleteTokenManager


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
