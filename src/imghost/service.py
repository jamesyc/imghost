from __future__ import annotations

import logging
import mimetypes
import secrets
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from zipfile import ZIP_DEFLATED

from zipstream import ZipStream

from fastapi import HTTPException, UploadFile

from .account_service import (
    UNSET,
    AccountService,
    ApiKeyIssueResult,
    LocalLoginInput,
    PasswordChangeInput,
    UserCreateInput,
    UserUpdateInput,
)
from .config import Settings
from .events import (
    AlbumCoverSet,
    AlbumCreated,
    AlbumDeleted,
    AlbumExpiryChanged,
    AlbumReordered,
    AlbumTitleChanged,
    EventBus,
    MediaDeleted,
    MediaUploaded,
)
from .ids import generate_album_id, generate_media_id
from .models import Album, Media, User, utcnow
from .oauth import OAuthIdentity
from .observability import ObservabilityState
from .processors import ProcessorRegistry, VideoProcessingError
from .rate_limits import RateLimiter
from .repositories import PostgresRepository
from .runtime_config import PostgresRuntimeConfig
from .payloads import album_to_payload
from .storage import StorageBackend
from .zip_streaming import AsyncIterableBridge

MAX_ALBUM_ITEMS = 1000
VIDEO_FORMATS = {"mp4", "mov", "webm"}
logger = logging.getLogger(__name__)


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


@dataclass
class CurrentActor:
    user: User | None
    source: str


@dataclass
class AdminAlbumUpdateInput:
    expires_at: object = UNSET


class UploadService:
    def __init__(
        self,
        settings: Settings,
        repository: PostgresRepository,
        storage: StorageBackend,
        event_bus: EventBus,
        processors: ProcessorRegistry,
        runtime_config: PostgresRuntimeConfig,
        rate_limiter: RateLimiter,
        observability: ObservabilityState | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.storage = storage
        self.event_bus = event_bus
        self.processors = processors
        self.runtime_config = runtime_config
        self.rate_limiter = rate_limiter
        self.observability = observability
        self.accounts = AccountService(settings, repository, storage, event_bus)

    def _require_password_value(self, password: str, *, label: str) -> str:
        return self.accounts._require_password_value(password, label=label)

    def _actor_kind(self, actor_user: User | None, delete_token: str | None = None, *, system: bool = False) -> str:
        if system:
            return "system"
        if actor_user is not None:
            return "admin" if actor_user.is_admin else "user"
        if delete_token:
            return "delete_token"
        return "anonymous"

    async def upload(
        self,
        file: UploadFile,
        album_id: str | None,
        title: str | None,
        correlation_id: str,
        *,
        actor: CurrentActor | None = None,
        delete_token: str | None = None,
        rate_limit_key: str | None = None,
    ) -> UploadResult:
        payload = await self._read_bounded_upload(file)
        if not payload:
            raise HTTPException(status_code=400, detail="Empty file upload.")

        actor = actor or CurrentActor(user=None, source="web")
        if rate_limit_key:
            await self.rate_limiter.enforce_upload_limits(
                actor_key=rate_limit_key,
                byte_count=len(payload),
                user=actor.user,
            )
        await self._enforce_storage_quotas(actor.user, incoming_bytes=len(payload))
        created_album = album_id is None
        album = await self._get_or_create_album(
            album_id=album_id,
            title=title,
            correlation_id=correlation_id,
            actor=actor,
            delete_token=delete_token,
        )
        if len(await self.repository.list_album_media(album.id)) >= MAX_ALBUM_ITEMS:
            raise HTTPException(status_code=413, detail="Album item limit reached.")
        try:
            media = await self._create_media(album.id, file, payload, correlation_id, actor=actor)
        except Exception:
            if created_album and not await self.repository.list_album_media(album.id):
                await self.repository.delete_album(album.id)
            raise
        album.updated_at = utcnow()
        if not album.title and title:
            album.title = title
        await self.repository.update_album(album)
        return UploadResult(album=album, media=media)

    async def _read_bounded_upload(self, file: UploadFile) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > self.settings.max_upload_bytes:
                raise HTTPException(status_code=413, detail="Upload exceeds V1 size limit.")
            chunks.append(chunk)
        return b"".join(chunks)

    async def _get_or_create_album(
        self,
        album_id: str | None,
        title: str | None,
        correlation_id: str,
        *,
        actor: CurrentActor,
        delete_token: str | None = None,
    ) -> Album:
        if album_id:
            album = await self.repository.get_album(album_id)
            if album is None:
                raise HTTPException(status_code=404, detail="Album not found.")
            if actor.user is not None:
                if album.user_id != actor.user.id:
                    raise HTTPException(status_code=403, detail="Album does not belong to authenticated user.")
            else:
                self._require_album_access(album, delete_token, actor.user)
            return album

        now = utcnow()
        album = Album(
            id=generate_album_id(),
            title=title,
            user_id=actor.user.id if actor.user else None,
            cover_media_id=None,
            delete_token=None if actor.user else secrets.token_urlsafe(24),
            created_at=now,
            updated_at=now,
            expires_at=None
            if actor.user
            else now + timedelta(hours=int(await self.runtime_config.get_value("anon_expiry_hours"))),
        )
        await self.repository.create_album(album)
        await self.event_bus.emit(
            AlbumCreated(
                album_id=album.id,
                user_id=actor.user.id if actor.user else None,
                item_count=0,
                actor_kind=self._actor_kind(actor.user, delete_token),
                source=actor.source,
                correlation_id=correlation_id,
            )
        )
        return album

    async def _create_media(
        self,
        album_id: str,
        file: UploadFile,
        payload: bytes,
        correlation_id: str,
        *,
        actor: CurrentActor,
    ) -> Media:
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

        try:
            validation = await processor.validate(payload)
            if not validation.ok:
                raise HTTPException(status_code=415, detail=validation.rejection_reason)
            metadata = await processor.extract_metadata(payload, fmt)
            sanitized = await processor.sanitize(payload, metadata)
        except HTTPException:
            raise
        except VideoProcessingError:
            detail = "Unsupported or invalid image file." if media_type == "image" else "Unsupported or invalid video file."
            raise HTTPException(status_code=415, detail=detail) from None
        payload = sanitized.data
        content_type = sanitized.mime_type
        fmt = sanitized.format
        suffix = f".{fmt if fmt != 'jpeg' else 'jpg'}"
        owner_segment = actor.user.id if actor.user else "anon"
        storage_key = f"originals/{owner_segment}/{media_id}{suffix}"
        await self.storage.put(storage_key, payload)
        position = await self.repository.next_position(album_id)

        media = Media(
            id=media_id,
            album_id=album_id,
            user_id=actor.user.id if actor.user else None,
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
                user_id=actor.user.id if actor.user else None,
                file_size=media.file_size,
                media_type=media.media_type,
                format=media.format,
                actor_kind=self._actor_kind(actor.user),
                source=actor.source,
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

        written_thumb_key: str | None = None
        try:
            processor = self.processors.get_processor(media.format)
            if processor is None:
                raise ValueError("processor_missing")
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
                written_thumb_key = thumb_key
                media.thumb_is_orig = False
                media.thumb_key = thumb_key
                media.thumb_size = thumbnail.size
            media.thumb_status = "done"
            await self.repository.update_media(media)
            return
        except Exception as exc:
            reason = self._thumbnail_failure_reason(exc)
            self._record_thumbnail_failure(
                reason=reason,
                media=media,
                correlation_id=correlation_id,
                error=exc,
            )
            media.thumb_status = "failed"
            media.thumb_key = None
            media.thumb_size = None
            media.thumb_is_orig = False
            if written_thumb_key is not None:
                try:
                    await self.storage.delete(written_thumb_key)
                except Exception as cleanup_exc:
                    self._record_thumbnail_failure(
                        reason="thumbnail_cleanup_failed",
                        media=media,
                        correlation_id=correlation_id,
                        error=cleanup_exc,
                    )
            try:
                await self.repository.update_media(media)
            except Exception as update_exc:
                if reason != "repository_update_failed":
                    self._record_thumbnail_failure(
                        reason="repository_update_failed",
                        media=media,
                        correlation_id=correlation_id,
                        error=update_exc,
                    )

    def _thumbnail_failure_reason(self, exc: Exception) -> str:
        if isinstance(exc, ValueError) and str(exc) == "processor_missing":
            return "processor_missing"
        message = str(exc).lower()
        if "extract" in message and "metadata" in message:
            return "metadata_extract_failed"
        if "thumbnail" in message and "cleanup" not in message:
            return "thumbnail_generate_failed"
        if "store" in message or "write thumb" in message or "thumbnail_store" in message:
            return "thumbnail_store_failed"
        if "read source" in message or "storage read" in message or "get_bytes" in message:
            return "storage_read_failed"
        if "update media" in message or "repository update" in message:
            return "repository_update_failed"
        return "thumbnail_generate_failed"

    def _record_thumbnail_failure(
        self,
        *,
        reason: str,
        media: Media,
        correlation_id: str,
        error: Exception,
    ) -> None:
        details = {
            "reason": reason,
            "media_id": media.id,
            "correlation_id": correlation_id,
            "storage_key": media.storage_key,
            "format": media.format,
        }
        logger.warning(
            "thumbnail_generation_failed",
            extra={**details, "error_type": type(error).__name__},
            exc_info=error,
        )
        if self.observability is not None:
            self.observability.record_task_failure(task_name="generate_thumbnail", details=details)

    async def delete_album(
        self,
        album_id: str,
        delete_token: str | None,
        correlation_id: str,
        *,
        actor_user: User | None = None,
    ) -> tuple[Album, list[Media]]:
        album = await self.repository.get_album(album_id)
        if album is None:
            raise HTTPException(status_code=404, detail="Album not found.")
        self._require_album_access(album, delete_token, actor_user)

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
                actor_id=actor_user.id if actor_user else None,
                actor_kind=self._actor_kind(actor_user, delete_token),
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
        actor_user: User | None = None,
        title: str | None | object = UNSET,
        cover_media_id: str | None | object = UNSET,
    ) -> tuple[Album, list[Media]]:
        album = await self.repository.get_album(album_id)
        if album is None:
            raise HTTPException(status_code=404, detail="Album not found.")
        self._require_album_access(album, delete_token, actor_user)

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
                        actor_id=actor_user.id if actor_user else None,
                        actor_kind=self._actor_kind(actor_user, delete_token),
                        old_title=old_title,
                        new_title=normalized_title,
                        source="api" if actor_user else "web",
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
                        actor_id=actor_user.id if actor_user else None,
                        actor_kind=self._actor_kind(actor_user, delete_token),
                        media_id=next_cover,
                        source="api" if actor_user else "web",
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
        *,
        actor_user: User | None = None,
    ) -> tuple[Album, list[Media]]:
        album = await self.repository.get_album(album_id)
        if album is None:
            raise HTTPException(status_code=404, detail="Album not found.")
        self._require_album_access(album, delete_token, actor_user)

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
                actor_id=actor_user.id if actor_user else None,
                actor_kind=self._actor_kind(actor_user, delete_token),
                source="api" if actor_user else "web",
                correlation_id=correlation_id,
            )
        )
        return album, reordered

    async def delete_media(
        self,
        media_id: str,
        delete_token: str | None,
        correlation_id: str,
        *,
        actor_user: User | None = None,
    ) -> MediaDeleteResult:
        media = await self.repository.get_media(media_id)
        if media is None:
            raise HTTPException(status_code=404, detail="Media not found.")

        album = await self.repository.get_album(media.album_id)
        if album is None:
            raise HTTPException(status_code=404, detail="Album not found.")
        self._require_album_access(album, delete_token, actor_user)

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
                actor_id=actor_user.id if actor_user else None,
                actor_kind=self._actor_kind(actor_user, delete_token),
                file_size=deleted_media.file_size + (deleted_media.thumb_size or 0),
                source="api" if actor_user else "web",
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
                        actor_id=actor_user.id if actor_user else None,
                        actor_kind=self._actor_kind(actor_user, delete_token),
                        item_count=0,
                        total_size=0,
                        source="api" if actor_user else "web",
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

    async def stream_album_zip(self, album_id: str) -> ZipStream:
        album = await self.repository.get_album(album_id)
        if album is None:
            raise HTTPException(status_code=404, detail="Album not found.")
        media_items = await self.repository.list_album_media(album_id)

        seen_names: set[str] = set()
        archive = ZipStream(compress_type=ZIP_DEFLATED)
        for index, media in enumerate(media_items, start=1):
            filename = self._archive_name(media, index, seen_names)
            archive.add(self._stream_storage_chunks(media.storage_key), filename)
        return archive

    def _stream_storage_chunks(self, storage_key: str) -> AsyncIterableBridge:
        async def factory() -> AsyncIterator[bytes]:
            stream = await self.storage.get_stream(storage_key)
            return stream.body

        return AsyncIterableBridge(factory)

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
                    actor_id=None,
                    actor_kind=self._actor_kind(None, system=True),
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

    def _require_album_access(self, album: Album, delete_token: str | None, actor_user: User | None) -> None:
        if actor_user is not None and (actor_user.is_admin or album.user_id == actor_user.id):
            return
        if album.delete_token is None:
            raise HTTPException(status_code=403, detail="Album access denied.")
        self._require_delete_token(album, delete_token)

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

    async def get_current_user_summary(self, user: User) -> dict[str, object]:
        return await self.accounts.get_current_user_summary(user)

    async def get_current_user_albums_page(
        self,
        user: User,
        *,
        base_url: str,
        limit: int = 10,
        offset: int = 0,
    ) -> dict[str, object]:
        albums, total = await self.repository.list_user_albums_page(user.id, limit=limit, offset=offset)
        media_by_album = await self.repository.list_media_for_album_ids([album.id for album in albums])
        items = [album_to_payload(base_url, album, media_by_album.get(album.id, [])) for album in albums]
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total,
        }

    async def issue_api_key(
        self,
        user: User,
        *,
        correlation_id: str | None = None,
        actor_id: str | None = None,
        source: str = "api",
    ) -> ApiKeyIssueResult:
        return await self.accounts.issue_api_key(
            user,
            correlation_id=correlation_id,
            actor_id=actor_id,
            source=source,
        )

    async def list_users_with_usage(self) -> list[dict[str, object]]:
        return await self.accounts.list_users_with_usage()

    async def list_users_with_usage_page(
        self,
        *,
        q: str | None = None,
        is_admin: bool | None = None,
        suspended: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, object]:
        return await self.accounts.list_users_with_usage_page(
            q=q,
            is_admin=is_admin,
            suspended=suspended,
            limit=limit,
            offset=offset,
        )

    async def get_user_with_usage_for_admin(self, user_id: str) -> dict[str, object]:
        return await self.accounts.get_user_with_usage_for_admin(user_id)

    async def get_user_storage_stats_for_admin(self, user_id: str) -> dict[str, object]:
        return await self.accounts.get_user_storage_stats_for_admin(user_id)

    async def create_user(
        self,
        payload: UserCreateInput,
        *,
        method: str = "admin",
        correlation_id: str | None = None,
        actor_id: str | None = None,
        source: str = "api",
    ) -> User:
        return await self.accounts.create_user(
            payload,
            method=method,
            correlation_id=correlation_id,
            actor_id=actor_id,
            source=source,
        )

    async def update_user(self, user_id: str, payload: UserUpdateInput, correlation_id: str, *, actor_id: str | None = None) -> User:
        return await self.accounts.update_user(user_id, payload, correlation_id, actor_id=actor_id)

    async def reset_user_password(self, user_id: str, new_password: str, correlation_id: str, *, actor_id: str | None = None) -> User:
        return await self.accounts.reset_user_password(user_id, new_password, correlation_id, actor_id=actor_id)

    async def change_password(
        self,
        user: User,
        payload: PasswordChangeInput,
        *,
        correlation_id: str | None = None,
        source: str = "api",
    ) -> User:
        return await self.accounts.change_password(
            user,
            payload,
            correlation_id=correlation_id,
            source=source,
        )

    async def authenticate_local_user(self, payload: LocalLoginInput) -> User:
        return await self.accounts.authenticate_local_user(payload)

    async def list_albums_for_admin(self) -> list[dict[str, object]]:
        albums = await self.repository.list_albums()
        users = {user.id: user for user in await self.repository.list_users()}
        payload: list[dict[str, object]] = []
        for album in albums:
            items = await self.repository.list_album_media(album.id)
            owner = users.get(album.user_id) if album.user_id else None
            payload.append(
                {
                    "id": album.id,
                    "title": album.title,
                    "user_id": album.user_id,
                    "owner_username": owner.username if owner else None,
                    "item_count": len(items),
                    "total_size": self._storage_bytes_for_media(items),
                    "created_at": album.created_at.isoformat(),
                    "updated_at": album.updated_at.isoformat(),
                    "expires_at": album.expires_at.isoformat() if album.expires_at else None,
                }
            )
        return payload

    async def list_albums_for_admin_page(
        self,
        *,
        q: str | None = None,
        owner: str | None = None,
        anonymous: bool | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> dict[str, object]:
        albums, total = await self.repository.list_albums_page(
            q=q,
            owner=owner,
            anonymous=anonymous,
            limit=limit,
            offset=offset,
        )
        users = {user.id: user for user in await self.repository.list_users()}
        items = []
        for album in albums:
            media_items = await self.repository.list_album_media(album.id)
            owner_user = users.get(album.user_id) if album.user_id else None
            items.append(
                {
                    "id": album.id,
                    "title": album.title,
                    "user_id": album.user_id,
                    "owner_username": owner_user.username if owner_user else None,
                    "item_count": len(media_items),
                    "total_size": self._storage_bytes_for_media(media_items),
                    "created_at": album.created_at.isoformat(),
                    "updated_at": album.updated_at.isoformat(),
                    "expires_at": album.expires_at.isoformat() if album.expires_at else None,
                }
            )
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total,
        }

    async def list_albums_for_user_admin_view(self, user_id: str) -> list[dict[str, object]]:
        page = await self.list_albums_for_user_admin_page(user_id, base_url="", limit=1000, offset=0)
        return page["items"]

    async def list_albums_for_user_admin_page(
        self,
        user_id: str,
        *,
        base_url: str,
        limit: int = 10,
        offset: int = 0,
    ) -> dict[str, object]:
        user = await self.repository.get_user(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found.")

        albums, total = await self.repository.list_user_albums_page(user.id, limit=limit, offset=offset)
        media_by_album = await self.repository.list_media_for_album_ids([album.id for album in albums])
        payload: list[dict[str, object]] = []
        for album in albums:
            items = media_by_album.get(album.id, [])
            album_payload = album_to_payload(base_url, album, items)
            album_payload["user_id"] = album.user_id
            album_payload["owner_username"] = user.username
            payload.append(album_payload)
        return {
            "items": payload,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(payload) < total,
        }

    async def list_public_albums_for_username(self, username: str) -> tuple[User, list[dict[str, object]]]:
        user = await self.repository.get_user_by_username(username)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found.")

        albums = await self.repository.list_user_albums(user.id)
        visible = [album for album in albums if album.expires_at is None or album.expires_at > utcnow()]
        visible.sort(key=lambda album: album.updated_at, reverse=True)

        payload: list[dict[str, object]] = []
        for album in visible:
            items = await self.repository.list_album_media(album.id)
            cover = None
            if album.cover_media_id:
                cover = next((item for item in items if item.id == album.cover_media_id), None)
            if cover is None and items:
                cover = items[0]
            payload.append(
                {
                    "id": album.id,
                    "title": album.title,
                    "item_count": len(items),
                    "total_size": sum(item.file_size for item in items),
                    "created_at": album.created_at.isoformat(),
                    "updated_at": album.updated_at.isoformat(),
                    "cover_media_id": cover.id if cover else None,
                    "cover_format": cover.format if cover else None,
                    "cover_thumb_format": (cover.thumb_key.rsplit(".", 1)[-1].lower() if cover and cover.thumb_key and not cover.thumb_is_orig else cover.format if cover else None),
                    "cover_thumb_status": cover.thumb_status if cover else None,
                }
            )
        return user, payload

    async def admin_update_album(
        self,
        album_id: str,
        payload: AdminAlbumUpdateInput,
        correlation_id: str,
        *,
        actor_id: str | None = None,
    ) -> Album:
        album = await self.repository.get_album(album_id)
        if album is None:
            raise HTTPException(status_code=404, detail="Album not found.")

        if payload.expires_at is not UNSET:
            old_expiry = album.expires_at.isoformat() if album.expires_at else None
            new_expiry = payload.expires_at.isoformat() if payload.expires_at else None
            if old_expiry != new_expiry:
                album.expires_at = payload.expires_at
                album.updated_at = utcnow()
                await self.repository.update_album(album)
                await self.event_bus.emit(
                    AlbumExpiryChanged(
                        album_id=album.id,
                        user_id=album.user_id,
                        actor_id=actor_id,
                        actor_kind="admin",
                        old_expiry=old_expiry,
                        new_expiry=new_expiry,
                        source="api",
                        correlation_id=correlation_id,
                    )
                )
        return album

    async def global_storage_stats(self) -> dict[str, object]:
        return await self.accounts.global_storage_stats()

    async def delete_user_account(self, user: User, correlation_id: str) -> dict[str, int]:
        return await self.accounts.delete_user_account(user, correlation_id)

    async def delete_user_by_id(
        self,
        user_id: str,
        correlation_id: str,
        *,
        deleted_by: str,
        actor_id: str | None = None,
    ) -> dict[str, int]:
        return await self.accounts.delete_user_by_id(
            user_id,
            correlation_id,
            deleted_by=deleted_by,
            actor_id=actor_id,
        )

    async def _enforce_storage_quotas(self, user: User | None, *, incoming_bytes: int) -> None:
        all_media = await self.repository.list_all_media()
        total_storage = self._storage_bytes_for_media(all_media)
        if self.settings.server_quota_bytes > 0 and total_storage + incoming_bytes > self.settings.server_quota_bytes:
            raise HTTPException(status_code=507, detail="Server storage quota reached.")
        if user is None:
            return

        user_media = [media for media in all_media if media.user_id == user.id]
        user_storage = self._storage_bytes_for_media(user_media)
        effective_quota = user.quota_bytes if user.quota_bytes is not None else self.settings.default_user_quota_bytes
        if effective_quota > 0 and user_storage + incoming_bytes > effective_quota:
            raise HTTPException(status_code=413, detail="User storage quota reached.")

    def _hash_password(self, password: str) -> str:
        return self.accounts._hash_password(password)

    def _verify_password(self, password: str, password_hash: str) -> bool:
        return self.accounts._verify_password(password, password_hash)

    def _hash_api_key(self, raw_key: str) -> str:
        return self.accounts._hash_api_key(raw_key)

    async def complete_oauth_login(
        self,
        identity: OAuthIdentity,
        *,
        current_user: User | None,
        allow_registration: bool,
        correlation_id: str | None = None,
        source: str = "web",
    ) -> tuple[User, str]:
        return await self.accounts.complete_oauth_login(
            identity,
            current_user=current_user,
            allow_registration=allow_registration,
            correlation_id=correlation_id,
            source=source,
        )

    async def disconnect_oauth_provider(self, user: User, provider: str) -> None:
        await self.accounts.disconnect_oauth_provider(user, provider)
