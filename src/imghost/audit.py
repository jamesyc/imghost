from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from .db import Database
from .events import (
    AdminLoggedIn,
    AlbumCoverSet,
    AlbumCreated,
    AlbumDeleted,
    AlbumExpiryChanged,
    AlbumReordered,
    AlbumTitleChanged,
    ConfigChanged,
    EventBus,
    MediaDeleted,
    MediaUploaded,
    UserDeleted,
    UserPasswordReset,
    UserRegistered,
    UserSuspended,
)
from .models import AuditEvent, utcnow


class PostgresAuditLog:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def write_audit_event(
        self,
        event_type: str,
        actor_id: str | None,
        actor_ip_hash: str | None,
        target_type: str,
        target_id: str,
        correlation_id: str,
        metadata: dict[str, object],
    ) -> AuditEvent:
        pool = self.database.require_pool()
        audit_event = AuditEvent(
            id=str(uuid4()),
            event_type=event_type,
            actor_id=actor_id,
            actor_ip_hash=actor_ip_hash,
            target_type=target_type,
            target_id=target_id,
            correlation_id=correlation_id,
            metadata=dict(metadata),
            created_at=utcnow(),
        )
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO audit_log (
                  id, event_type, actor_id, actor_ip_hash, target_type, target_id, correlation_id, metadata, created_at
                ) VALUES (
                  $1::uuid, $2, $3::uuid, $4, $5, $6, $7, $8::jsonb, $9
                )
                RETURNING id, event_type, actor_id, actor_ip_hash, target_type, target_id, correlation_id, metadata, created_at
                """,
                audit_event.id,
                audit_event.event_type,
                audit_event.actor_id,
                audit_event.actor_ip_hash,
                audit_event.target_type,
                audit_event.target_id,
                audit_event.correlation_id,
                audit_event.metadata,
                audit_event.created_at,
            )
        return AuditEvent(
            id=str(row["id"]),
            event_type=row["event_type"],
            actor_id=str(row["actor_id"]) if row["actor_id"] is not None else None,
            actor_ip_hash=row["actor_ip_hash"],
            target_type=row["target_type"],
            target_id=row["target_id"],
            correlation_id=row["correlation_id"],
            metadata=row["metadata"] or {},
            created_at=row["created_at"],
        )

    async def query_audit_log(
        self,
        *,
        event_type: str | None = None,
        actor_id: str | None = None,
        user_id: str | None = None,
        correlation_id: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditEvent]:
        query = """
        SELECT id, event_type, actor_id, actor_ip_hash, target_type, target_id, correlation_id, metadata, created_at
        FROM audit_log
        WHERE ($1::text IS NULL OR event_type = $1)
          AND ($2::uuid IS NULL OR actor_id = $2::uuid)
          AND ($3::text IS NULL OR correlation_id = $3)
          AND ($4::timestamptz IS NULL OR created_at >= $4)
          AND ($5::timestamptz IS NULL OR created_at <= $5)
          AND ($6::text IS NULL OR actor_id::text = $6 OR target_id = $6)
        ORDER BY created_at DESC
        LIMIT $7 OFFSET $8
        """
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, event_type, actor_id, correlation_id, after, before, user_id, limit, offset)
        return [
            AuditEvent(
                id=str(row["id"]),
                event_type=row["event_type"],
                actor_id=str(row["actor_id"]) if row["actor_id"] is not None else None,
                actor_ip_hash=row["actor_ip_hash"],
                target_type=row["target_type"],
                target_id=row["target_id"],
                correlation_id=row["correlation_id"],
                metadata=row["metadata"] or {},
                created_at=row["created_at"],
            )
            for row in rows
        ]


def register_audit_listeners(event_bus: EventBus, audit_log: PostgresAuditLog) -> None:
    async def write_album_created(event: AlbumCreated) -> None:
        await audit_log.write_audit_event(
            "album_created",
            event.user_id,
            None,
            "album",
            event.album_id,
            event.correlation_id,
            {
                "album_id": event.album_id,
                "item_count": event.item_count,
                "source": event.source,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_media_uploaded(event: MediaUploaded) -> None:
        await audit_log.write_audit_event(
            "media_uploaded",
            event.user_id,
            None,
            "media",
            event.media_id,
            event.correlation_id,
            {
                "media_id": event.media_id,
                "album_id": event.album_id,
                "file_size": event.file_size,
                "media_type": event.media_type,
                "format": event.format,
                "source": event.source,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_album_deleted(event: AlbumDeleted) -> None:
        await audit_log.write_audit_event(
            "album_deleted",
            event.actor_id,
            None,
            "album",
            event.album_id,
            event.correlation_id,
            {
                "album_id": event.album_id,
                "item_count": event.item_count,
                "total_size": event.total_size,
                "source": event.source,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_media_deleted(event: MediaDeleted) -> None:
        await audit_log.write_audit_event(
            "media_deleted",
            event.actor_id,
            None,
            "media",
            event.media_id,
            event.correlation_id,
            {
                "media_id": event.media_id,
                "album_id": event.album_id,
                "file_size": event.file_size,
                "source": event.source,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_album_title_changed(event: AlbumTitleChanged) -> None:
        await audit_log.write_audit_event(
            "album_title_changed",
            event.actor_id,
            None,
            "album",
            event.album_id,
            event.correlation_id,
            {
                "album_id": event.album_id,
                "old_title": event.old_title,
                "new_title": event.new_title,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_album_cover_set(event: AlbumCoverSet) -> None:
        await audit_log.write_audit_event(
            "album_cover_set",
            event.actor_id,
            None,
            "album",
            event.album_id,
            event.correlation_id,
            {
                "album_id": event.album_id,
                "media_id": event.media_id,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_album_reordered(event: AlbumReordered) -> None:
        await audit_log.write_audit_event(
            "album_reordered",
            event.actor_id,
            None,
            "album",
            event.album_id,
            event.correlation_id,
            {
                "album_id": event.album_id,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_album_expiry_changed(event: AlbumExpiryChanged) -> None:
        await audit_log.write_audit_event(
            "album_expiry_changed",
            event.actor_id,
            None,
            "album",
            event.album_id,
            event.correlation_id,
            {
                "album_id": event.album_id,
                "old_expiry": event.old_expiry,
                "new_expiry": event.new_expiry,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_user_deleted(event: UserDeleted) -> None:
        await audit_log.write_audit_event(
            "user_deleted",
            event.actor_id,
            None,
            "user",
            event.user_id,
            event.correlation_id,
            {
                "target_user_id": event.user_id,
                "deleted_by": event.deleted_by,
                "album_count": event.album_count,
                "media_count": event.media_count,
                "source": event.source,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_user_registered(event: UserRegistered) -> None:
        await audit_log.write_audit_event(
            "user_created",
            event.actor_id,
            None,
            "user",
            event.user_id,
            event.correlation_id,
            {
                "target_user_id": event.user_id,
                "method": event.method,
                "source": event.source,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_user_suspended(event: UserSuspended) -> None:
        await audit_log.write_audit_event(
            "user_suspended",
            event.actor_id,
            None,
            "user",
            event.user_id,
            event.correlation_id,
            {
                "target_user_id": event.user_id,
                "suspended": event.suspended,
                "source": event.source,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_user_password_reset(event: UserPasswordReset) -> None:
        await audit_log.write_audit_event(
            "user_password_reset",
            event.actor_id,
            None,
            "user",
            event.user_id,
            event.correlation_id,
            {
                "target_user_id": event.user_id,
                "source": event.source,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_admin_logged_in(event: AdminLoggedIn) -> None:
        await audit_log.write_audit_event(
            "admin_login",
            event.admin_id,
            None,
            "user",
            event.admin_id,
            event.correlation_id,
            {
                "source": event.source,
                "correlation_id": event.correlation_id,
            },
        )

    async def write_config_changed(event: ConfigChanged) -> None:
        await audit_log.write_audit_event(
            "config_changed",
            event.actor_id,
            None,
            "config",
            event.key,
            event.correlation_id,
            {
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
    event_bus.subscribe(ConfigChanged, write_config_changed)
