from __future__ import annotations

from datetime import UTC, datetime
from math import ceil
from typing import Any
from urllib.parse import urlencode


def is_expired(expires_at: datetime | None) -> bool:
    return expires_at is not None and expires_at <= datetime.now(UTC)


def humanize_expiry(expires_at: datetime | None) -> str | None:
    if expires_at is None:
        return None
    delta = expires_at - datetime.now(UTC)
    total_seconds = max(0, int(delta.total_seconds()))
    if total_seconds < 3600:
        minutes = max(1, ceil(total_seconds / 60))
        return f"This album expires in {minutes} minute(s)."
    if total_seconds < 86400:
        hours = ceil(total_seconds / 3600)
        return f"This album expires in {hours} hour(s)."
    days = ceil(total_seconds / 86400)
    return f"This album expires in {days} day(s)."


def humanize_bytes(byte_count: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(byte_count)
    unit = units[0]
    for candidate in units:
        unit = candidate
        if size < 1024.0 or candidate == units[-1]:
            break
        size /= 1024.0
    if unit == "B":
        return f"{int(size)} {unit}"
    return f"{size:.1f} {unit}"


def display_timestamp(value: str) -> str:
    return datetime.fromisoformat(value).strftime("%-m/%-d/%Y, %-I:%M:%S %p")


def album_manage_url(base_url: str, album: Any, *, include_token: bool = False) -> str:
    path = f"{base_url}/manage/{album.id}"
    if not album.delete_token or not include_token:
        return path
    query = urlencode({"token": album.delete_token})
    return f"{path}?{query}"
