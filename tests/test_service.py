import asyncio
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import bcrypt
import pytest

from fastapi import HTTPException
from imghost.config import Settings
from imghost.models import Album, User, utcnow
from imghost.storage import StorageStream
from imghost.payloads import album_to_payload
from imghost.service import CurrentActor, LocalLoginInput, PasswordChangeInput, UploadService


class DummyRepository:
    def __init__(self, user: User | None = None) -> None:
        self.user = user
        self.updated_user: User | None = None
        self.album: Album | None = None
        self.album_media: list[object] = []

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

    async def get_album(self, album_id: str) -> Album | None:
        if self.album and self.album.id == album_id:
            return self.album
        return None

    async def list_album_media(self, album_id: str) -> list[object]:
        if self.album and self.album.id == album_id:
            return list(self.album_media)
        return []


class DummyStorage:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.get_bytes_called = False

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
        return self.payloads[key]


def make_service(user: User | None = None, *, storage=None) -> tuple[UploadService, DummyRepository]:
    repository = DummyRepository(user)
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
    )
    service = UploadService(
        settings=settings,
        repository=repository,
        storage=storage,  # type: ignore[arg-type]
        event_bus=None,  # type: ignore[arg-type]
        processors=None,  # type: ignore[arg-type]
        runtime_config=None,  # type: ignore[arg-type]
        rate_limiter=None,  # type: ignore[arg-type]
    )
    return service, repository


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


def test_hash_password_uses_bcrypt_and_is_not_deterministic() -> None:
    service, _ = make_service()

    first = service._hash_password("secret-pass")
    second = service._hash_password("secret-pass")

    assert first != second
    assert first.startswith("$2")
    assert second.startswith("$2")
    assert bcrypt.checkpw(b"secret-pass", first.encode("utf-8"))
    assert bcrypt.checkpw(b"secret-pass", second.encode("utf-8"))


def test_authenticate_local_user_accepts_bcrypt_hash() -> None:
    password_hash = bcrypt.hashpw(b"secret-pass", bcrypt.gensalt()).decode("utf-8")
    service, _ = make_service(make_user(password_hash=password_hash))

    user = asyncio.run(
        service.authenticate_local_user(LocalLoginInput(login="alice@example.com", password="secret-pass"))
    )

    assert user.username == "alice"


def test_change_password_replaces_hash_with_new_bcrypt_hash() -> None:
    old_hash = bcrypt.hashpw(b"old-pass", bcrypt.gensalt()).decode("utf-8")
    user = make_user(password_hash=old_hash)
    service, repository = make_service(user)

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


def test_verify_password_returns_false_for_invalid_stored_hash() -> None:
    service, _ = make_service()

    assert service._verify_password("secret-pass", "not-a-bcrypt-hash") is False


def test_require_album_access_allows_owner_admin_or_valid_token() -> None:
    service, _ = make_service()
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
    service, repository = make_service()
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
    service, repository = make_service(storage=storage)
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
