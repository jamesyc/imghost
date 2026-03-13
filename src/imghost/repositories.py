from __future__ import annotations

import json
from asyncio import Lock
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import Album, Media


@dataclass
class State:
    albums: dict[str, Album]
    media: dict[str, Media]


class JsonRepository:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self._lock = Lock()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            self.state_path.write_text('{"albums": {}, "media": {}}', encoding="utf-8")

    def _load(self) -> State:
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        return State(
            albums={key: Album.from_dict(value) for key, value in payload["albums"].items()},
            media={key: Media.from_dict(value) for key, value in payload["media"].items()},
        )

    def _save(self, state: State) -> None:
        payload: dict[str, Any] = {
            "albums": {key: value.to_dict() for key, value in state.albums.items()},
            "media": {key: value.to_dict() for key, value in state.media.items()},
        }
        self.state_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    async def create_album(self, album: Album) -> Album:
        async with self._lock:
            state = self._load()
            state.albums[album.id] = album
            self._save(state)
        return album

    async def get_album(self, album_id: str) -> Album | None:
        async with self._lock:
            return self._load().albums.get(album_id)

    async def update_album(self, album: Album) -> Album:
        async with self._lock:
            state = self._load()
            state.albums[album.id] = album
            self._save(state)
        return album

    async def create_media(self, media: Media) -> Media:
        async with self._lock:
            state = self._load()
            state.media[media.id] = media
            self._save(state)
        return media

    async def get_media(self, media_id: str) -> Media | None:
        async with self._lock:
            return self._load().media.get(media_id)

    async def list_album_media(self, album_id: str) -> list[Media]:
        async with self._lock:
            state = self._load()
            items = [item for item in state.media.values() if item.album_id == album_id]
        return sorted(items, key=lambda item: item.position)

    async def next_position(self, album_id: str) -> int:
        items = await self.list_album_media(album_id)
        return (items[-1].position + 1000) if items else 1000

