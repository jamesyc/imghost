from __future__ import annotations

import json
from asyncio import Lock
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Album, ApiKey, Media, User


@dataclass
class State:
    users: dict[str, User]
    api_keys: dict[str, ApiKey]
    albums: dict[str, Album]
    media: dict[str, Media]


class JsonRepository:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self._lock = Lock()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            self.state_path.write_text('{"users": {}, "api_keys": {}, "albums": {}, "media": {}}', encoding="utf-8")

    def _load(self) -> State:
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        return State(
            users={key: User.from_dict(value) for key, value in payload.get("users", {}).items()},
            api_keys={key: ApiKey.from_dict(value) for key, value in payload.get("api_keys", {}).items()},
            albums={key: Album.from_dict(value) for key, value in payload["albums"].items()},
            media={key: Media.from_dict(value) for key, value in payload["media"].items()},
        )

    def _save(self, state: State) -> None:
        payload: dict[str, Any] = {
            "users": {key: value.to_dict() for key, value in state.users.items()},
            "api_keys": {key: value.to_dict() for key, value in state.api_keys.items()},
            "albums": {key: value.to_dict() for key, value in state.albums.items()},
            "media": {key: value.to_dict() for key, value in state.media.items()},
        }
        self.state_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    async def create_user(self, user: User) -> User:
        async with self._lock:
            state = self._load()
            state.users[user.id] = user
            self._save(state)
        return user

    async def get_user(self, user_id: str) -> User | None:
        async with self._lock:
            return self._load().users.get(user_id)

    async def get_user_by_email(self, email: str) -> User | None:
        async with self._lock:
            state = self._load()
            for user in state.users.values():
                if user.email == email:
                    return user
        return None

    async def get_user_by_username(self, username: str) -> User | None:
        async with self._lock:
            state = self._load()
            for user in state.users.values():
                if user.username == username:
                    return user
        return None

    async def update_user(self, user: User) -> User:
        async with self._lock:
            state = self._load()
            state.users[user.id] = user
            self._save(state)
        return user

    async def upsert_api_key(self, api_key: ApiKey) -> ApiKey:
        async with self._lock:
            state = self._load()
            for key_id, item in list(state.api_keys.items()):
                if item.user_id == api_key.user_id:
                    state.api_keys.pop(key_id, None)
            state.api_keys[api_key.id] = api_key
            self._save(state)
        return api_key

    async def get_api_key_by_hash(self, key_hash: str) -> ApiKey | None:
        async with self._lock:
            state = self._load()
            for api_key in state.api_keys.values():
                if api_key.key_hash == key_hash:
                    return api_key
        return None

    async def get_api_key_for_user(self, user_id: str) -> ApiKey | None:
        async with self._lock:
            state = self._load()
            for api_key in state.api_keys.values():
                if api_key.user_id == user_id:
                    return api_key
        return None

    async def update_api_key(self, api_key: ApiKey) -> ApiKey:
        async with self._lock:
            state = self._load()
            state.api_keys[api_key.id] = api_key
            self._save(state)
        return api_key

    async def list_user_media(self, user_id: str) -> list[Media]:
        async with self._lock:
            state = self._load()
            items = [item for item in state.media.values() if item.user_id == user_id]
        return sorted(items, key=lambda item: item.created_at)

    async def list_user_albums(self, user_id: str) -> list[Album]:
        async with self._lock:
            state = self._load()
            items = [album for album in state.albums.values() if album.user_id == user_id]
        return sorted(items, key=lambda album: album.created_at)

    async def list_users(self) -> list[User]:
        async with self._lock:
            state = self._load()
            items = list(state.users.values())
        return sorted(items, key=lambda user: user.created_at)

    async def list_all_media(self) -> list[Media]:
        async with self._lock:
            state = self._load()
            items = list(state.media.values())
        return sorted(items, key=lambda media: media.created_at)

    async def delete_user(self, user_id: str) -> tuple[User | None, list[Album], list[Media]]:
        async with self._lock:
            state = self._load()
            user = state.users.pop(user_id, None)
            if user is None:
                return None, [], []

            albums = [album for album in state.albums.values() if album.user_id == user_id]
            album_ids = {album.id for album in albums}
            media_items = [media for media in state.media.values() if media.album_id in album_ids or media.user_id == user_id]

            for album_id in album_ids:
                state.albums.pop(album_id, None)
            for media in media_items:
                state.media.pop(media.id, None)
            for api_key_id, api_key in list(state.api_keys.items()):
                if api_key.user_id == user_id:
                    state.api_keys.pop(api_key_id, None)

            self._save(state)
            return user, sorted(albums, key=lambda album: album.created_at), sorted(media_items, key=lambda media: media.created_at)

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

    async def update_media(self, media: Media) -> Media:
        async with self._lock:
            state = self._load()
            state.media[media.id] = media
            self._save(state)
        return media

    async def list_media_by_thumb_status(self, *statuses: str) -> list[Media]:
        status_set = set(statuses)
        async with self._lock:
            state = self._load()
            items = [item for item in state.media.values() if item.thumb_status in status_set]
        return sorted(items, key=lambda item: item.created_at)

    async def find_pending_thumbnails(self) -> list[Media]:
        return await self.list_media_by_thumb_status("pending", "processing")

    async def find_failed_thumbnails(self) -> list[Media]:
        return await self.list_media_by_thumb_status("failed")

    async def list_expired_albums(self, now: datetime) -> list[Album]:
        async with self._lock:
            state = self._load()
            items = [album for album in state.albums.values() if album.expires_at is not None and album.expires_at <= now]
        return sorted(items, key=lambda album: album.expires_at or now)

    async def list_albums(self) -> list[Album]:
        async with self._lock:
            state = self._load()
            items = list(state.albums.values())
        return sorted(items, key=lambda album: album.created_at)

    async def list_album_media(self, album_id: str) -> list[Media]:
        async with self._lock:
            state = self._load()
            items = [item for item in state.media.values() if item.album_id == album_id]
        return sorted(items, key=lambda item: item.position)

    async def next_position(self, album_id: str) -> int:
        items = await self.list_album_media(album_id)
        return (items[-1].position + 1000) if items else 1000

    async def delete_media(self, media_id: str) -> Media | None:
        async with self._lock:
            state = self._load()
            media = state.media.pop(media_id, None)
            if media is not None:
                self._save(state)
            return media

    async def delete_album(self, album_id: str) -> tuple[Album | None, list[Media]]:
        async with self._lock:
            state = self._load()
            album = state.albums.pop(album_id, None)
            if album is None:
                return None, []
            media_items = [item for item in state.media.values() if item.album_id == album_id]
            for item in media_items:
                state.media.pop(item.id, None)
            self._save(state)
            return album, sorted(media_items, key=lambda item: item.position)

    async def update_media_positions(self, album_id: str, positions: dict[str, int]) -> list[Media]:
        async with self._lock:
            state = self._load()
            media_items = [item for item in state.media.values() if item.album_id == album_id]
            for item in media_items:
                if item.id in positions:
                    item.position = positions[item.id]
                    state.media[item.id] = item
            self._save(state)
            return sorted(media_items, key=lambda item: item.position)
