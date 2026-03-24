from __future__ import annotations

import base64
import hmac
import json
from dataclasses import asdict, dataclass
from hashlib import sha256


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(f"{data}{padding}")


@dataclass(slots=True)
class ShareXDeletePayload:
    album_id: str
    user_id: str


class ShareXDeleteTokenManager:
    def __init__(self, secret_key: str) -> None:
        self.secret_key = secret_key

    def dumps(self, payload: ShareXDeletePayload) -> str:
        payload_bytes = json.dumps(asdict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(self.secret_key.encode("utf-8"), payload_bytes, sha256).hexdigest()
        return f"{_b64encode(payload_bytes)}.{signature}"

    def loads(self, value: str) -> ShareXDeletePayload:
        payload_b64, dot, signature = value.partition(".")
        if not dot or not payload_b64 or not signature:
            raise ValueError("invalid_sharex_delete_token")
        try:
            payload_bytes = _b64decode(payload_b64)
        except Exception as exc:
            raise ValueError("invalid_sharex_delete_token") from exc
        expected = hmac.new(self.secret_key.encode("utf-8"), payload_bytes, sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid_sharex_delete_token")
        try:
            data = json.loads(payload_bytes.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("invalid_sharex_delete_token") from exc
        album_id = str(data.get("album_id") or "").strip()
        user_id = str(data.get("user_id") or "").strip()
        if not album_id or not user_id:
            raise ValueError("invalid_sharex_delete_token")
        return ShareXDeletePayload(album_id=album_id, user_id=user_id)
