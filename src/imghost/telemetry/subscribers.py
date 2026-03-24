from __future__ import annotations

from ..events import (
    ApiKeyIssued,
    AdminLoggedIn,
    AlbumCoverSet,
    AlbumCreated,
    AlbumDeleted,
    AlbumExpiryChanged,
    AlbumReordered,
    AlbumTitleChanged,
    ConfigChanged,
    EventBus,
    LoginFailed,
    MediaDeleted,
    MediaUploaded,
    UserAdminStatusChanged,
    UserDeleted,
    UserLoggedOut,
    UserLimitsChanged,
    UserPasswordChanged,
    UserPasswordReset,
    UserRegistered,
    UserSuspended,
)
from . import actions
from .models import TelemetryActor, TelemetryObject
from .service import TelemetryService


def register_telemetry_subscribers(event_bus: EventBus, telemetry: TelemetryService) -> None:
    async def write_album_created(event: AlbumCreated) -> None:
        await telemetry.emit_event(
            event_type=actions.ALBUM_CREATED,
            action="album.create",
            result="success",
            actor=TelemetryActor(id=event.user_id, type=event.actor_kind),
            object=TelemetryObject(type="album", id=event.album_id),
            metadata={
                "album_id": event.album_id,
                "item_count": event.item_count,
                "actor_kind": event.actor_kind,
                "source": event.source,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_media_uploaded(event: MediaUploaded) -> None:
        await telemetry.emit_event(
            event_type=actions.MEDIA_UPLOADED,
            action="media.upload",
            result="success",
            actor=TelemetryActor(id=event.user_id, type=event.actor_kind),
            object=TelemetryObject(type="media", id=event.media_id),
            metadata={
                "media_id": event.media_id,
                "album_id": event.album_id,
                "file_size": event.file_size,
                "media_type": event.media_type,
                "format": event.format,
                "actor_kind": event.actor_kind,
                "source": event.source,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_album_deleted(event: AlbumDeleted) -> None:
        await telemetry.emit_event(
            event_type=actions.ALBUM_DELETED,
            action="album.delete",
            result="success",
            actor=TelemetryActor(id=event.actor_id, type=event.actor_kind),
            object=TelemetryObject(type="album", id=event.album_id),
            metadata={
                "album_id": event.album_id,
                "item_count": event.item_count,
                "total_size": event.total_size,
                "actor_kind": event.actor_kind,
                "source": event.source,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_media_deleted(event: MediaDeleted) -> None:
        await telemetry.emit_event(
            event_type=actions.MEDIA_DELETED,
            action="media.delete",
            result="success",
            actor=TelemetryActor(id=event.actor_id, type=event.actor_kind),
            object=TelemetryObject(type="media", id=event.media_id),
            metadata={
                "media_id": event.media_id,
                "album_id": event.album_id,
                "file_size": event.file_size,
                "actor_kind": event.actor_kind,
                "source": event.source,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_album_title_changed(event: AlbumTitleChanged) -> None:
        await telemetry.emit_event(
            event_type=actions.ALBUM_TITLE_CHANGED,
            action="album.title.update",
            result="success",
            actor=TelemetryActor(id=event.actor_id, type=event.actor_kind),
            object=TelemetryObject(type="album", id=event.album_id),
            metadata={
                "album_id": event.album_id,
                "old_title": event.old_title,
                "new_title": event.new_title,
                "actor_kind": event.actor_kind,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_album_cover_set(event: AlbumCoverSet) -> None:
        await telemetry.emit_event(
            event_type=actions.ALBUM_COVER_SET,
            action="album.cover.set",
            result="success",
            actor=TelemetryActor(id=event.actor_id, type=event.actor_kind),
            object=TelemetryObject(type="album", id=event.album_id),
            metadata={
                "album_id": event.album_id,
                "media_id": event.media_id,
                "actor_kind": event.actor_kind,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_album_reordered(event: AlbumReordered) -> None:
        await telemetry.emit_event(
            event_type=actions.ALBUM_REORDERED,
            action="album.reorder",
            result="success",
            actor=TelemetryActor(id=event.actor_id, type=event.actor_kind),
            object=TelemetryObject(type="album", id=event.album_id),
            metadata={
                "album_id": event.album_id,
                "actor_kind": event.actor_kind,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_album_expiry_changed(event: AlbumExpiryChanged) -> None:
        await telemetry.emit_event(
            event_type=actions.ALBUM_EXPIRY_CHANGED,
            action="album.expiry.update",
            result="success",
            actor=TelemetryActor(id=event.actor_id, type=event.actor_kind),
            object=TelemetryObject(type="album", id=event.album_id),
            metadata={
                "album_id": event.album_id,
                "old_expiry": event.old_expiry,
                "new_expiry": event.new_expiry,
                "actor_kind": event.actor_kind,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_user_deleted(event: UserDeleted) -> None:
        await telemetry.emit_event(
            event_type=actions.USER_DELETED,
            action="user.delete",
            result="success",
            actor=TelemetryActor(id=event.actor_id, type=event.actor_kind),
            object=TelemetryObject(type="user", id=event.user_id),
            metadata={
                "target_user_id": event.user_id,
                "deleted_by": event.deleted_by,
                "actor_kind": event.actor_kind,
                "album_count": event.album_count,
                "media_count": event.media_count,
                "source": event.source,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_user_registered(event: UserRegistered) -> None:
        await telemetry.emit_event(
            event_type=actions.USER_CREATED,
            action="user.create",
            result="success",
            actor=TelemetryActor(id=event.actor_id, type="user" if event.actor_id else "system"),
            object=TelemetryObject(type="user", id=event.user_id),
            metadata={
                "target_user_id": event.user_id,
                "method": event.method,
                "source": event.source,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_user_suspended(event: UserSuspended) -> None:
        await telemetry.emit_event(
            event_type=actions.USER_SUSPENDED,
            action="user.suspension.update",
            result="success",
            actor=TelemetryActor(id=event.actor_id, type="admin" if event.actor_id else "system"),
            object=TelemetryObject(type="user", id=event.user_id),
            metadata={
                "target_user_id": event.user_id,
                "suspended": event.suspended,
                "source": event.source,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_user_password_reset(event: UserPasswordReset) -> None:
        await telemetry.emit_event(
            event_type=actions.USER_PASSWORD_RESET,
            action="user.password.reset",
            result="success",
            actor=TelemetryActor(id=event.actor_id, type="admin" if event.actor_id else "system"),
            object=TelemetryObject(type="user", id=event.user_id),
            metadata={
                "target_user_id": event.user_id,
                "source": event.source,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_admin_logged_in(event: AdminLoggedIn) -> None:
        await telemetry.emit_event(
            event_type=actions.ADMIN_LOGIN,
            action="auth.login.success",
            result="success",
            actor=TelemetryActor(id=event.admin_id, type="admin"),
            object=TelemetryObject(type="user", id=event.admin_id),
            metadata={"source": event.source, "correlation_id": event.correlation_id},
        )

    async def write_login_failed(event: LoginFailed) -> None:
        await telemetry.emit_event(
            event_type=actions.LOGIN_FAILED,
            action="auth.login.failed",
            result="denied",
            actor=TelemetryActor(id=None, type="anonymous"),
            object=TelemetryObject(type="auth", id=event.login_identifier),
            metadata={
                "login_identifier": event.login_identifier,
                "reason": event.reason,
                "source": event.source,
                "correlation_id": event.correlation_id,
            },
            reason=event.reason,
        )

    async def write_user_logged_out(event: UserLoggedOut) -> None:
        await telemetry.emit_event(
            event_type=actions.LOGOUT,
            action="auth.logout",
            result="success",
            actor=TelemetryActor(id=event.user_id, type="user"),
            object=TelemetryObject(type="user", id=event.user_id),
            metadata={
                "target_user_id": event.user_id,
                "source": event.source,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_api_key_issued(event: ApiKeyIssued) -> None:
        await telemetry.emit_event(
            event_type=actions.API_KEY_ISSUED,
            action="apikey.issue",
            result="success",
            actor=TelemetryActor(id=event.actor_id, type="user" if event.actor_id else "system"),
            object=TelemetryObject(type="user", id=event.user_id),
            metadata={
                "target_user_id": event.user_id,
                "replaced_existing": event.replaced_existing,
                "source": event.source,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_user_password_changed(event: UserPasswordChanged) -> None:
        await telemetry.emit_event(
            event_type=actions.USER_PASSWORD_CHANGED,
            action="user.password.change",
            result="success",
            actor=TelemetryActor(id=event.actor_id, type="user" if event.actor_id else "system"),
            object=TelemetryObject(type="user", id=event.user_id),
            metadata={
                "target_user_id": event.user_id,
                "source": event.source,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_user_admin_status_changed(event: UserAdminStatusChanged) -> None:
        await telemetry.emit_event(
            event_type=actions.USER_ADMIN_STATUS_CHANGED,
            action="user.role.update",
            result="success",
            actor=TelemetryActor(id=event.actor_id, type="admin" if event.actor_id else "system"),
            object=TelemetryObject(type="user", id=event.user_id),
            metadata={
                "target_user_id": event.user_id,
                "old_is_admin": event.old_is_admin,
                "new_is_admin": event.new_is_admin,
                "source": event.source,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_user_limits_changed(event: UserLimitsChanged) -> None:
        await telemetry.emit_event(
            event_type=actions.USER_LIMITS_CHANGED,
            action="user.limits.update",
            result="success",
            actor=TelemetryActor(id=event.actor_id, type="admin" if event.actor_id else "system"),
            object=TelemetryObject(type="user", id=event.user_id),
            metadata={
                "target_user_id": event.user_id,
                "changes": event.changes,
                "source": event.source,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_config_changed(event: ConfigChanged) -> None:
        await telemetry.emit_event(
            event_type=actions.CONFIG_CHANGED,
            action="config.update",
            result="success",
            actor=TelemetryActor(id=event.actor_id, type="admin" if event.actor_id else "system"),
            object=TelemetryObject(type="config", id=event.key),
            metadata={
                "key": event.key,
                "old_value": event.old_value,
                "new_value": event.new_value,
                "source": event.source,
                "correlation_id": event.correlation_id,
            },
        )

    event_bus.subscribe(AlbumCreated, write_album_created)
    event_bus.subscribe(MediaUploaded, write_media_uploaded)
    event_bus.subscribe(AlbumDeleted, write_album_deleted)
    event_bus.subscribe(MediaDeleted, write_media_deleted)
    event_bus.subscribe(AlbumTitleChanged, write_album_title_changed)
    event_bus.subscribe(AlbumCoverSet, write_album_cover_set)
    event_bus.subscribe(AlbumReordered, write_album_reordered)
    event_bus.subscribe(AlbumExpiryChanged, write_album_expiry_changed)
    event_bus.subscribe(UserDeleted, write_user_deleted)
    event_bus.subscribe(UserRegistered, write_user_registered)
    event_bus.subscribe(UserSuspended, write_user_suspended)
    event_bus.subscribe(UserPasswordReset, write_user_password_reset)
    event_bus.subscribe(AdminLoggedIn, write_admin_logged_in)
    event_bus.subscribe(LoginFailed, write_login_failed)
    event_bus.subscribe(UserLoggedOut, write_user_logged_out)
    event_bus.subscribe(ApiKeyIssued, write_api_key_issued)
    event_bus.subscribe(UserPasswordChanged, write_user_password_changed)
    event_bus.subscribe(UserAdminStatusChanged, write_user_admin_status_changed)
    event_bus.subscribe(UserLimitsChanged, write_user_limits_changed)
    event_bus.subscribe(ConfigChanged, write_config_changed)
