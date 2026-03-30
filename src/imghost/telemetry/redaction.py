from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import TelemetryEvent

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
    "imghost_delete_reauth",
    "token",
    "delete_token",
    "reauth_token",
    "delete_reauth_token",
    "delete_url",
    "manage_url",
}
_URL_KEYS = {"referer", "url", "delete_url", "manage_url"}
_SECRET_QUERY_KEYS = {"token", "delete_token", "reauth_token", "delete_reauth_token"}


def redact_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return _redact_value(dict(metadata))


def redact_record(record: TelemetryEvent) -> TelemetryEvent:
    redacted = deepcopy(record)
    redacted.metadata = redact_metadata(redacted.metadata)
    if redacted.request is not None:
        request_payload = _redact_value(redacted.request.to_dict())
        redacted.request.origin = request_payload.get("origin")
        redacted.request.referer = request_payload.get("referer")
        redacted.request.host = request_payload.get("host")
        redacted.request.user_agent = request_payload.get("user_agent")
        redacted.request.forwarded_for = request_payload.get("forwarded_for")
    return redacted


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if key.lower() in _SECRET_KEYS:
                redacted[key] = _REDACTED
            elif key.lower() in _URL_KEYS and isinstance(item, str):
                redacted[key] = _sanitize_url(item)
            else:
                redacted[key] = _redact_value(item)
        return redacted
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    return value


def _sanitize_url(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        return value
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return value
    if not parsed.query:
        return value
    changed = False
    query_items: list[tuple[str, str]] = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in _SECRET_QUERY_KEYS:
            query_items.append((key, _REDACTED))
            changed = True
        else:
            query_items.append((key, item))
    if not changed:
        return value
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query_items, doseq=True), parsed.fragment))
