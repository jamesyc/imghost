from __future__ import annotations

from typing import Any

from ..payloads import album_to_payload, compatibility_warning
from .display_helpers import display_timestamp, humanize_bytes, humanize_expiry


def build_workspace_bootstrap(
    album_id: str,
    *,
    access_mode: str,
    post_delete_url: str,
    delete_token: str | None,
) -> dict[str, Any]:
    return {
        "album_id": album_id,
        "access_mode": access_mode,
        "workspace_label": "Owner view" if access_mode == "owner" else "Manage view",
        "post_delete_url": post_delete_url,
        "delete_token": delete_token,
    }


def build_public_album_page_context(
    base_url: str,
    album: Any,
    items: list[Any],
    *,
    viewer_user_id: str | None,
) -> dict[str, Any]:
    album_payload = album_to_payload(base_url, album, items)
    album_payload["total_size_display"] = humanize_bytes(int(album_payload["total_size"]))
    album_payload["updated_at_display"] = display_timestamp(album_payload["updated_at"])
    for item in album_payload["items"]:
        item["file_size_display"] = humanize_bytes(int(item["file_size"]))
    return {
        "album_payload": album_payload,
        "expiry_hint": humanize_expiry(album.expires_at),
        "compat_warnings": [warning for warning in dict.fromkeys(compatibility_warning(item) for item in items) if warning],
        "is_owner_viewer": viewer_user_id is not None and album.user_id == viewer_user_id,
    }


def build_public_user_album_list_context(albums: list[dict[str, object]]) -> dict[str, Any]:
    public_albums: list[dict[str, object]] = []
    for album in albums:
        entry = dict(album)
        entry["total_size_display"] = humanize_bytes(int(entry["total_size"]))
        entry["created_at_display"] = display_timestamp(str(entry["created_at"]))
        public_albums.append(entry)
    return {"public_albums": public_albums}
