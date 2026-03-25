from datetime import UTC, datetime

from imghost.models import Album, Media
from imghost.web.page_views import (
    build_public_album_page_context,
    build_public_user_album_list_context,
    build_workspace_bootstrap,
)


def test_build_workspace_bootstrap_sets_owner_and_token_labels() -> None:
    owner = build_workspace_bootstrap("album123", access_mode="owner", post_delete_url="/albums", delete_token=None)
    assert owner == {
        "album_id": "album123",
        "access_mode": "owner",
        "workspace_label": "Owner view",
        "post_delete_url": "/albums",
        "delete_token": None,
    }

    token = build_workspace_bootstrap("album123", access_mode="token", post_delete_url="/", delete_token="secret")
    assert token == {
        "album_id": "album123",
        "access_mode": "token",
        "workspace_label": "Manage view",
        "post_delete_url": "/",
        "delete_token": "secret",
    }


def test_build_public_album_page_context_adds_display_fields_and_owner_flag() -> None:
    created = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    album = Album(
        id="album123",
        title="Display Album",
        user_id="user123",
        cover_media_id=None,
        delete_token=None,
        created_at=created,
        updated_at=created,
        expires_at=None,
    )
    item = Media(
        id="media123",
        album_id="album123",
        user_id="user123",
        filename_orig="sample.png",
        media_type="image",
        format="png",
        mime_type="image/png",
        storage_key="media/sample.png",
        thumb_key="thumb/sample.jpg",
        thumb_is_orig=False,
        thumb_status="done",
        file_size=68,
        thumb_size=12,
        width=1,
        height=1,
        duration_secs=None,
        is_animated=False,
        codec_hint=None,
        position=0,
        created_at=created,
    )

    payload = build_public_album_page_context(
        "https://testserver",
        album,
        [item],
        viewer_user_id="user123",
    )

    assert payload["album_payload"]["total_size_display"] == "68 B"
    assert payload["album_payload"]["created_at_display"]
    assert payload["album_payload"]["updated_at_display"]
    assert payload["album_payload"]["last_edited_display"] is None
    assert payload["album_payload"]["items"][0]["file_size_display"] == "68 B"
    assert payload["expiry_hint"] is None
    assert payload["compat_warnings"] == []
    assert payload["is_owner_viewer"] is True


def test_build_public_album_page_context_omits_last_edited_when_album_was_not_edited() -> None:
    created = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    album = Album(
        id="album123",
        user_id="user123",
        title="Public Album",
        cover_media_id=None,
        delete_token="token123",
        created_at=created,
        updated_at=created,
        expires_at=None,
    )
    item = Media(
        id="media123",
        album_id="album123",
        user_id="user123",
        filename_orig="pixel.png",
        media_type="image",
        format="png",
        mime_type="image/png",
        storage_key="media/pixel.png",
        thumb_key=None,
        thumb_is_orig=False,
        thumb_status="done",
        file_size=68,
        thumb_size=None,
        width=1,
        height=1,
        duration_secs=None,
        is_animated=False,
        codec_hint=None,
        position=0,
        created_at=created,
    )

    payload = build_public_album_page_context(
        "https://testserver",
        album,
        [item],
        viewer_user_id=None,
    )

    assert payload["album_payload"]["created_at_display"]
    assert payload["album_payload"]["updated_at_display"]
    assert payload["album_payload"]["last_edited_display"] is None


def test_build_public_album_page_context_includes_client_compatibility_check_for_hevc_video() -> None:
    created = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    album = Album(
        id="album123",
        user_id="user123",
        title="Compat Album",
        cover_media_id=None,
        delete_token="token123",
        created_at=created,
        updated_at=created,
        expires_at=None,
    )
    item = Media(
        id="media123",
        album_id="album123",
        user_id="user123",
        filename_orig="clip.mov",
        media_type="video",
        format="mov",
        mime_type="video/quicktime",
        storage_key="media/clip.mov",
        thumb_key=None,
        thumb_is_orig=False,
        thumb_status="done",
        file_size=68,
        thumb_size=None,
        width=640,
        height=360,
        duration_secs=3.0,
        is_animated=False,
        codec_hint="hevc",
        position=0,
        created_at=created,
    )

    payload = build_public_album_page_context(
        "https://testserver",
        album,
        [item],
        viewer_user_id=None,
    )

    entry = payload["album_payload"]["items"][0]
    assert entry["compat_warning_key"] == "hevc"
    assert entry["client_compat_check"] == "hevc"
    assert "HEVC encoding" in entry["compat_warning"]


def test_build_public_user_album_list_context_adds_display_fields_without_mutating_input() -> None:
    source = [
        {
            "id": "album123",
            "title": "Public Album",
            "item_count": 1,
            "total_size": 68,
            "created_at": "2026-01-02T03:04:05+00:00",
            "cover_media_id": "media123",
            "cover_thumb_format": "jpg",
            "cover_thumb_status": "done",
        }
    ]

    payload = build_public_user_album_list_context("https://testserver", "gallery", source)

    assert payload["public_albums"][0]["total_size_display"] == "68 B"
    assert payload["public_albums"][0]["created_at_display"]
    assert payload["open_graph"] == {
        "title": "gallery albums",
        "description": "1 public album(s), sorted by most recently modified.",
        "type": "website",
        "url": "https://testserver/u/gallery",
        "image": "https://testserver/t/media123.jpg",
        "twitter_card": "summary_large_image",
        "twitter_title": "gallery albums",
        "twitter_description": "1 public album(s), sorted by most recently modified.",
        "twitter_image": "https://testserver/t/media123.jpg",
    }
    assert "total_size_display" not in source[0]
    assert "created_at_display" not in source[0]
