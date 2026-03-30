from __future__ import annotations

import base64
import hmac
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256

from .models import utcnow


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(f"{data}{padding}")


@dataclass(slots=True)
class AccountDeleteReauthPayload:
    user_id: str
    provider: str
    provider_uid: str
    created_at: str | None = None


class AccountDeleteReauthTokenManager:
    def __init__(self, secret_key: str) -> None:
        self.secret_key = secret_key

    def dumps(self, payload: AccountDeleteReauthPayload) -> str:
        values = asdict(payload)
        values["created_at"] = values["created_at"] or utcnow().isoformat()
        payload_bytes = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(self.secret_key.encode("utf-8"), payload_bytes, sha256).hexdigest()
        return f"{_b64encode(payload_bytes)}.{signature}"

    def loads(self, value: str, *, max_age: int = 600) -> AccountDeleteReauthPayload:
        payload_b64, dot, signature = value.partition(".")
        if not dot or not payload_b64 or not signature:
            raise ValueError("invalid_account_delete_reauth_token")
        try:
            payload_bytes = _b64decode(payload_b64)
        except Exception as exc:
            raise ValueError("invalid_account_delete_reauth_token") from exc
        expected = hmac.new(self.secret_key.encode("utf-8"), payload_bytes, sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid_account_delete_reauth_token")
        try:
            data = json.loads(payload_bytes.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("invalid_account_delete_reauth_token") from exc
        if not isinstance(data, dict):
            raise ValueError("invalid_account_delete_reauth_token")
        created_at = str(data.get("created_at") or "").strip()
        user_id = str(data.get("user_id") or "").strip()
        provider = str(data.get("provider") or "").strip().lower()
        provider_uid = str(data.get("provider_uid") or "").strip()
        if not created_at or not user_id or not provider or not provider_uid:
            raise ValueError("invalid_account_delete_reauth_token")
        try:
            age = (utcnow() - datetime.fromisoformat(created_at)).total_seconds()
        except ValueError as exc:
            raise ValueError("invalid_account_delete_reauth_token") from exc
        if age < 0 or age > max_age:
            raise ValueError("invalid_account_delete_reauth_token")
        return AccountDeleteReauthPayload(
            user_id=user_id,
            provider=provider,
            provider_uid=provider_uid,
            created_at=created_at,
        )
