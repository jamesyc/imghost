from __future__ import annotations

from typing import Any


COMPAT_WARNING_MESSAGES = {
    "hevc": "This video uses HEVC encoding and may not play in Firefox. Try Chrome or Safari.",
    "vp9_webm": "This video may not play in older Safari. Try Chrome or Firefox.",
}


def media_url(base_url: str, media_id: str, fmt: str) -> str:
    normalized = "jpg" if fmt == "jpeg" else fmt
    ext = f".{normalized}" if normalized else ""
    return f"{base_url}/i/{media_id}{ext}"


def thumb_url(base_url: str, media_id: str, fmt: str) -> str:
    normalized = "jpg" if fmt == "jpeg" else fmt
    ext = f".{normalized}" if normalized else ""
    return f"{base_url}/t/{media_id}{ext}"


def thumb_format(item: Any) -> str:
    if item.thumb_is_orig or not item.thumb_key:
        return item.format
    suffix = item.thumb_key.rsplit(".", 1)[-1].lower()
    return suffix


def compatibility_warning_key(item: Any) -> str | None:
    if item.codec_hint == "hevc":
        return "hevc"
    if item.codec_hint == "vp9" and item.format == "webm":
        return "vp9_webm"
    return None


def compatibility_warning(item: Any) -> str | None:
    key = compatibility_warning_key(item)
    return COMPAT_WARNING_MESSAGES.get(key)


def resolve_cover_media(album: Any, media_items: list[Any]) -> Any | None:
    if album.cover_media_id:
        for item in media_items:
            if item.id == album.cover_media_id:
                return item
    return media_items[0] if media_items else None


def album_to_payload(
    base_url: str,
    album: Any,
    media_items: list[Any],
) -> dict[str, Any]:
    cover = resolve_cover_media(album, media_items)
    return {
        "id": album.id,
        "title": album.title,
        "cover_media_id": album.cover_media_id,
        "created_at": album.created_at.isoformat(),
        "updated_at": album.updated_at.isoformat(),
        "expires_at": album.expires_at.isoformat() if album.expires_at else None,
        "item_count": len(media_items),
        "total_size": sum(item.file_size for item in media_items),
        "cover_url": media_url(base_url, cover.id, cover.format) if cover else None,
        "items": [
            {
                "id": item.id,
                "filename": item.filename_orig,
                "media_type": item.media_type,
                "mime_type": item.mime_type,
                "media_url": media_url(base_url, item.id, item.format),
                "thumb_url": thumb_url(base_url, item.id, thumb_format(item)),
                "position": item.position,
                "file_size": item.file_size,
                "thumb_status": item.thumb_status,
                "codec_hint": item.codec_hint,
                "compat_warning_key": compatibility_warning_key(item),
                "compat_warning": compatibility_warning(item),
                "client_compat_check": compatibility_warning_key(item),
            }
            for item in media_items
        ],
    }
