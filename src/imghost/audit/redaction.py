from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .models import AuditRecord

_REDACTED = "[REDACTED]"
_SECRET_KEYS = {
    "password",
    "current_password",
    "new_password",
    "api_key",
    "raw_api_key",
    "authorization",
    "cookie",
    "set-cookie",
    "session_token",
    "token",
}


def redact_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return _redact_value(dict(metadata))


def redact_record(record: AuditRecord) -> AuditRecord:
    redacted = deepcopy(record)
    redacted.metadata = redact_metadata(redacted.metadata)
    return redacted


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if key.lower() in _SECRET_KEYS:
                redacted[key] = _REDACTED
            else:
                redacted[key] = _redact_value(item)
        return redacted
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    return value
