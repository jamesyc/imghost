import asyncio
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile
from uuid import uuid4

import bcrypt
import pytest

from fastapi import HTTPException
from imghost.config import Settings
from imghost.events import ApiKeyIssued, UserAdminStatusChanged, UserLimitsChanged, UserPasswordChanged
from imghost.models import Album, ApiKey, Media, User, UserSsoLink, utcnow
from imghost.processors import MediaMetadata, ThumbnailResult
from imghost.storage import StorageStream
from imghost.payloads import album_to_payload
from imghost.service import CurrentActor, LocalLoginInput, PasswordChangeInput, UNSET, UploadService, UserCreateInput, UserUpdateInput
from imghost.tasks import TASK_STATE_FAILED_EXHAUSTED, TASK_STATE_RETRY_SCHEDULED


class DummyRepository:
    def __init__(self, user: User | None = None) -> None:
        self.user = user
        self.updated_user: User | None = None
        self.created_user: User | None = None
        self.api_key: ApiKey | None = None
        self.album: Album | None = None
        self.album_media: list[object] = []
        self.user_media: list[Media] = []
        self.user_albums: list[Album] = []
        self.user_sso_links: list[UserSsoLink] = []
        self.media: Media | None = None
        self.update_media_calls = 0
        self.fail_update_media_on_call: int | None = None
        self.deleted_expired_sharex_delete_capabilities = 0
        self.deleted_consumed_sharex_delete_capabilities = 0
        self.deleted_revoked_sharex_delete_capabilities = 0

    async def get_user_by_email(self, email: str) -> User | None:
        if self.user and self.user.email == email:
            return self.user
        return None

    async def get_user_by_username(self, username: str) -> User | None:
        if self.user and self.user.username == username:
            return self.user
        return None

    async def update_user(self, user: User) -> User:
        self.updated_user = user
        self.user = user
        return user

    async def create_user(self, user: User) -> User:
        self.created_user = user
        self.user = user
        return user

    async def get_user(self, user_id: str) -> User | None:
        if self.user and self.user.id == user_id:
            return self.user
        return None

    async def get_album(self, album_id: str) -> Album | None:
        if self.album and self.album.id == album_id:
            return self.album
        return None

    async def list_album_media(self, album_id: str) -> list[object]:
        if self.album and self.album.id == album_id:
            return list(self.album_media)
        return []

    async def get_api_key_for_user(self, user_id: str) -> ApiKey | None:
        if self.api_key and self.api_key.user_id == user_id:
            return self.api_key
        return None

    async def list_user_media(self, user_id: str) -> list[Media]:
        if self.user and self.user.id == user_id:
            return list(self.user_media)
        return []

    async def list_user_albums(self, user_id: str) -> list[Album]:
        if self.user and self.user.id == user_id:
            return list(self.user_albums)
        return []

    async def list_user_sso_links(self, user_id: str) -> list[UserSsoLink]:
        if self.user and self.user.id == user_id:
            return list(self.user_sso_links)
        return []

    async def upsert_api_key(self, api_key: ApiKey) -> ApiKey:
        self.api_key = api_key
        return api_key

    async def get_media(self, media_id: str) -> Media | None:
        if self.media and self.media.id == media_id:
            return self.media
        return None

    async def update_media(self, media: Media) -> Media:
        self.update_media_calls += 1
        if self.fail_update_media_on_call == self.update_media_calls:
            raise RuntimeError("repository update failed")
        self.media = media
        return media

    async def delete_expired_sharex_delete_capabilities(self, now) -> int:
        return self.deleted_expired_sharex_delete_capabilities

    async def delete_consumed_sharex_delete_capabilities_older_than(self, cutoff) -> int:
        return self.deleted_consumed_sharex_delete_capabilities

    async def delete_revoked_sharex_delete_capabilities_older_than(self, cutoff) -> int:
        return self.deleted_revoked_sharex_delete_capabilities


class DummyStorage:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.get_bytes_called = False
        self.put_calls: list[tuple[str, bytes]] = []
        self.delete_calls: list[str] = []
        self.fail_get_bytes = False
        self.fail_put = False
        self.fail_delete = False

    async def get_stream(self, key: str, range_header: str | None = None) -> StorageStream:
        data = self.payloads[key]

        async def iterator():
            midpoint = max(1, len(data) // 2)
            yield data[:midpoint]
            if midpoint < len(data):
                yield data[midpoint:]

        return StorageStream(
            status_code=200,
            content_type="application/octet-stream",
            content_length=len(data),
            content_range=None,
            body=iterator(),
        )

    async def get_bytes(self, key: str) -> bytes:
        self.get_bytes_called = True
        if self.fail_get_bytes:
            raise RuntimeError("storage read failed")
        return self.payloads[key]

    async def put(self, key: str, data: bytes) -> None:
        if self.fail_put:
            raise RuntimeError("thumbnail_store_failed")
        self.put_calls.append((key, data))
        self.payloads[key] = data

    async def delete(self, key: str) -> None:
        self.delete_calls.append(key)
        if self.fail_delete:
            raise RuntimeError("thumbnail cleanup failed")
        self.payloads.pop(key, None)


class DummyProcessors:
    def __init__(self, processor=None) -> None:
        self.processor = processor

    def get_processor(self, format_name: str):
        return self.processor


class DummyRuntimeConfig:
    def __init__(self, values: dict[str, int | bool] | None = None) -> None:
        self.values: dict[str, int | bool] = {
            "max_upload_bytes": 50 * 1024 * 1024,
            "anon_expiry_hours": 24,
            "video_thumb_frames": 10,
            "default_user_quota_bytes": 2 * 1024 * 1024 * 1024,
            "server_quota_bytes": 0,
        }
        if values:
            self.values.update(values)

    async def get_value(self, key: str) -> int | bool:
        return self.values[key]


class DummyProcessor:
    def __init__(self) -> None:
        self.metadata = MediaMetadata(
            width=640,
            height=360,
            duration_secs=1.0,
            codec_hint=None,
            is_animated=True,
            mime_type="video/mp4",
            format="mp4",
        )
        self.thumbnail = ThumbnailResult(data=b"thumb-bytes", thumb_is_orig=False, format="jpg", size=len(b"thumb-bytes"))
        self.extract_error: Exception | None = None
        self.generate_error: Exception | None = None

    async def extract_metadata(self, payload: bytes, format_hint: str) -> MediaMetadata:
        if self.extract_error is not None:
            raise self.extract_error
        return self.metadata

    async def generate_thumbnail(self, payload: bytes, metadata: MediaMetadata) -> ThumbnailResult:
        if self.generate_error is not None:
            raise self.generate_error
        return self.thumbnail


class DummyEventBus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def emit(self, event: object) -> None:
        self.events.append(event)


class DummyTelemetryService:
    async def emit_event(self, **kwargs) -> None:
        return None

    async def count_audit_events_older_than(self, before):
        return 0

    async def delete_audit_events_older_than(self, before):
        return 0

    async def query_audit_log(self, **kwargs):
        return []


class RecordingTelemetry:
    def __init__(self) -> None:
        self.last_task_failure_at: float | None = None
        self.last_task_failure: dict[str, object] | None = None
        self.last_task_event: dict[str, object] | None = None
        self.thumbnail_jobs: list[dict[str, object]] = []
        self.audit_count_before = None
        self.audit_delete_before = None
        self.audit_count_result = 0
        self.audit_delete_result = 0

    def record_task_state(self, *, task_name: str, state: str, details: dict[str, object]) -> None:
        self.last_task_event = {"task_name": task_name, "state": state, **details}

    def record_thumbnail_failure(self, *, media: Media, correlation_id: str, reason: str, error: Exception) -> None:
        self.last_task_failure = {
            "task_name": "generate_thumbnail",
            "reason": reason,
            "media_id": media.id,
            "correlation_id": correlation_id,
            "storage_key": media.storage_key,
            "format": media.format,
            "error_type": type(error).__name__,
        }

    def record_thumbnail_job(
        self,
        *,
        result: str,
        media_type: str,
        reason: str | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        self.thumbnail_jobs.append(
            {
                "result": result,
                "media_type": media_type,
                "reason": reason,
                "duration_seconds": duration_seconds,
            }
        )

    async def count_audit_events_older_than(self, before) -> int:
        self.audit_count_before = before
        return self.audit_count_result

    async def delete_audit_events_older_than(self, before) -> int:
        self.audit_delete_before = before
        return self.audit_delete_result


def make_service(
    user: User | None = None,
    *,
    storage=None,
    processors=None,
    runtime_values: dict[str, int | bool] | None = None,
) -> tuple[UploadService, DummyRepository, DummyEventBus, RecordingTelemetry]:
    repository = DummyRepository(user)
    event_bus = DummyEventBus()
    telemetry = RecordingTelemetry()
    runtime_config = DummyRuntimeConfig(runtime_values)
    settings = Settings(
        base_url="http://testserver",
        public_origin_enabled=True,
        trusted_public_origins=("http://testserver",),
        trusted_proxy_cidrs_enabled=False,
        trusted_proxy_cidrs=(),
        database_url="postgresql://test",
        data_dir=Path("/tmp/imghost-test"),
        redis_url=None,
        redis_password=None,
        redis_mode="auto",
        redis_prefix="imghost",
        storage_backend="filesystem",
        s3_endpoint_url=None,
        s3_access_key_id=None,
        s3_secret_access_key=None,
        s3_bucket=None,
        s3_region="garage",
        secret_key="secret",
        session_cookie_name="imghost_session",
        session_cookie_secure=False,
        session_redis_fail_closed=False,
        session_remember_days=30,
        max_upload_bytes=50 * 1024 * 1024,
        anon_expiry_hours=24,
        max_pixel_megapixels=50,
        default_user_quota_bytes=2 * 1024 * 1024 * 1024,
        server_quota_bytes=0,
        video_thumb_frames=10,
        task_queue_mode="async",
        task_worker_enabled=True,
        thumbnail_worker_count=1,
        app_scheduler_enabled=False,
    )
    service = UploadService(
        settings=settings,
        repository=repository,
        storage=storage,  # type: ignore[arg-type]
        event_bus=event_bus,  # type: ignore[arg-type]
        processors=processors,  # type: ignore[arg-type]
        runtime_config=runtime_config,  # type: ignore[arg-type]
        rate_limiter=None,  # type: ignore[arg-type]
        telemetry=telemetry,
    )
    return service, repository, event_bus, telemetry


def make_media(*, media_type: str = "video", format: str = "mp4", storage_key: str = "originals/u/media.mp4") -> Media:
    return Media(
        id="media-1",
        album_id="album-1",
        user_id="user-1",
        filename_orig="sample.mp4",
        media_type=media_type,
        format=format,
        mime_type="video/mp4",
        storage_key=storage_key,
        thumb_key=None,
        thumb_is_orig=False,
        thumb_status="pending",
        file_size=123,
        thumb_size=None,
        width=640,
        height=360,
        duration_secs=1.0,
        is_animated=True,
        codec_hint=None,
        position=1000,
        created_at=utcnow(),
    )


def make_user(*, password_hash: str) -> User:
    now = utcnow()
    return User(
        id="user-1",
        username="alice",
        email="alice@example.com",
        password_hash=password_hash,
        is_admin=False,
        suspended=False,
        quota_bytes=None,
        rate_limit_rpm=None,
        rate_limit_bph=None,
        created_at=now,
        updated_at=now,
    )


def test_prune_expired_albums_dry_run_counts_stale_audit_events_without_deleting() -> None:
    service, repository, _, telemetry = make_service()
    expired_at = utcnow()
    album = Album(
        id="album-1",
        title=None,
        user_id=None,
        cover_media_id=None,
        delete_token="token",
        created_at=expired_at,
        updated_at=expired_at,
        expires_at=expired_at,
    )
    async def _list_expired_albums(now):
        return [album]

    media_items = [
        Media(
            id="media-1",
            album_id="album-1",
            user_id=None,
            filename_orig="sample.png",
            media_type="image",
            format="png",
            mime_type="image/png",
            storage_key="originals/anon/media-1.png",
            thumb_key="thumbnails/media-1.jpg",
            thumb_is_orig=False,
            thumb_status="done",
            file_size=100,
            thumb_size=20,
            width=1,
            height=1,
            duration_secs=None,
            is_animated=False,
            codec_hint=None,
            position=1000,
            created_at=expired_at,
        )
    ]
    async def _list_album_media(album_id):
        return list(media_items) if album_id == "album-1" else []

    repository.list_expired_albums = _list_expired_albums  # type: ignore[method-assign]
    repository.list_album_media = _list_album_media  # type: ignore[method-assign]
    telemetry.audit_count_result = 4

    result = asyncio.run(service.prune_expired_albums(dry_run=True))

    assert result.dry_run is True
    assert result.album_ids == ["album-1"]
    assert result.item_count == 1
    assert result.bytes_freed == 120
    assert result.audit_event_count == 4
    assert telemetry.audit_count_before is not None
    assert telemetry.audit_delete_before is None


def test_prune_expired_albums_deletes_stale_audit_events_even_without_expired_albums() -> None:
    service, repository, _, telemetry = make_service()
    async def _list_expired_albums(now):
        return []

    repository.list_expired_albums = _list_expired_albums  # type: ignore[method-assign]
    telemetry.audit_delete_result = 9

    result = asyncio.run(service.prune_expired_albums(dry_run=False))

    assert result.dry_run is False
    assert result.album_ids == []
    assert result.item_count == 0
    assert result.bytes_freed == 0
    assert result.audit_event_count == 9
    assert telemetry.audit_delete_before is not None


def test_hash_password_uses_bcrypt_and_is_not_deterministic() -> None:
    service, _, _, _ = make_service()

    first = service._hash_password("secret-pass")
    second = service._hash_password("secret-pass")

    assert first != second
    assert first.startswith("$2")
    assert second.startswith("$2")
    assert bcrypt.checkpw(b"secret-pass", first.encode("utf-8"))
    assert bcrypt.checkpw(b"secret-pass", second.encode("utf-8"))


def test_authenticate_local_user_accepts_bcrypt_hash() -> None:
    password_hash = bcrypt.hashpw(b"secret-pass", bcrypt.gensalt()).decode("utf-8")
    service, _, _, _ = make_service(make_user(password_hash=password_hash))

    user = asyncio.run(
        service.authenticate_local_user(LocalLoginInput(login="alice@example.com", password="secret-pass"))
    )

    assert user.username == "alice"


def test_change_password_replaces_hash_with_new_bcrypt_hash() -> None:
    old_hash = bcrypt.hashpw(b"old-pass", bcrypt.gensalt()).decode("utf-8")
    user = make_user(password_hash=old_hash)
    service, repository, _, _ = make_service(user)

    updated = asyncio.run(
        service.change_password(
            user,
            PasswordChangeInput(current_password="old-pass", new_password="new-pass"),
        )
    )

    assert repository.updated_user is not None
    assert updated.password_hash != old_hash
    assert bcrypt.checkpw(b"new-pass", updated.password_hash.encode("utf-8"))
    assert not bcrypt.checkpw(b"old-pass", updated.password_hash.encode("utf-8"))


def test_change_password_emits_audit_event_when_correlation_is_provided() -> None:
    old_hash = bcrypt.hashpw(b"old-pass", bcrypt.gensalt()).decode("utf-8")
    user = make_user(password_hash=old_hash)
    service, _, event_bus, _ = make_service(user)

    asyncio.run(
        service.change_password(
            user,
            PasswordChangeInput(current_password="old-pass", new_password="new-pass"),
            correlation_id="pw-change",
        )
    )

    assert any(isinstance(event, UserPasswordChanged) for event in event_bus.events)
    changed = next(event for event in event_bus.events if isinstance(event, UserPasswordChanged))
    assert changed.user_id == user.id
    assert changed.actor_id == user.id
    assert changed.correlation_id == "pw-change"


def test_create_user_rejects_password_shorter_than_eight_characters() -> None:
    service, repository, _, _ = make_service()

    with pytest.raises(HTTPException) as rejected:
        asyncio.run(
            service.create_user(
                UserCreateInput(
                    username="shortpass",
                    email="shortpass@example.com",
                    password="short7!",
                    is_admin=False,
                    quota_bytes=None,
                )
            )
        )

    assert rejected.value.status_code == 400
    assert rejected.value.detail == "Password must be at least 8 characters."
    assert repository.created_user is None


def test_reset_user_password_rejects_password_shorter_than_eight_characters() -> None:
    old_hash = bcrypt.hashpw(b"old-pass", bcrypt.gensalt()).decode("utf-8")
    user = make_user(password_hash=old_hash)
    service, repository, event_bus, _ = make_service(user)

    with pytest.raises(HTTPException) as rejected:
        asyncio.run(service.reset_user_password(user.id, "short7!", "reset-short"))

    assert rejected.value.status_code == 400
    assert rejected.value.detail == "New password must be at least 8 characters."
    assert repository.updated_user is None
    assert event_bus.events == []


def test_change_password_rejects_password_shorter_than_eight_characters() -> None:
    old_hash = bcrypt.hashpw(b"old-pass", bcrypt.gensalt()).decode("utf-8")
    user = make_user(password_hash=old_hash)
    service, repository, _, _ = make_service(user)

    with pytest.raises(HTTPException) as rejected:
        asyncio.run(
            service.change_password(
                user,
                PasswordChangeInput(current_password="old-pass", new_password="short7!"),
            )
        )

    assert rejected.value.status_code == 400
    assert rejected.value.detail == "New password must be at least 8 characters."
    assert repository.updated_user is None


def test_verify_password_returns_false_for_invalid_stored_hash() -> None:
    service, _, _, _ = make_service()

    assert service._verify_password("secret-pass", "not-a-bcrypt-hash") is False


def test_issue_api_key_emits_event_with_replaced_existing_flag() -> None:
    old_hash = bcrypt.hashpw(b"old-pass", bcrypt.gensalt()).decode("utf-8")
    user = make_user(password_hash=old_hash)
    service, repository, event_bus, _ = make_service(user)
    repository.api_key = ApiKey(
        id=str(uuid4()),
        user_id=user.id,
        key_hash="old-hash",
        created_at=utcnow(),
        last_used_at=None,
    )

    issued = asyncio.run(
        service.issue_api_key(
            user,
            correlation_id="rotate-key",
            actor_id=user.id,
            source="api",
        )
    )

    assert issued.raw_key
    assert repository.api_key is not None
    assert any(isinstance(event, ApiKeyIssued) for event in event_bus.events)
    api_event = next(event for event in event_bus.events if isinstance(event, ApiKeyIssued))
    assert api_event.user_id == user.id
    assert api_event.actor_id == user.id
    assert api_event.replaced_existing is True


def test_get_current_user_summary_includes_password_api_key_and_sso_metadata() -> None:
    user = make_user(password_hash=bcrypt.hashpw(b"old-pass", bcrypt.gensalt()).decode("utf-8"))
    service, repository, _, _ = make_service(user)
    repository.user_media = [
        make_media(media_type="image", format="png", storage_key="originals/user-1/image.png"),
    ]
    repository.user_media[0].thumb_key = "thumbnails/media-1.jpg"
    repository.user_media[0].thumb_size = 7
    repository.user_albums = [
        Album(
            id="album-1",
            title="Album",
            user_id=user.id,
            cover_media_id=None,
            delete_token=None,
            created_at=utcnow(),
            updated_at=utcnow(),
            expires_at=None,
        )
    ]
    repository.api_key = ApiKey(
        id=str(uuid4()),
        user_id=user.id,
        key_hash="existing-key-hash",
        created_at=utcnow(),
        last_used_at=None,
    )
    repository.user_sso_links = [
        UserSsoLink(
            id=str(uuid4()),
            user_id=user.id,
            provider="google",
            provider_uid="google-123",
            linked_at=utcnow(),
        )
    ]

    summary = asyncio.run(service.get_current_user_summary(user))

    assert summary["has_password"] is True
    assert summary["has_api_key"] is True
    assert summary["album_count"] == 1
    assert summary["media_count"] == 1
    assert summary["storage_used_bytes"] == 130
    assert summary["sso_providers"] == [
        {
            "provider": "google",
            "linked_at": repository.user_sso_links[0].linked_at.isoformat(),
        }
    ]


def test_update_user_emits_admin_status_and_limits_events() -> None:
    user = make_user(password_hash=bcrypt.hashpw(b"old-pass", bcrypt.gensalt()).decode("utf-8"))
    service, _, event_bus, _ = make_service(user)

    asyncio.run(
        service.update_user(
            user.id,
            UserUpdateInput(
                is_admin=True,
                suspended=None,
                quota_bytes=2048,
                rate_limit_rpm=12,
                rate_limit_bph=4096,
                password=None,
            ),
            "user-update-audit",
            actor_id="admin-1",
        )
    )

    admin_event = next(event for event in event_bus.events if isinstance(event, UserAdminStatusChanged))
    assert admin_event.user_id == user.id
    assert admin_event.actor_id == "admin-1"
    assert admin_event.old_is_admin is False
    assert admin_event.new_is_admin is True

    limits_event = next(event for event in event_bus.events if isinstance(event, UserLimitsChanged))
    assert limits_event.user_id == user.id
    assert limits_event.changes == {
        "quota_bytes": {"old": None, "new": 2048},
        "rate_limit_rpm": {"old": None, "new": 12},
        "rate_limit_bph": {"old": None, "new": 4096},
    }


def test_update_user_skips_admin_and_limits_events_when_values_do_not_change() -> None:
    user = make_user(password_hash=bcrypt.hashpw(b"old-pass", bcrypt.gensalt()).decode("utf-8"))
    user.rate_limit_rpm = 12
    user.rate_limit_bph = 4096
    user.quota_bytes = 2048
    service, _, event_bus, _ = make_service(user)

    asyncio.run(
        service.update_user(
            user.id,
            UserUpdateInput(
                is_admin=UNSET,
                suspended=None,
                quota_bytes=2048,
                rate_limit_rpm=12,
                rate_limit_bph=4096,
                password=None,
            ),
            "no-change-audit",
            actor_id="admin-1",
        )
    )

    assert not any(isinstance(event, UserAdminStatusChanged) for event in event_bus.events)
    assert not any(isinstance(event, UserLimitsChanged) for event in event_bus.events)


def test_set_user_admin_status_emits_event_with_requested_source() -> None:
    user = make_user(password_hash=bcrypt.hashpw(b"old-pass", bcrypt.gensalt()).decode("utf-8"))
    service, repository, event_bus, _ = make_service(user)

    updated = asyncio.run(
        service.set_user_admin_status(
            user.id,
            is_admin=True,
            correlation_id="cli-promote",
            source="cli",
        )
    )

    assert updated.is_admin is True
    assert repository.updated_user is not None
    admin_event = next(event for event in event_bus.events if isinstance(event, UserAdminStatusChanged))
    assert admin_event.source == "cli"
    assert admin_event.new_is_admin is True


def test_set_user_admin_status_is_idempotent_when_already_matching() -> None:
    user = make_user(password_hash=bcrypt.hashpw(b"old-pass", bcrypt.gensalt()).decode("utf-8"))
    user.is_admin = True
    service, repository, event_bus, _ = make_service(user)

    updated = asyncio.run(
        service.set_user_admin_status(
            user.id,
            is_admin=True,
            correlation_id="cli-promote",
            source="cli",
        )
    )

    assert updated.is_admin is True
    assert repository.updated_user is None
    assert not any(isinstance(event, UserAdminStatusChanged) for event in event_bus.events)


def test_require_album_access_allows_owner_admin_or_valid_token() -> None:
    service, _, _, _ = make_service()
    owner = make_user(password_hash=bcrypt.hashpw(b"x", bcrypt.gensalt()).decode("utf-8"))
    admin = make_user(password_hash=bcrypt.hashpw(b"y", bcrypt.gensalt()).decode("utf-8"))
    admin.id = "admin-1"
    admin.is_admin = True
    stranger = make_user(password_hash=bcrypt.hashpw(b"z", bcrypt.gensalt()).decode("utf-8"))
    stranger.id = "user-2"

    album = Album(
        id="album-1",
        title="Album",
        user_id=owner.id,
        cover_media_id=None,
        delete_token=None,
        created_at=utcnow(),
        updated_at=utcnow(),
        expires_at=None,
    )

    service._require_album_access(album, None, owner)
    service._require_album_access(album, None, admin)

    with pytest.raises(HTTPException) as denied:
        service._require_album_access(album, None, stranger)
    assert denied.value.status_code == 403

    anon_album = Album(
        id="album-2",
        title="Anon",
        user_id=None,
        cover_media_id=None,
        delete_token="secret-token",
        created_at=utcnow(),
        updated_at=utcnow(),
        expires_at=None,
    )

    service._require_album_access(anon_album, "secret-token", None)

    with pytest.raises(HTTPException) as bad_token:
        service._require_album_access(anon_album, "wrong", None)
    assert bad_token.value.status_code == 403


def test_get_or_create_album_requires_delete_token_for_anonymous_append() -> None:
    service, repository, _, _ = make_service()
    repository.album = Album(
        id="album-2",
        title="Anon",
        user_id=None,
        cover_media_id=None,
        delete_token="secret-token",
        created_at=utcnow(),
        updated_at=utcnow(),
        expires_at=None,
    )

    with pytest.raises(HTTPException) as denied:
        asyncio.run(
            service._get_or_create_album(
                album_id="album-2",
                title=None,
                correlation_id="cid-1",
                actor=CurrentActor(user=None, source="web"),
                delete_token=None,
            )
        )
    assert denied.value.status_code == 403

    album = asyncio.run(
        service._get_or_create_album(
            album_id="album-2",
            title=None,
            correlation_id="cid-2",
            actor=CurrentActor(user=None, source="web"),
            delete_token="secret-token",
        )
    )
    assert album.id == "album-2"


def test_album_to_payload_omits_delete_token_by_default() -> None:
    album = Album(
        id="album-3",
        title="Anon",
        user_id=None,
        cover_media_id=None,
        delete_token="secret-token",
        created_at=utcnow(),
        updated_at=utcnow(),
        expires_at=None,
    )
    media_item = type(
        "MediaStub",
        (),
        {
            "id": "media-1",
            "filename_orig": "sample.png",
            "media_type": "image",
            "mime_type": "image/png",
            "format": "png",
            "thumb_is_orig": False,
            "thumb_key": None,
            "position": 1000,
            "file_size": 123,
            "thumb_status": "done",
            "codec_hint": None,
        },
    )()

    payload = album_to_payload("http://testserver", album, [media_item])
    assert "delete_url" not in payload


def test_stream_album_zip_uses_storage_streams_without_buffering_whole_files() -> None:
    storage = DummyStorage({"media/original.png": b"png-data"})
    service, repository, _, _ = make_service(storage=storage)
    repository.album = Album(
        id="album-1",
        title="Album",
        user_id=None,
        cover_media_id=None,
        delete_token="token",
        created_at=utcnow(),
        updated_at=utcnow(),
        expires_at=None,
    )
    repository.album_media = [
        type(
            "MediaStub",
            (),
            {
                "storage_key": "media/original.png",
                "filename_orig": "sample.png",
                "format": "png",
            },
        )()
    ]

    archive = asyncio.run(service.stream_album_zip("album-1"))
    zipped = b"".join(archive)

    with ZipFile(BytesIO(zipped)) as extracted:
        assert extracted.namelist() == ["sample.png"]
        assert extracted.read("sample.png") == b"png-data"
    assert storage.get_bytes_called is False


def test_stream_album_zip_sanitizes_windows_paths_and_control_chars_in_filenames() -> None:
    storage = DummyStorage({"media/original.png": b"png-data"})
    service, repository, _, _ = make_service(storage=storage)
    repository.album = Album(
        id="album-1",
        title="Album",
        user_id=None,
        cover_media_id=None,
        delete_token="token",
        created_at=utcnow(),
        updated_at=utcnow(),
        expires_at=None,
    )
    repository.album_media = [
        type(
            "MediaStub",
            (),
            {
                "id": "media-1",
                "storage_key": "media/original.png",
                "filename_orig": "..\\\\evil\x00name?.png",
                "format": "png",
            },
        )()
    ]

    archive = asyncio.run(service.stream_album_zip("album-1"))
    zipped = b"".join(archive)

    with ZipFile(BytesIO(zipped)) as extracted:
        assert extracted.namelist() == ["evilname_.png"]
        assert extracted.read("evilname_.png") == b"png-data"


def test_generate_thumbnail_records_processor_missing_failure(caplog) -> None:
    storage = DummyStorage({"originals/u/media.mp4": b"video"})
    service, repository, _, telemetry = make_service(storage=storage, processors=DummyProcessors(None))
    repository.media = make_media()

    asyncio.run(service.generate_thumbnail("media-1", "thumb-cid"))

    assert repository.media is not None
    assert repository.media.thumb_status == "failed"
    assert telemetry.last_task_failure is not None
    assert telemetry.last_task_failure["reason"] == "processor_missing"
    assert telemetry.last_task_failure["media_id"] == "media-1"
    assert telemetry.last_task_failure["correlation_id"] == "thumb-cid"


def test_generate_thumbnail_records_storage_read_failure() -> None:
    storage = DummyStorage({"originals/u/media.mp4": b"video"})
    storage.fail_get_bytes = True
    processor = DummyProcessor()
    service, repository, _, telemetry = make_service(storage=storage, processors=DummyProcessors(processor))
    repository.media = make_media()

    asyncio.run(service.generate_thumbnail("media-1", "thumb-read-fail"))

    assert repository.media is not None
    assert repository.media.thumb_status == "failed"
    assert telemetry.last_task_failure is not None
    assert telemetry.last_task_failure["reason"] == "storage_read_failed"


def test_generate_thumbnail_records_metadata_extract_failure() -> None:
    storage = DummyStorage({"originals/u/media.mp4": b"video"})
    processor = DummyProcessor()
    processor.extract_error = RuntimeError("metadata extract failed")
    service, repository, _, telemetry = make_service(storage=storage, processors=DummyProcessors(processor))
    repository.media = make_media()

    asyncio.run(service.generate_thumbnail("media-1", "thumb-metadata-fail"))

    assert repository.media is not None
    assert repository.media.thumb_status == "failed"
    assert telemetry.last_task_failure is not None
    assert telemetry.last_task_failure["reason"] == "metadata_extract_failed"


def test_generate_thumbnail_records_thumbnail_generation_failure_and_clears_fields() -> None:
    storage = DummyStorage({"originals/u/media.mp4": b"video"})
    processor = DummyProcessor()
    processor.generate_error = RuntimeError("thumbnail generate failed")
    service, repository, _, telemetry = make_service(storage=storage, processors=DummyProcessors(processor))
    repository.media = make_media()

    asyncio.run(service.generate_thumbnail("media-1", "thumb-generate-fail"))

    assert repository.media is not None
    assert repository.media.thumb_status == "failed"
    assert repository.media.thumb_key is None
    assert repository.media.thumb_size is None
    assert repository.media.thumb_is_orig is False
    assert telemetry.last_task_failure is not None
    assert telemetry.last_task_failure["reason"] == "thumbnail_generate_failed"


def test_generate_thumbnail_retryable_failure_returns_retry_state_and_resets_pending() -> None:
    storage = DummyStorage({"originals/u/media.mp4": b"video"})
    processor = DummyProcessor()
    processor.generate_error = RuntimeError("thumbnail generate failed")
    service, repository, _, telemetry = make_service(storage=storage, processors=DummyProcessors(processor))
    repository.media = make_media()

    result = asyncio.run(service.generate_thumbnail("media-1", "thumb-retry", attempt=1, max_attempts=3))

    assert result.state == TASK_STATE_RETRY_SCHEDULED
    assert repository.media is not None
    assert repository.media.thumb_status == "pending"
    assert telemetry.last_task_failure is None
    assert telemetry.thumbnail_jobs[-1]["result"] == "retry"


def test_generate_thumbnail_retryable_failure_exhaustion_returns_failed_exhausted() -> None:
    storage = DummyStorage({"originals/u/media.mp4": b"video"})
    processor = DummyProcessor()
    processor.generate_error = RuntimeError("thumbnail generate failed")
    service, repository, _, telemetry = make_service(storage=storage, processors=DummyProcessors(processor))
    repository.media = make_media()

    result = asyncio.run(service.generate_thumbnail("media-1", "thumb-exhausted", attempt=3, max_attempts=3))

    assert result.state == TASK_STATE_FAILED_EXHAUSTED
    assert repository.media is not None
    assert repository.media.thumb_status == "failed"
    assert telemetry.last_task_failure is not None
    assert telemetry.last_task_failure["reason"] == "thumbnail_generate_failed"


def test_generate_thumbnail_uses_runtime_configured_video_thumb_frames() -> None:
    storage = DummyStorage({"originals/u/media.mp4": b"video"})
    processor = DummyProcessor()
    processor.thumb_frames = 1  # type: ignore[attr-defined]
    service, repository, _, _ = make_service(
        storage=storage,
        processors=DummyProcessors(processor),
        runtime_values={"video_thumb_frames": 17},
    )
    repository.media = make_media()

    asyncio.run(service.generate_thumbnail("media-1", "thumb-runtime-frames"))

    assert processor.thumb_frames == 17  # type: ignore[attr-defined]
    assert repository.media is not None
    assert repository.media.thumb_status == "done"


def test_generate_thumbnail_cleans_up_written_thumbnail_on_repository_update_failure() -> None:
    storage = DummyStorage({"originals/u/media.mp4": b"video"})
    processor = DummyProcessor()
    service, repository, _, telemetry = make_service(storage=storage, processors=DummyProcessors(processor))
    repository.media = make_media()
    repository.fail_update_media_on_call = 2

    asyncio.run(service.generate_thumbnail("media-1", "thumb-update-fail"))

    assert repository.media is not None
    assert repository.media.thumb_status == "failed"
    assert repository.media.thumb_key is None
    assert "thumbnails/media-1.jpg" in storage.delete_calls
    assert "thumbnails/media-1.jpg" not in storage.payloads
    assert telemetry.last_task_failure is not None
    assert telemetry.last_task_failure["reason"] == "repository_update_failed"


def test_generate_thumbnail_records_cleanup_failure_but_keeps_failed_state() -> None:
    storage = DummyStorage({"originals/u/media.mp4": b"video"})
    storage.fail_delete = True
    processor = DummyProcessor()
    service, repository, _, telemetry = make_service(storage=storage, processors=DummyProcessors(processor))
    repository.media = make_media()
    repository.fail_update_media_on_call = 2

    asyncio.run(service.generate_thumbnail("media-1", "thumb-cleanup-fail"))

    assert repository.media is not None
    assert repository.media.thumb_status == "failed"
    assert repository.media.thumb_key is None
    assert "thumbnails/media-1.jpg" in storage.delete_calls
    assert telemetry.last_task_failure is not None
    assert telemetry.last_task_failure["reason"] == "thumbnail_cleanup_failed"
