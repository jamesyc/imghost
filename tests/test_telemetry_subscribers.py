from __future__ import annotations

import asyncio

import pytest

from imghost.events import (
    AdminLoggedIn,
    AlbumCoverSet,
    AlbumCreated,
    AlbumDeleted,
    AlbumExpiryChanged,
    AlbumReordered,
    AlbumTitleChanged,
    ApiKeyIssued,
    ConfigChanged,
    EventBus,
    LoginFailed,
    MediaDeleted,
    MediaUploaded,
    UserAdminStatusChanged,
    UserDeleted,
    UserLimitsChanged,
    UserLoggedOut,
    UserPasswordChanged,
    UserPasswordReset,
    UserRegistered,
    UserSuspended,
)
from imghost.telemetry.subscribers import register_telemetry_subscribers


class _RecordingTelemetryService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def emit_event(self, **kwargs) -> None:
        self.calls.append(kwargs)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (
            AlbumCreated(album_id="album-1", user_id="user-1", item_count=2, actor_kind="user", source="web", correlation_id="corr-1"),
            {"event_type": "album_created", "action": "album.create", "result": "success", "object_type": "album", "object_id": "album-1"},
        ),
        (
            MediaUploaded(media_id="media-1", album_id="album-1", user_id="user-1", file_size=12, media_type="image", format="png", actor_kind="user", source="web", correlation_id="corr-1"),
            {"event_type": "media_uploaded", "action": "media.upload", "result": "success", "object_type": "media", "object_id": "media-1"},
        ),
        (
            AlbumDeleted(album_id="album-1", user_id="user-1", actor_id="user-1", actor_kind="user", item_count=2, total_size=20, source="web", correlation_id="corr-1"),
            {"event_type": "album_deleted", "action": "album.delete", "result": "success", "object_type": "album", "object_id": "album-1"},
        ),
        (
            MediaDeleted(media_id="media-1", album_id="album-1", user_id="user-1", actor_id="user-1", actor_kind="user", file_size=12, source="web", correlation_id="corr-1"),
            {"event_type": "media_deleted", "action": "media.delete", "result": "success", "object_type": "media", "object_id": "media-1"},
        ),
        (
            AlbumTitleChanged(album_id="album-1", user_id="user-1", actor_id="user-1", actor_kind="user", old_title="old", new_title="new", source="web", correlation_id="corr-1"),
            {"event_type": "album_title_changed", "action": "album.title.update", "result": "success", "object_type": "album", "object_id": "album-1"},
        ),
        (
            AlbumCoverSet(album_id="album-1", user_id="user-1", actor_id="user-1", actor_kind="user", media_id="media-1", source="web", correlation_id="corr-1"),
            {"event_type": "album_cover_set", "action": "album.cover.set", "result": "success", "object_type": "album", "object_id": "album-1"},
        ),
        (
            AlbumReordered(album_id="album-1", user_id="user-1", actor_id="user-1", actor_kind="user", source="web", correlation_id="corr-1"),
            {"event_type": "album_reordered", "action": "album.reorder", "result": "success", "object_type": "album", "object_id": "album-1"},
        ),
        (
            AlbumExpiryChanged(album_id="album-1", user_id="user-1", actor_id="user-1", actor_kind="user", old_expiry="2026-01-01", new_expiry="2026-02-01", source="web", correlation_id="corr-1"),
            {"event_type": "album_expiry_changed", "action": "album.expiry.update", "result": "success", "object_type": "album", "object_id": "album-1"},
        ),
        (
            UserDeleted(user_id="user-1", actor_id="admin-1", actor_kind="admin", deleted_by="admin", album_count=3, media_count=4, source="api", correlation_id="corr-1"),
            {"event_type": "user_deleted", "action": "user.delete", "result": "success", "object_type": "user", "object_id": "user-1"},
        ),
        (
            UserRegistered(user_id="user-1", actor_id=None, method="password", source="web", correlation_id="corr-1"),
            {"event_type": "user_created", "action": "user.create", "result": "success", "object_type": "user", "object_id": "user-1"},
        ),
        (
            UserSuspended(user_id="user-1", actor_id="admin-1", suspended=True, source="api", correlation_id="corr-1"),
            {"event_type": "user_suspended", "action": "user.suspension.update", "result": "success", "object_type": "user", "object_id": "user-1"},
        ),
        (
            UserPasswordReset(user_id="user-1", actor_id="admin-1", source="api", correlation_id="corr-1"),
            {"event_type": "user_password_reset", "action": "user.password.reset", "result": "success", "object_type": "user", "object_id": "user-1"},
        ),
        (
            AdminLoggedIn(admin_id="admin-1", source="web", correlation_id="corr-1"),
            {"event_type": "admin_login", "action": "auth.login.success", "result": "success", "object_type": "user", "object_id": "admin-1"},
        ),
        (
            LoginFailed(login_identifier="user@example.com", reason="bad_password", source="web", correlation_id="corr-1"),
            {"event_type": "login_failed", "action": "auth.login.failed", "result": "denied", "object_type": "auth", "object_id": "user@example.com"},
        ),
        (
            UserLoggedOut(user_id="user-1", source="web", correlation_id="corr-1"),
            {"event_type": "logout", "action": "auth.logout", "result": "success", "object_type": "user", "object_id": "user-1"},
        ),
        (
            ApiKeyIssued(user_id="user-1", actor_id="user-1", replaced_existing=False, source="web", correlation_id="corr-1"),
            {"event_type": "api_key_issued", "action": "apikey.issue", "result": "success", "object_type": "user", "object_id": "user-1"},
        ),
        (
            UserPasswordChanged(user_id="user-1", actor_id="user-1", source="web", correlation_id="corr-1"),
            {"event_type": "user_password_changed", "action": "user.password.change", "result": "success", "object_type": "user", "object_id": "user-1"},
        ),
        (
            UserAdminStatusChanged(user_id="user-1", actor_id="admin-1", old_is_admin=False, new_is_admin=True, source="api", correlation_id="corr-1"),
            {"event_type": "user_admin_status_changed", "action": "user.role.update", "result": "success", "object_type": "user", "object_id": "user-1"},
        ),
        (
            UserLimitsChanged(user_id="user-1", actor_id="admin-1", changes={"upload_bytes_per_hour": {"old": None, "new": 10}}, source="api", correlation_id="corr-1"),
            {"event_type": "user_limits_changed", "action": "user.limits.update", "result": "success", "object_type": "user", "object_id": "user-1"},
        ),
        (
            ConfigChanged(key="allow_registration", actor_id="admin-1", old_value=True, new_value=False, source="api", correlation_id="corr-1"),
            {"event_type": "config_changed", "action": "config.update", "result": "success", "object_type": "config", "object_id": "allow_registration"},
        ),
    ],
)
async def test_register_telemetry_subscribers_maps_domain_events(event, expected: dict[str, str]) -> None:
    event_bus = EventBus()
    telemetry = _RecordingTelemetryService()
    register_telemetry_subscribers(event_bus, telemetry)

    await event_bus.emit(event)

    assert len(telemetry.calls) == 1
    call = telemetry.calls[0]
    assert call["event_type"] == expected["event_type"]
    assert call["action"] == expected["action"]
    assert call["result"] == expected["result"]
    assert call["object"].type == expected["object_type"]
    assert call["object"].id == expected["object_id"]
    assert call["metadata"]["correlation_id"] == "corr-1"
