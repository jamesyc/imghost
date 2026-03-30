from __future__ import annotations

import base64
import hmac
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256

from ..models import utcnow


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(f"{data}{padding}")


@dataclass(slots=True)
class OAuthStatePayload:
    mode: str
    next_path: str
    jti: str
    user_id: str | None = None
    created_at: str | None = None


class OAuthStateManager:
    def __init__(self, secret_key: str) -> None:
        self.secret_key = secret_key

    def dumps(self, payload: OAuthStatePayload) -> str:
        values = asdict(payload)
        values["created_at"] = values["created_at"] or utcnow().isoformat()
        payload_bytes = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(self.secret_key.encode("utf-8"), payload_bytes, sha256).hexdigest()
        return f"{_b64encode(payload_bytes)}.{signature}"

    def loads(self, value: str, *, max_age: int = 600) -> OAuthStatePayload:
        payload_b64, dot, signature = value.partition(".")
        if not dot or not payload_b64 or not signature:
            raise ValueError("invalid_oauth_state")
        try:
            payload_bytes = _b64decode(payload_b64)
        except Exception as exc:
            raise ValueError("invalid_oauth_state") from exc
        expected = hmac.new(self.secret_key.encode("utf-8"), payload_bytes, sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid_oauth_state")
        try:
            data = json.loads(payload_bytes.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("invalid_oauth_state") from exc
        if not isinstance(data, dict):
            raise ValueError("invalid_oauth_state")
        created_at = str(data.get("created_at") or "").strip()
        if not created_at:
            raise ValueError("invalid_oauth_state")
        try:
            age = (utcnow() - datetime.fromisoformat(created_at)).total_seconds()
        except ValueError as exc:
            raise ValueError("invalid_oauth_state") from exc
        if age < 0 or age > max_age:
            raise ValueError("invalid_oauth_state")
        return OAuthStatePayload(
            mode=str(data.get("mode") or "").strip(),
            next_path=str(data.get("next_path") or "").strip(),
            jti=str(data.get("jti") or "").strip(),
            user_id=(str(data.get("user_id")).strip() if data.get("user_id") else None),
            created_at=created_at,
        )
