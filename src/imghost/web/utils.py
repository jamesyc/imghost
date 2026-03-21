from __future__ import annotations

from datetime import UTC, datetime
from math import ceil
from typing import Any
from urllib.parse import urlencode

from fastapi import HTTPException, Request

from ..ids import MEDIA_ID_LENGTH, is_valid_id
from ..models import User
from ..payloads import thumb_format
from ..rate_limits import hash_anon_identity


def client_ip(request: Request) -> str:
    for header_name in ("CF-Connecting-IP", "X-Real-IP", "X-Forwarded-For"):
        value = request.headers.get(header_name)
        if not value:
            continue
        if header_name == "X-Forwarded-For":
            return value.split(",", 1)[0].strip()
        return value.strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def upload_rate_limit_key(request: Request, user: User | None) -> str:
    if user is not None:
        return user.id
    return hash_anon_identity(client_ip(request), request.headers.get("User-Agent", ""))


def extract_media_id(raw_id: str) -> str:
    return raw_id.rsplit(".", 1)[0].lower()


def validate_media_id_or_404(raw_id: str) -> str:
    media_id = extract_media_id(raw_id)
    if not is_valid_id(media_id, MEDIA_ID_LENGTH):
        raise HTTPException(status_code=404)
    return media_id


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


def thumb_media_type(item: Any) -> str:
    fmt = thumb_format(item)
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "png": "image/png",
        "gif": "image/gif",
        "bmp": "image/bmp",
    }.get(fmt, item.mime_type)


def album_manage_url(base_url: str, album: Any, *, include_token: bool = False) -> str:
    path = f"{base_url}/manage/{album.id}"
    if not album.delete_token or not include_token:
        return path
    query = urlencode({"token": album.delete_token})
    return f"{path}?{query}"
