from __future__ import annotations

import mimetypes
import secrets
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import HTTPException, UploadFile

from .config import Settings
from .events import (
    AlbumCoverSet,
    AlbumCreated,
    AlbumDeleted,
    AlbumReordered,
    AlbumTitleChanged,
    EventBus,
    MediaDeleted,
    MediaUploaded,
)
from .ids import generate_album_id, generate_media_id
from .models import Album, Media, utcnow
from .processors import ProcessorRegistry
from .repositories import JsonRepository
from .storage import LocalFilesystemBackend

MAX_ALBUM_ITEMS = 1000
UNSET = object()
VIDEO_FORMATS = {"mp4", "mov", "webm"}


@dataclass
class UploadResult:
    album: Album
    media: Media


@dataclass
class MediaDeleteResult:
    deleted_media: Media
    album: Album | None
    remaining_items: list[Media]
    album_deleted: bool


@dataclass
class PruneResult:
    dry_run: bool
    album_ids: list[str]
    item_count: int
    bytes_freed: int


class UploadService:
    def __init__(
        self,
        settings: Settings,
        repository: JsonRepository,
        storage: LocalFilesystemBackend,
        event_bus: EventBus,
        processors: ProcessorRegistry,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.storage = storage
        self.event_bus = event_bus
        self.processors = processors

    async def upload(self, file: UploadFile, album_id: str | None, title: str | None, correlation_id: str) -> UploadResult:
        payload = await file.read()
        if not payload:
            raise HTTPException(status_code=400, detail="Empty file upload.")
        if len(payload) > self.settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail="Upload exceeds V1 size limit.")

        album = await self._get_or_create_album(album_id=album_id, title=title, correlation_id=correlation_id)
        if len(await self.repository.list_album_media(album.id)) >= MAX_ALBUM_ITEMS:
            raise HTTPException(status_code=413, detail="Album item limit reached.")
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
            delete_token=secrets.token_urlsafe(24),
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
        suffix = Path(file.filename or "upload.bin").suffix.lower() or mimetypes.guess_extension(content_type) or ""
        fmt = suffix.lstrip(".") or content_type.split("/")[-1]
        media_type = "video" if content_type.startswith("video/") or fmt.lower() in VIDEO_FORMATS else "image"
        processor = self.processors.get_processor(fmt)
        if processor is None:
            if media_type == "image":
                raise HTTPException(status_code=415, detail="Unsupported image format.")
            raise HTTPException(status_code=415, detail="Unsupported video format.")

        validation = await processor.validate(payload)
        if not validation.ok:
            raise HTTPException(status_code=415, detail=validation.rejection_reason)
        metadata = await processor.extract_metadata(payload, fmt)
        sanitized = await processor.sanitize(payload, metadata)
        payload = sanitized.data
        content_type = sanitized.mime_type
        fmt = sanitized.format
        suffix = f".{fmt if fmt != 'jpeg' else 'jpg'}"
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
            thumb_is_orig=False,
            thumb_status="pending",
            file_size=len(payload),
            thumb_size=None,
            width=metadata.width if metadata else None,
            height=metadata.height if metadata else None,
            duration_secs=metadata.duration_secs if metadata else None,
            is_animated=metadata.is_animated if metadata else False,
            codec_hint=metadata.codec_hint if metadata else None,
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
        refreshed_media = await self.repository.get_media(media.id)
        return refreshed_media or media

    async def generate_thumbnail(self, media_id: str, correlation_id: str) -> None:
        media = await self.repository.get_media(media_id)
        if media is None or media.thumb_status == "done":
            return

        media.thumb_status = "processing"
        await self.repository.update_media(media)

        try:
            processor = self.processors.get_processor(media.format)
            if processor is None:
                raise ValueError(f"No processor for format {media.format}")
            payload = await self.storage.get_bytes(media.storage_key)
            metadata = await processor.extract_metadata(payload, media.format)
            thumbnail = await processor.generate_thumbnail(payload, metadata)
            if thumbnail.thumb_is_orig:
                media.thumb_is_orig = True
                media.thumb_key = None
                media.thumb_size = media.file_size
            else:
                thumb_ext = thumbnail.format if thumbnail.format != "jpeg" else "jpg"
                thumb_key = f"thumbnails/{media.id}.{thumb_ext}"
                await self.storage.put(thumb_key, thumbnail.data or b"")
                media.thumb_is_orig = False
                media.thumb_key = thumb_key
                media.thumb_size = thumbnail.size
            media.thumb_status = "done"
        except Exception:
            media.thumb_status = "failed"
            media.thumb_key = None
            media.thumb_size = None
            media.thumb_is_orig = False
        await self.repository.update_media(media)

    async def delete_album(self, album_id: str, delete_token: str | None, correlation_id: str) -> tuple[Album, list[Media]]:
        album = await self.repository.get_album(album_id)
        if album is None:
            raise HTTPException(status_code=404, detail="Album not found.")
        self._require_delete_token(album, delete_token)

        media_items = await self.repository.list_album_media(album_id)
        for media in media_items:
            await self.storage.delete(media.storage_key)
            if media.thumb_key and media.thumb_key != media.storage_key:
                await self.storage.delete(media.thumb_key)

        deleted_album, deleted_media = await self.repository.delete_album(album_id)
        if deleted_album is None:
            raise HTTPException(status_code=404, detail="Album not found.")

        await self.event_bus.emit(
            AlbumDeleted(
                album_id=deleted_album.id,
                user_id=deleted_album.user_id,
                item_count=len(deleted_media),
                total_size=sum(item.file_size + (item.thumb_size or 0) for item in deleted_media),
                source="web",
                correlation_id=correlation_id,
            )
        )
        return deleted_album, deleted_media

    async def update_album(
        self,
        album_id: str,
        delete_token: str | None,
        correlation_id: str,
        *,
        title: str | None | object = UNSET,
        cover_media_id: str | None | object = UNSET,
    ) -> tuple[Album, list[Media]]:
        album = await self.repository.get_album(album_id)
        if album is None:
            raise HTTPException(status_code=404, detail="Album not found.")
        self._require_delete_token(album, delete_token)

        items = await self.repository.list_album_media(album_id)
        media_by_id = {item.id: item for item in items}
        changed = False

        if title is not UNSET:
            normalized_title = self._normalize_title(title)
            if album.title != normalized_title:
                old_title = album.title
                album.title = normalized_title
                changed = True
                await self.event_bus.emit(
                    AlbumTitleChanged(
                        album_id=album.id,
                        user_id=album.user_id,
                        old_title=old_title,
                        new_title=normalized_title,
                        source="web",
                        correlation_id=correlation_id,
                    )
                )

        if cover_media_id is not UNSET:
            next_cover = self._normalize_cover_media_id(cover_media_id, media_by_id)
            if album.cover_media_id != next_cover:
                album.cover_media_id = next_cover
                changed = True
                await self.event_bus.emit(
                    AlbumCoverSet(
                        album_id=album.id,
                        user_id=album.user_id,
                        media_id=next_cover,
                        source="web",
                        correlation_id=correlation_id,
                    )
                )

        if changed:
            album.updated_at = utcnow()
            await self.repository.update_album(album)
        return album, await self.repository.list_album_media(album_id)

    async def reorder_album(
        self,
        album_id: str,
        delete_token: str | None,
        order: list[tuple[str, int]],
        correlation_id: str,
    ) -> tuple[Album, list[Media]]:
        album = await self.repository.get_album(album_id)
        if album is None:
            raise HTTPException(status_code=404, detail="Album not found.")
        self._require_delete_token(album, delete_token)

        items = await self.repository.list_album_media(album_id)
        media_by_id = {item.id: item for item in items}
        if not order:
            raise HTTPException(status_code=400, detail="At least one position update is required.")

        positions: dict[str, int] = {}
        for media_id, position in order:
            media = media_by_id.get(media_id)
            if media is None:
                raise HTTPException(status_code=404, detail=f"Media {media_id} not found in album.")
            positions[media_id] = position

        reordered = await self.repository.update_media_positions(album_id, positions)
        if self._needs_rebalance(reordered):
            positions = {item.id: index * 1000 for index, item in enumerate(reordered, start=1)}
            reordered = await self.repository.update_media_positions(album_id, positions)

        album.updated_at = utcnow()
        await self.repository.update_album(album)
        await self.event_bus.emit(
            AlbumReordered(
                album_id=album.id,
                user_id=album.user_id,
                source="web",
                correlation_id=correlation_id,
            )
        )
        return album, reordered

    async def delete_media(
        self,
        media_id: str,
        delete_token: str | None,
        correlation_id: str,
    ) -> MediaDeleteResult:
        media = await self.repository.get_media(media_id)
        if media is None:
            raise HTTPException(status_code=404, detail="Media not found.")

        album = await self.repository.get_album(media.album_id)
        if album is None:
            raise HTTPException(status_code=404, detail="Album not found.")
        self._require_delete_token(album, delete_token)

        await self.storage.delete(media.storage_key)
        if media.thumb_key and media.thumb_key != media.storage_key:
            await self.storage.delete(media.thumb_key)

        deleted_media = await self.repository.delete_media(media_id)
        if deleted_media is None:
            raise HTTPException(status_code=404, detail="Media not found.")

        await self.event_bus.emit(
            MediaDeleted(
                media_id=deleted_media.id,
                album_id=deleted_media.album_id,
                user_id=deleted_media.user_id,
                file_size=deleted_media.file_size + (deleted_media.thumb_size or 0),
                source="web",
                correlation_id=correlation_id,
            )
        )

        remaining_items = await self.repository.list_album_media(album.id)
        if not remaining_items:
            deleted_album, _ = await self.repository.delete_album(album.id)
            if deleted_album is not None:
                await self.event_bus.emit(
                    AlbumDeleted(
                        album_id=deleted_album.id,
                        user_id=deleted_album.user_id,
                        item_count=0,
                        total_size=0,
                        source="web",
                        correlation_id=correlation_id,
                    )
                )
            return MediaDeleteResult(
                deleted_media=deleted_media,
                album=None,
                remaining_items=[],
                album_deleted=True,
            )

        if album.cover_media_id == deleted_media.id:
            album.cover_media_id = None
        album.updated_at = utcnow()
        await self.repository.update_album(album)
        return MediaDeleteResult(
            deleted_media=deleted_media,
            album=album,
            remaining_items=remaining_items,
            album_deleted=False,
        )

    async def build_album_zip(self, album_id: str) -> bytes:
        album = await self.repository.get_album(album_id)
        if album is None:
            raise HTTPException(status_code=404, detail="Album not found.")
        media_items = await self.repository.list_album_media(album_id)

        from io import BytesIO

        seen_names: set[str] = set()
        archive_buffer = BytesIO()
        with ZipFile(archive_buffer, mode="w", compression=ZIP_DEFLATED) as archive:
            for index, media in enumerate(media_items, start=1):
                filename = self._archive_name(media, index, seen_names)
                archive.writestr(filename, await self.storage.get_bytes(media.storage_key))
        return archive_buffer.getvalue()

    async def prune_expired_albums(self, *, dry_run: bool = False) -> PruneResult:
        expired_albums = await self.repository.list_expired_albums(utcnow())
        album_ids: list[str] = []
        item_count = 0
        bytes_freed = 0

        for album in expired_albums:
            media_items = await self.repository.list_album_media(album.id)
            storage_keys = self._storage_keys_for_media(media_items)
            album_ids.append(album.id)
            item_count += len(media_items)
            bytes_freed += self._storage_bytes_for_media(media_items)

            if dry_run:
                continue

            storage_ok = True
            for key in storage_keys:
                try:
                    await self.storage.delete(key)
                except Exception:
                    storage_ok = False
                    break

            if not storage_ok:
                album_ids.pop()
                item_count -= len(media_items)
                bytes_freed -= self._storage_bytes_for_media(media_items)
                continue

            deleted_album, deleted_media = await self.repository.delete_album(album.id)
            if deleted_album is None:
                continue
            await self.event_bus.emit(
                AlbumDeleted(
                    album_id=deleted_album.id,
                    user_id=deleted_album.user_id,
                    item_count=len(deleted_media),
                    total_size=self._storage_bytes_for_media(deleted_media),
                    source="system",
                    correlation_id=f"prune-{deleted_album.id}",
                )
            )

        return PruneResult(
            dry_run=dry_run,
            album_ids=album_ids,
            item_count=item_count,
            bytes_freed=bytes_freed,
        )

    def _archive_name(self, media: Media, index: int, seen_names: set[str]) -> str:
        candidate = Path(media.filename_orig).name or f"{media.id}.{media.format}"
        if "." not in candidate and media.format:
            candidate = f"{candidate}.{media.format}"
        if candidate not in seen_names:
            seen_names.add(candidate)
            return candidate

        stem = Path(candidate).stem or media.id
        suffix = Path(candidate).suffix
        while True:
            deduped = f"{stem}-{index}{suffix}"
            if deduped not in seen_names:
                seen_names.add(deduped)
                return deduped
            index += 1

    def _require_delete_token(self, album: Album, delete_token: str | None) -> None:
        if album.delete_token and delete_token != album.delete_token:
            raise HTTPException(status_code=403, detail="Invalid delete token.")

    def _normalize_title(self, title: str | None | object) -> str | None:
        if title is None:
            return None
        if not isinstance(title, str):
            raise HTTPException(status_code=400, detail="Invalid title.")
        normalized = title.strip()
        return normalized or None

    def _normalize_cover_media_id(self, cover_media_id: str | None | object, media_by_id: dict[str, Media]) -> str | None:
        if cover_media_id is None:
            return None
        if not isinstance(cover_media_id, str):
            raise HTTPException(status_code=400, detail="Invalid cover_media_id.")
        if cover_media_id not in media_by_id:
            raise HTTPException(status_code=404, detail="Cover media not found in album.")
        return cover_media_id

    def _needs_rebalance(self, items: list[Media]) -> bool:
        for previous, current in zip(items, items[1:]):
            if current.position - previous.position < 2:
                return True
        return False

    def _storage_keys_for_media(self, media_items: list[Media]) -> list[str]:
        keys: list[str] = []
        seen: set[str] = set()
        for media in media_items:
            for key in (media.storage_key, media.thumb_key):
                if not key or key in seen:
                    continue
                seen.add(key)
                keys.append(key)
        return keys

    def _storage_bytes_for_media(self, media_items: list[Media]) -> int:
        total = 0
        for media in media_items:
            total += media.file_size
            if media.thumb_key and media.thumb_key != media.storage_key:
                total += media.thumb_size or 0
        return total
