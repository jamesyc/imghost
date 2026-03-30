import base64
import hmac
import json
from types import SimpleNamespace
from hashlib import sha256

import pytest

from imghost.account_delete_reauth import AccountDeleteReauthTokenManager
from imghost.oauth.state import OAuthStateManager
from imghost.sessions import _decode_signed_token
from imghost.sharex_delete import ShareXDeleteTokenManager


def _signed_value(secret_key: str, payload: object) -> str:
    payload_bytes = json.dumps(payload).encode("utf-8")
    signature = hmac.new(secret_key.encode("utf-8"), payload_bytes, sha256).hexdigest()
    encoded = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode("ascii")
    return f"{encoded}.{signature}"


@pytest.mark.parametrize("payload", [[], "hello", 1, True, None])
def test_decode_signed_session_token_rejects_non_object_json(payload: object) -> None:
    settings = SimpleNamespace(secret_key="test-secret")

    assert _decode_signed_token(settings, _signed_value("test-secret", payload)) is None


@pytest.mark.parametrize("payload", [[], "hello", 1, True, None])
def test_oauth_state_rejects_non_object_json(payload: object) -> None:
    manager = OAuthStateManager("test-secret")

    with pytest.raises(ValueError, match="invalid_oauth_state"):
        manager.loads(_signed_value("test-secret", payload))


@pytest.mark.parametrize("payload", [[], "hello", 1, True, None])
def test_sharex_confirmation_rejects_non_object_json(payload: object) -> None:
    manager = ShareXDeleteTokenManager("test-secret")

    with pytest.raises(ValueError, match="invalid_sharex_delete_confirmation"):
        manager.loads_confirmation(_signed_value("test-secret", payload))


@pytest.mark.parametrize("payload", [[], "hello", 1, True, None])
def test_account_delete_reauth_rejects_non_object_json(payload: object) -> None:
    manager = AccountDeleteReauthTokenManager("test-secret")

    with pytest.raises(ValueError, match="invalid_account_delete_reauth_token"):
        manager.loads(_signed_value("test-secret", payload))
