from __future__ import annotations

import mimetypes
import secrets
from hashlib import sha256
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import HTTPException, UploadFile

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
    UserDeleted,
    UserPasswordReset,
    UserRegistered,
    UserSuspended,
)
from .ids import generate_album_id, generate_media_id
from .models import Album, ApiKey, Media, User, utcnow
from .processors import ProcessorRegistry
from .rate_limits import InMemoryRateLimiter
from .repositories import JsonRepository
from .runtime_config import JsonRuntimeConfig
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


@dataclass
class CurrentActor:
    user: User | None
    source: str


@dataclass
class ApiKeyIssueResult:
    api_key: ApiKey
    raw_key: str


@dataclass
class UserCreateInput:
    username: str
    email: str
    password: str | None
    is_admin: bool
    quota_bytes: int | None
    rate_limit_rpm: int | None = None
    rate_limit_bph: int | None = None


@dataclass
class UserUpdateInput:
    suspended: bool | None = None
    quota_bytes: int | None | object = UNSET
    rate_limit_rpm: int | None | object = UNSET
    rate_limit_bph: int | None | object = UNSET
    password: str | None = None


@dataclass
class PasswordChangeInput:
    current_password: str
    new_password: str


@dataclass
class LocalLoginInput:
    login: str
    password: str


@dataclass
class AdminAlbumUpdateInput:
    expires_at: object = UNSET


class UploadService:
    def __init__(
        self,
        settings: Settings,
        repository: JsonRepository,
        storage: LocalFilesystemBackend,
        event_bus: EventBus,
        processors: ProcessorRegistry,
        runtime_config: JsonRuntimeConfig,
        rate_limiter: InMemoryRateLimiter,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.storage = storage
        self.event_bus = event_bus
        self.processors = processors
        self.runtime_config = runtime_config
        self.rate_limiter = rate_limiter

    async def upload(
        self,
        file: UploadFile,
        album_id: str | None,
        title: str | None,
        correlation_id: str,
        *,
        actor: CurrentActor | None = None,
        rate_limit_key: str | None = None,
    ) -> UploadResult:
        payload = await file.read()
        if not payload:
            raise HTTPException(status_code=400, detail="Empty file upload.")
        if len(payload) > self.settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail="Upload exceeds V1 size limit.")

        actor = actor or CurrentActor(user=None, source="web")
        if rate_limit_key:
            await self.rate_limiter.enforce_upload_limits(
                actor_key=rate_limit_key,
                byte_count=len(payload),
                user=actor.user,
            )
        await self._enforce_storage_quotas(actor.user, incoming_bytes=len(payload))
        album = await self._get_or_create_album(
            album_id=album_id,
            title=title,
            correlation_id=correlation_id,
            actor=actor,
        )
        if len(await self.repository.list_album_media(album.id)) >= MAX_ALBUM_ITEMS:
            raise HTTPException(status_code=413, detail="Album item limit reached.")
        media = await self._create_media(album.id, file, payload, correlation_id, actor=actor)
        album.updated_at = utcnow()
        if not album.title and title:
            album.title = title
        await self.repository.update_album(album)
        return UploadResult(album=album, media=media)

    async def _get_or_create_album(
        self,
        album_id: str | None,
        title: str | None,
        correlation_id: str,
        *,
        actor: CurrentActor,
    ) -> Album:
        if album_id:
            album = await self.repository.get_album(album_id)
            if album is None:
                raise HTTPException(status_code=404, detail="Album not found.")
            if actor.user is not None and album.user_id != actor.user.id:
                raise HTTPException(status_code=403, detail="Album does not belong to authenticated user.")
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

        validation = await processor.validate(payload)
        if not validation.ok:
            raise HTTPException(status_code=415, detail=validation.rejection_reason)
        metadata = await processor.extract_metadata(payload, fmt)
        sanitized = await processor.sanitize(payload, metadata)
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
                        actor_id=None,
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
                        actor_id=None,
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
                actor_id=None,
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
                actor_id=None,
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
                        actor_id=None,
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
                    actor_id=None,
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
        items = await self.repository.list_user_media(user.id)
        usage = self._storage_bytes_for_media(items)
        api_key = await self.repository.get_api_key_for_user(user.id)
        effective_quota = user.quota_bytes if user.quota_bytes is not None else self.settings.default_user_quota_bytes
        return {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "quota_bytes": effective_quota,
            "storage_used_bytes": usage,
            "has_api_key": api_key is not None,
            "api_key_last_used_at": api_key.last_used_at.isoformat() if api_key and api_key.last_used_at else None,
        }

    async def issue_api_key(self, user: User) -> ApiKeyIssueResult:
        raw_key = secrets.token_hex(16)
        api_key = ApiKey(
            id=str(uuid4()),
            user_id=user.id,
            key_hash=sha256(raw_key.encode("utf-8")).hexdigest(),
            created_at=utcnow(),
            last_used_at=None,
        )
        await self.repository.upsert_api_key(api_key)
        return ApiKeyIssueResult(api_key=api_key, raw_key=raw_key)

    async def list_users_with_usage(self) -> list[dict[str, object]]:
        users = await self.repository.list_users()
        all_media = await self.repository.list_all_media()
        usage_by_user: dict[str, int] = {}
        count_by_user: dict[str, int] = {}
        for media in all_media:
            if media.user_id is None:
                continue
            usage_by_user[media.user_id] = usage_by_user.get(media.user_id, 0) + media.file_size
            if media.thumb_key and media.thumb_key != media.storage_key:
                usage_by_user[media.user_id] += media.thumb_size or 0
            count_by_user[media.user_id] = count_by_user.get(media.user_id, 0) + 1

        return [
            {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "is_admin": user.is_admin,
                "suspended": user.suspended,
                "quota_bytes": user.quota_bytes if user.quota_bytes is not None else self.settings.default_user_quota_bytes,
                "rate_limit_rpm": user.rate_limit_rpm,
                "rate_limit_bph": user.rate_limit_bph,
                "storage_used_bytes": usage_by_user.get(user.id, 0),
                "media_count": count_by_user.get(user.id, 0),
                "created_at": user.created_at.isoformat(),
            }
            for user in users
        ]

    async def create_user(
        self,
        payload: UserCreateInput,
        *,
        method: str = "admin",
        correlation_id: str | None = None,
        actor_id: str | None = None,
        source: str = "api",
    ) -> User:
        username = payload.username.strip()
        email = payload.email.strip().lower()
        if not username:
            raise HTTPException(status_code=400, detail="Username is required.")
        if not email:
            raise HTTPException(status_code=400, detail="Email is required.")
        if payload.password is not None and not payload.password.strip():
            raise HTTPException(status_code=400, detail="Password is required.")
        if await self.repository.get_user_by_username(username):
            raise HTTPException(status_code=409, detail="Username already exists.")
        if await self.repository.get_user_by_email(email):
            raise HTTPException(status_code=409, detail="Email already exists.")

        now = utcnow()
        user = User(
            id=str(uuid4()),
            username=username,
            email=email,
            password_hash=self._hash_password(payload.password) if payload.password else None,
            is_admin=payload.is_admin,
            suspended=False,
            quota_bytes=payload.quota_bytes,
            rate_limit_rpm=payload.rate_limit_rpm,
            rate_limit_bph=payload.rate_limit_bph,
            created_at=now,
            updated_at=now,
        )
        await self.repository.create_user(user)
        if correlation_id is not None:
            await self.event_bus.emit(
                UserRegistered(
                    user_id=user.id,
                    actor_id=actor_id if actor_id is not None else (user.id if method == "registration" else None),
                    method=method,
                    source=source,
                    correlation_id=correlation_id,
                )
            )
        return user

    async def update_user(self, user_id: str, payload: UserUpdateInput, correlation_id: str, *, actor_id: str | None = None) -> User:
        user = await self.repository.get_user(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found.")

        if payload.suspended is not None and payload.suspended != user.suspended:
            user.suspended = payload.suspended
            await self.event_bus.emit(
                UserSuspended(
                    user_id=user.id,
                    actor_id=actor_id,
                    suspended=user.suspended,
                    source="api",
                    correlation_id=correlation_id,
                )
            )
        if payload.quota_bytes is not UNSET:
            user.quota_bytes = payload.quota_bytes if payload.quota_bytes is None or isinstance(payload.quota_bytes, int) else None
        if payload.rate_limit_rpm is not UNSET:
            user.rate_limit_rpm = (
                payload.rate_limit_rpm if payload.rate_limit_rpm is None or isinstance(payload.rate_limit_rpm, int) else None
            )
        if payload.rate_limit_bph is not UNSET:
            user.rate_limit_bph = (
                payload.rate_limit_bph if payload.rate_limit_bph is None or isinstance(payload.rate_limit_bph, int) else None
            )
        if payload.password is not None:
            user.password_hash = self._hash_password(payload.password)
        user.updated_at = utcnow()
        await self.repository.update_user(user)
        return user

    async def reset_user_password(self, user_id: str, new_password: str, correlation_id: str, *, actor_id: str | None = None) -> User:
        user = await self.repository.get_user(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found.")
        if not new_password.strip():
            raise HTTPException(status_code=400, detail="New password is required.")

        user.password_hash = self._hash_password(new_password)
        user.updated_at = utcnow()
        await self.repository.update_user(user)
        await self.event_bus.emit(
            UserPasswordReset(
                user_id=user.id,
                actor_id=actor_id,
                source="api",
                correlation_id=correlation_id,
            )
        )
        return user

    async def change_password(self, user: User, payload: PasswordChangeInput) -> User:
        if user.password_hash is None:
            raise HTTPException(status_code=400, detail="Password login is not configured for this user.")
        if self._hash_password(payload.current_password) != user.password_hash:
            raise HTTPException(status_code=403, detail="Current password is incorrect.")
        if not payload.new_password.strip():
            raise HTTPException(status_code=400, detail="New password is required.")
        user.password_hash = self._hash_password(payload.new_password)
        user.updated_at = utcnow()
        await self.repository.update_user(user)
        return user

    async def authenticate_local_user(self, payload: LocalLoginInput) -> User:
        login = payload.login.strip()
        if not login or not payload.password:
            raise HTTPException(status_code=400, detail="Login and password are required.")

        user = await self.repository.get_user_by_email(login.lower())
        if user is None:
            user = await self.repository.get_user_by_username(login)
        if user is None or user.password_hash is None:
            raise HTTPException(status_code=401, detail="Invalid credentials.")
        if user.suspended:
            raise HTTPException(status_code=403, detail="User is not allowed to authenticate.")
        if self._hash_password(payload.password) != user.password_hash:
            raise HTTPException(status_code=401, detail="Invalid credentials.")
        return user

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
                        old_expiry=old_expiry,
                        new_expiry=new_expiry,
                        source="api",
                        correlation_id=correlation_id,
                    )
                )
        return album

    async def global_storage_stats(self) -> dict[str, object]:
        all_media = await self.repository.list_all_media()
        total_storage = self._storage_bytes_for_media(all_media)
        anonymous_storage = self._storage_bytes_for_media([item for item in all_media if item.user_id is None])
        return {
            "server_quota_bytes": self.settings.server_quota_bytes,
            "total_storage_used_bytes": total_storage,
            "anonymous_storage_used_bytes": anonymous_storage,
            "user_count": len(await self.repository.list_users()),
            "users": await self.list_users_with_usage(),
        }

    async def delete_user_account(self, user: User, correlation_id: str) -> dict[str, int]:
        return await self.delete_user_by_id(user.id, correlation_id, deleted_by="self", actor_id=user.id)

    async def delete_user_by_id(
        self,
        user_id: str,
        correlation_id: str,
        *,
        deleted_by: str,
        actor_id: str | None = None,
    ) -> dict[str, int]:
        user = await self.repository.get_user(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found.")

        albums = await self.repository.list_user_albums(user.id)
        media_items = await self.repository.list_user_media(user.id)

        for key in self._storage_keys_for_media(media_items):
            await self.storage.delete(key)

        deleted_user, deleted_albums, deleted_media = await self.repository.delete_user(user.id)
        if deleted_user is None:
            raise HTTPException(status_code=404, detail="User not found.")

        await self.event_bus.emit(
            UserDeleted(
                user_id=deleted_user.id,
                actor_id=actor_id,
                deleted_by=deleted_by,
                album_count=len(deleted_albums),
                media_count=len(deleted_media),
                source="api",
                correlation_id=correlation_id,
            )
        )
        return {
            "album_count": len(albums),
            "media_count": len(media_items),
        }

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
        return sha256(password.encode("utf-8")).hexdigest()
