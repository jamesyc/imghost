from __future__ import annotations

import base64
import hmac
import json
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from hashlib import sha256

from .models import ShareXDeleteCapability, utcnow

SHAREX_DELETE_CAPABILITY_DAYS = 90
SHAREX_DELETE_CONFIRM_SECONDS = 300
SHAREX_DELETE_CONSUMED_RETENTION_DAYS = 30
SHAREX_DELETE_REVOKED_RETENTION_DAYS = 30
SHAREX_DELETE_CONFIRM_COOKIE_NAME = "imghost_sharex_delete_confirm"
SHAREX_DELETE_CAPABILITY_PURPOSE = "sharex_delete_album"


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(f"{data}{padding}")


@dataclass(slots=True)
class ShareXDeleteConfirmationPayload:
    selector: str
    album_id: str
    user_id: str
    created_at: str | None = None


class ShareXDeleteTokenManager:
    def __init__(self, secret_key: str) -> None:
        self.secret_key = secret_key

    def issue_capability(self, album_id: str, user_id: str) -> tuple[ShareXDeleteCapability, str]:
        now = utcnow()
        selector = secrets.token_urlsafe(12)
        secret = secrets.token_urlsafe(24)
        capability = ShareXDeleteCapability(
            selector=selector,
            purpose=SHAREX_DELETE_CAPABILITY_PURPOSE,
            album_id=album_id,
            user_id=user_id,
            secret_hash=sha256(secret.encode("utf-8")).hexdigest(),
            created_at=now,
            expires_at=now + timedelta(days=SHAREX_DELETE_CAPABILITY_DAYS),
            consumed_at=None,
            revoked_at=None,
            last_seen_at=None,
        )
        return capability, f"{selector}.{secret}"

    def split_capability_token(self, value: str) -> tuple[str, str]:
        selector, dot, secret = value.partition(".")
        if not dot or not selector or not secret:
            raise ValueError("invalid_sharex_delete_token")
        return selector.strip(), secret.strip()

    def verify_capability_secret(self, capability: ShareXDeleteCapability, secret: str) -> bool:
        expected = capability.secret_hash
        actual = sha256(secret.encode("utf-8")).hexdigest()
        return hmac.compare_digest(actual, expected)

    def dumps_confirmation(self, payload: ShareXDeleteConfirmationPayload) -> str:
        values = asdict(payload)
        values["created_at"] = values["created_at"] or utcnow().isoformat()
        payload_bytes = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(self.secret_key.encode("utf-8"), payload_bytes, sha256).hexdigest()
        return f"{_b64encode(payload_bytes)}.{signature}"

    def loads_confirmation(
        self,
        value: str,
        *,
        max_age: int = SHAREX_DELETE_CONFIRM_SECONDS,
    ) -> ShareXDeleteConfirmationPayload:
        payload_b64, dot, signature = value.partition(".")
        if not dot or not payload_b64 or not signature:
            raise ValueError("invalid_sharex_delete_confirmation")
        try:
            payload_bytes = _b64decode(payload_b64)
        except Exception as exc:
            raise ValueError("invalid_sharex_delete_confirmation") from exc
        expected = hmac.new(self.secret_key.encode("utf-8"), payload_bytes, sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid_sharex_delete_confirmation")
        try:
            data = json.loads(payload_bytes.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("invalid_sharex_delete_confirmation") from exc
        created_at = str(data.get("created_at") or "").strip()
        selector = str(data.get("selector") or "").strip()
        album_id = str(data.get("album_id") or "").strip()
        user_id = str(data.get("user_id") or "").strip()
        if not created_at or not selector or not album_id or not user_id:
            raise ValueError("invalid_sharex_delete_confirmation")
        try:
            age = (utcnow() - datetime.fromisoformat(created_at)).total_seconds()
        except ValueError as exc:
            raise ValueError("invalid_sharex_delete_confirmation") from exc
        if age < 0 or age > max_age:
            raise ValueError("invalid_sharex_delete_confirmation")
        return ShareXDeleteConfirmationPayload(
            selector=selector,
            album_id=album_id,
            user_id=user_id,
            created_at=created_at,
        )
