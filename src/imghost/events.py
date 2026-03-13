from __future__ import annotations

import asyncio
import inspect
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

Listener = Callable[[Any], Awaitable[None] | None]


@dataclass(slots=True)
class AlbumCreated:
    album_id: str
    user_id: str | None
    item_count: int
    source: str
    correlation_id: str


@dataclass(slots=True)
class MediaUploaded:
    media_id: str
    album_id: str
    user_id: str | None
    file_size: int
    media_type: str
    format: str
    source: str
    correlation_id: str


@dataclass(slots=True)
class AlbumDeleted:
    album_id: str
    user_id: str | None
    actor_id: str | None
    item_count: int
    total_size: int
    source: str
    correlation_id: str


@dataclass(slots=True)
class MediaDeleted:
    media_id: str
    album_id: str
    user_id: str | None
    actor_id: str | None
    file_size: int
    source: str
    correlation_id: str


@dataclass(slots=True)
class AlbumTitleChanged:
    album_id: str
    user_id: str | None
    actor_id: str | None
    old_title: str | None
    new_title: str | None
    source: str
    correlation_id: str


@dataclass(slots=True)
class AlbumCoverSet:
    album_id: str
    user_id: str | None
    actor_id: str | None
    media_id: str | None
    source: str
    correlation_id: str


@dataclass(slots=True)
class AlbumReordered:
    album_id: str
    user_id: str | None
    actor_id: str | None
    source: str
    correlation_id: str


@dataclass(slots=True)
class AlbumExpiryChanged:
    album_id: str
    user_id: str | None
    actor_id: str | None
    old_expiry: str | None
    new_expiry: str | None
    source: str
    correlation_id: str


@dataclass(slots=True)
class UserDeleted:
    user_id: str
    actor_id: str | None
    deleted_by: str
    album_count: int
    media_count: int
    source: str
    correlation_id: str


@dataclass(slots=True)
class UserSuspended:
    user_id: str
    actor_id: str | None
    suspended: bool
    source: str
    correlation_id: str


@dataclass(slots=True)
class UserRegistered:
    user_id: str
    actor_id: str | None
    method: str
    source: str
    correlation_id: str


@dataclass(slots=True)
class ConfigChanged:
    key: str
    actor_id: str | None
    old_value: bool | int | None
    new_value: bool | int | None
    source: str
    correlation_id: str


class EventBus:
    def __init__(self) -> None:
        self._listeners: dict[type, list[Listener]] = defaultdict(list)

    def subscribe(self, event_type: type, listener: Listener) -> None:
        self._listeners[event_type].append(listener)

    async def emit(self, event: Any) -> None:
        listeners = list(self._listeners.get(type(event), []))
        if not listeners:
            return
        for listener in listeners:
            try:
                result = listener(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("event_listener_failed", extra={"event_type": type(event).__name__})
                await asyncio.sleep(0)
