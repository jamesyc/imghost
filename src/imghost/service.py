from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from fastapi import HTTPException, UploadFile

from .config import Settings
from .events import AlbumCreated, EventBus, MediaUploaded
from .ids import generate_album_id, generate_media_id
from .models import Album, Media, utcnow
from .repositories import JsonRepository
from .storage import LocalFilesystemBackend


@dataclass
class UploadResult:
    album: Album
    media: Media


class UploadService:
    def __init__(
        self,
        settings: Settings,
        repository: JsonRepository,
        storage: LocalFilesystemBackend,
        event_bus: EventBus,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.storage = storage
        self.event_bus = event_bus

    async def upload(self, file: UploadFile, album_id: str | None, title: str | None, correlation_id: str) -> UploadResult:
        payload = await file.read()
        if not payload:
            raise HTTPException(status_code=400, detail="Empty file upload.")
        if len(payload) > self.settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail="Upload exceeds V1 size limit.")

        album = await self._get_or_create_album(album_id=album_id, title=title, correlation_id=correlation_id)
        media = await self._create_media(album.id, file, payload, correlation_id)
        album.updated_at = utcnow()
        if not album.title and title:
            album.title = title
        await self.repository.update_album(album)
        return UploadResult(album=album, media=media)

    async def _get_or_create_album(self, album_id: str | None, title: str | None, correlation_id: str) -> Album:
        if album_id:
            album = await self.repository.get_album(album_id)
            if album is None:
                raise HTTPException(status_code=404, detail="Album not found.")
            return album

        now = utcnow()
        album = Album(
            id=generate_album_id(),
            title=title,
            user_id=None,
            cover_media_id=None,
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(hours=self.settings.anon_expiry_hours),
        )
        await self.repository.create_album(album)
        await self.event_bus.emit(
            AlbumCreated(
                album_id=album.id,
                user_id=None,
                item_count=0,
                source="web",
                correlation_id=correlation_id,
            )
        )
        return album

    async def _create_media(self, album_id: str, file: UploadFile, payload: bytes, correlation_id: str) -> Media:
        media_id = generate_media_id()
        content_type = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"
        media_type = "video" if content_type.startswith("video/") else "image"
        suffix = Path(file.filename or "upload.bin").suffix.lower() or mimetypes.guess_extension(content_type) or ""
        fmt = suffix.lstrip(".") or content_type.split("/")[-1]
        storage_key = f"originals/anon/{media_id}{suffix}"
        await self.storage.put(storage_key, payload)
        position = await self.repository.next_position(album_id)

        media = Media(
            id=media_id,
            album_id=album_id,
            user_id=None,
            filename_orig=file.filename or media_id,
            media_type=media_type,
            format=fmt,
            mime_type=content_type,
            storage_key=storage_key,
            thumb_key=None,
            thumb_is_orig=True,
            thumb_status="done",
            file_size=len(payload),
            thumb_size=len(payload),
            width=None,
            height=None,
            duration_secs=None,
            is_animated=False,
            codec_hint=None,
            position=position,
            created_at=utcnow(),
        )
        await self.repository.create_media(media)
        await self.event_bus.emit(
            MediaUploaded(
                media_id=media.id,
                album_id=media.album_id,
                user_id=None,
                file_size=media.file_size,
                media_type=media.media_type,
                format=media.format,
                source="web",
                correlation_id=correlation_id,
            )
        )
        return media

