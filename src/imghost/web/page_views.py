from __future__ import annotations

from typing import Any

from ..payloads import album_to_payload, compatibility_warning, thumb_url
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
    album_payload["created_at_display"] = display_timestamp(album_payload["created_at"])
    album_payload["updated_at_display"] = display_timestamp(album_payload["updated_at"])
    album_payload["last_edited_display"] = (
        album_payload["updated_at_display"]
        if album_payload["updated_at_display"] != album_payload["created_at_display"]
        else None
    )
    for item in album_payload["items"]:
        item["file_size_display"] = humanize_bytes(int(item["file_size"]))
    album_title = album_payload["title"] or "Untitled album"
    item_count = int(album_payload["item_count"])
    return {
        "album_payload": album_payload,
        "expiry_hint": humanize_expiry(album.expires_at),
        "compat_warnings": [warning for warning in dict.fromkeys(compatibility_warning(item) for item in items) if warning],
        "is_owner_viewer": viewer_user_id is not None and album.user_id == viewer_user_id,
        "open_graph": build_open_graph(
            title=album_title,
            description=f"{item_count} item(s) · {album_payload['total_size_display']}",
            url=f"{base_url}/a/{album.id}",
            image_url=album_payload["cover_url"],
        ),
    }


def build_public_user_album_list_context(base_url: str, username: str, albums: list[dict[str, object]]) -> dict[str, Any]:
    public_albums: list[dict[str, object]] = []
    for album in albums:
        entry = dict(album)
        entry["total_size_display"] = humanize_bytes(int(entry["total_size"]))
        entry["created_at_display"] = display_timestamp(str(entry["created_at"]))
        public_albums.append(entry)
    image_url = None
    for album in public_albums:
        if (
            album.get("cover_media_id")
            and album.get("cover_thumb_format")
            and album.get("cover_thumb_status") == "done"
        ):
            image_url = thumb_url(base_url, str(album["cover_media_id"]), str(album["cover_thumb_format"]))
            break
    return {
        "public_albums": public_albums,
        "open_graph": build_open_graph(
            title=f"{username} albums",
            description=f"{len(public_albums)} public album(s), sorted by most recently modified.",
            url=f"{base_url}/u/{username}",
            image_url=image_url,
        ),
    }


def build_open_graph(
    *,
    title: str,
    description: str,
    url: str,
    image_url: str | None = None,
    type_: str = "website",
) -> dict[str, str]:
    tags = {
        "title": title,
        "description": description,
        "type": type_,
        "url": url,
        "twitter_card": "summary_large_image" if image_url else "summary",
        "twitter_title": title,
        "twitter_description": description,
    }
    if image_url:
        tags["image"] = image_url
        tags["twitter_image"] = image_url
    return tags
