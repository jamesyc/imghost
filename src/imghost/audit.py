from __future__ import annotations

import json
from asyncio import Lock
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .events import (
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
    UserSuspended,
)
from .models import AuditEvent, utcnow


class JsonAuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("[]", encoding="utf-8")

    def _load(self) -> list[AuditEvent]:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return [AuditEvent.from_dict(item) for item in payload]

    def _save(self, events: list[AuditEvent]) -> None:
        self.path.write_text(
            json.dumps([event.to_dict() for event in events], indent=2, sort_keys=True),
            encoding="utf-8",
        )

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
        async with self._lock:
            events = self._load()
            events.append(audit_event)
            self._save(events)
        return audit_event

    async def query_audit_log(
        self,
        *,
        event_type: str | None = None,
        actor_id: str | None = None,
        correlation_id: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditEvent]:
        async with self._lock:
            events = self._load()

        filtered: list[AuditEvent] = []
        for event in events:
            if event_type is not None and event.event_type != event_type:
                continue
            if actor_id is not None and event.actor_id != actor_id:
                continue
            if correlation_id is not None and event.correlation_id != correlation_id:
                continue
            if after is not None and event.created_at < after:
                continue
            if before is not None and event.created_at > before:
                continue
            filtered.append(event)

        filtered.sort(key=lambda event: event.created_at, reverse=True)
        return filtered[offset : offset + limit]


def register_audit_listeners(event_bus: EventBus, audit_log: JsonAuditLog) -> None:
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
    event_bus.subscribe(UserSuspended, write_user_suspended)
    event_bus.subscribe(ConfigChanged, write_config_changed)
