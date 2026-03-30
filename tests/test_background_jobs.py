import asyncio
from datetime import timedelta
from io import BytesIO
from time import monotonic, sleep
from uuid import uuid4

from fastapi.testclient import TestClient

from imghost.app_state import AppState
from imghost.__main__ import main as cli_main
from imghost.config import load_settings
from imghost.main import app
from imghost.models import ShareXDeleteCapability, utcnow
from imghost.processors import MediaMetadata, SanitizedFile, ThumbnailResult, ValidationResult, VideoProcessingError
from imghost.sharex_delete import (
    SHAREX_DELETE_CONSUMED_RETENTION_DAYS,
    SHAREX_DELETE_REVOKED_RETENTION_DAYS,
)

from .helpers import (
    PNG_1X1,
    create_user_and_api_key,
    get_album_record,
    get_media_record,
    update_album_record,
    update_media_record,
    wait_for_thumbnail,
)


async def _create_sharex_capability(
    state: AppState,
    *,
    album_id: str,
    user_id: str,
    created_at=None,
    expires_at=None,
    consumed_at=None,
    revoked_at=None,
) -> ShareXDeleteCapability:
    now = utcnow()
    capability = ShareXDeleteCapability(
        selector=f"cap-{uuid4().hex[:16]}",
        purpose="sharex_delete_album",
        album_id=album_id,
        user_id=user_id,
        secret_hash=uuid4().hex,
        created_at=created_at or now,
        expires_at=expires_at or (now + timedelta(days=90)),
        consumed_at=consumed_at,
        revoked_at=revoked_at,
        last_seen_at=None,
    )
    return await state.repository.create_sharex_delete_capability(capability)


def wait_for_failed_thumbnail(client: TestClient, media_id: str, *, timeout: float = 2.0) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        media = client.portal.call(client.app.state.imghost.repository.get_media, media_id)
        assert media is not None
        if media.thumb_status == "failed":
            return
        sleep(0.02)
    raise AssertionError(f"thumbnail for {media_id} did not enter failed state within {timeout} seconds")


async def _run_thumbnail_worker_startup_and_wait_for_status(media_id: str, *, expected_status: str) -> None:
    state = AppState(load_settings(), process_role="worker", task_worker_queues=("thumbnails",))
    await state.start()
    try:
        deadline = monotonic() + 2.0
        while monotonic() < deadline:
            media = await state.repository.get_media(media_id)
            assert media is not None
            if media.thumb_status == expected_status:
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"thumbnail for {media_id} did not enter {expected_status!r} within startup timeout")
    finally:
        await state.stop()


def test_async_thumbnail_worker_recovers_pending_items_on_startup(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("TASK_QUEUE_MODE", "sync")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
        )
        assert response.status_code == 200
        payload = response.json()
        media_id = payload["media_id"]
        wait_for_thumbnail(client, media_id)
        update_media_record(client, media_id, thumb_status="processing", thumb_key=None, thumb_size=None)

    thumb_path = tmp_path / "thumbnails"
    for existing in thumb_path.glob(f"{media_id}.*"):
        existing.unlink()

    monkeypatch.setenv("TASK_QUEUE_MODE", "async")
    asyncio.run(_run_thumbnail_worker_startup_and_wait_for_status(media_id, expected_status="done"))

    with TestClient(app) as client:
        album = client.get(f"/api/v1/album/{payload['album_id']}").json()
        assert album["items"][0]["thumb_status"] == "done"


def test_async_thumbnail_worker_marks_processing_item_failed_when_source_missing_on_startup(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("TASK_QUEUE_MODE", "sync")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
        )
        assert response.status_code == 200
        payload = response.json()
        media_id = payload["media_id"]
        wait_for_thumbnail(client, media_id)
        update_media_record(
            client,
            media_id,
            thumb_status="processing",
            thumb_key=None,
            thumb_size=None,
            thumb_is_orig=False,
        )

    original_path = next((tmp_path / "originals" / "anon").glob(f"{media_id}.*"))
    original_path.unlink()
    for existing in (tmp_path / "thumbnails").glob(f"{media_id}.*"):
        existing.unlink()

    monkeypatch.setenv("TASK_QUEUE_MODE", "async")
    asyncio.run(_run_thumbnail_worker_startup_and_wait_for_status(media_id, expected_status="failed"))

    with TestClient(app) as client:
        media = client.portal.call(client.app.state.imghost.repository.get_media, media_id)
        assert media is not None
        assert media.thumb_status == "failed"
        assert client.get(f"/t/{media_id}.jpg").status_code == 404


def test_failed_thumbnail_can_be_reenqueued_for_recovery(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("TASK_QUEUE_MODE", "async")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
        )
        assert response.status_code == 200
        payload = response.json()
        media_id = payload["media_id"]
        wait_for_thumbnail(client, media_id)

        media_path = next((tmp_path / "originals" / "anon").glob(f"{media_id}.*"))
        media_path.write_bytes(b"broken")

        recovered = client.app.state.imghost
        media = client.portal.call(recovered.repository.get_media, media_id)
        assert media is not None
        media.thumb_status = "pending"
        media.thumb_key = None
        media.thumb_size = None
        media.thumb_is_orig = False
        client.portal.call(recovered.repository.update_media, media)
        for existing in (tmp_path / "thumbnails").glob(f"{media_id}.*"):
            existing.unlink()

        client.portal.call(recovered.uploads.generate_thumbnail, media_id, "test-failure")
        failed_response = client.get(f"/t/{media_id}.jpg")
        assert failed_response.status_code == 404

        media = client.portal.call(recovered.repository.get_media, media_id)
        assert media is not None
        assert media.thumb_status == "failed"

        media_path.write_bytes(PNG_1X1)
        reenqueued = client.portal.call(lambda: recovered.recover_thumbnails(include_failed=True))
        assert reenqueued >= 1
        wait_for_thumbnail(client, media_id)


def test_prune_dry_run_preserves_expired_album(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("expired.png", BytesIO(PNG_1X1), "image/png"))],
        )
        assert response.status_code == 200
        payload = response.json()
        media_id = payload["media_id"]
        wait_for_thumbnail(client, media_id)
        update_album_record(client, payload["album_id"], expires_at=utcnow().replace(microsecond=0))

    exit_code = cli_main(["prune", "--dry-run"])
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "prune dry-run: albums=1 items=1" in output
    assert payload["album_id"] in output

    with TestClient(app) as client:
        assert get_album_record(client, payload["album_id"]) is not None
        assert get_media_record(client, payload["media_id"]) is not None
    assert next((tmp_path / "originals" / "anon").glob(f"{payload['media_id']}.*")).exists()


def test_prune_deletes_expired_album_and_media(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("expired.png", BytesIO(PNG_1X1), "image/png"))],
        )
        assert response.status_code == 200
        payload = response.json()
        media_id = payload["media_id"]
        wait_for_thumbnail(client, media_id)
        update_album_record(client, payload["album_id"], expires_at=utcnow().replace(microsecond=0))

    exit_code = cli_main(["prune"])
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "prune deleted: albums=1 items=1" in output

    with TestClient(app) as client:
        assert client.get(f"/api/v1/album/{payload['album_id']}").status_code == 404
        assert client.get(f"/i/{payload['media_id']}.png").status_code == 404


def test_prune_removes_expired_sharex_delete_capabilities(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("SECRET_KEY", "sharex-delete-secret")
    user_id, api_key = create_user_and_api_key(capsys, username="prunecapowner1", email="prunecapowner1@example.com")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/upload",
            headers={"Authorization": f"Bearer {api_key}"},
            files=[("file", ("expired.png", BytesIO(PNG_1X1), "image/png"))],
        )
        assert created.status_code == 200
        payload = created.json()
        state = client.app.state.imghost
        capability = client.portal.call(
            lambda: _create_sharex_capability(
                state,
                album_id=payload["album_id"],
                user_id=user_id,
                expires_at=utcnow() - timedelta(seconds=1),
            )
        )

    exit_code = cli_main(["prune"])
    assert exit_code == 0

    with TestClient(app) as client:
        assert client.portal.call(client.app.state.imghost.repository.get_sharex_delete_capability, capability.selector) is None


def test_prune_keeps_recent_consumed_sharex_delete_capabilities(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    user_id, api_key = create_user_and_api_key(capsys, username="prunecapowner2", email="prunecapowner2@example.com")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/upload",
            headers={"Authorization": f"Bearer {api_key}"},
            files=[("file", ("kept.png", BytesIO(PNG_1X1), "image/png"))],
        )
        payload = created.json()
        capability = client.portal.call(
            lambda: _create_sharex_capability(
                client.app.state.imghost,
                album_id=payload["album_id"],
                user_id=user_id,
                consumed_at=utcnow() - timedelta(days=SHAREX_DELETE_CONSUMED_RETENTION_DAYS - 1),
            )
        )
        client.portal.call(lambda: client.app.state.imghost.uploads.prune_expired_albums(dry_run=False))
        kept = client.portal.call(client.app.state.imghost.repository.get_sharex_delete_capability, capability.selector)
        assert kept is not None


def test_prune_removes_consumed_sharex_delete_capabilities_past_retention(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    user_id, api_key = create_user_and_api_key(capsys, username="prunecapowner3", email="prunecapowner3@example.com")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/upload",
            headers={"Authorization": f"Bearer {api_key}"},
            files=[("file", ("oldconsumed.png", BytesIO(PNG_1X1), "image/png"))],
        )
        payload = created.json()
        capability = client.portal.call(
            lambda: _create_sharex_capability(
                client.app.state.imghost,
                album_id=payload["album_id"],
                user_id=user_id,
                consumed_at=utcnow() - timedelta(days=SHAREX_DELETE_CONSUMED_RETENTION_DAYS + 1),
            )
        )
        client.portal.call(lambda: client.app.state.imghost.uploads.prune_expired_albums(dry_run=False))
        assert client.portal.call(client.app.state.imghost.repository.get_sharex_delete_capability, capability.selector) is None


def test_prune_keeps_recent_revoked_sharex_delete_capabilities(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    user_id, api_key = create_user_and_api_key(capsys, username="prunecapowner4", email="prunecapowner4@example.com")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/upload",
            headers={"Authorization": f"Bearer {api_key}"},
            files=[("file", ("keptrevoked.png", BytesIO(PNG_1X1), "image/png"))],
        )
        payload = created.json()
        capability = client.portal.call(
            lambda: _create_sharex_capability(
                client.app.state.imghost,
                album_id=payload["album_id"],
                user_id=user_id,
                revoked_at=utcnow() - timedelta(days=SHAREX_DELETE_REVOKED_RETENTION_DAYS - 1),
            )
        )
        client.portal.call(lambda: client.app.state.imghost.uploads.prune_expired_albums(dry_run=False))
        kept = client.portal.call(client.app.state.imghost.repository.get_sharex_delete_capability, capability.selector)
        assert kept is not None


def test_prune_removes_revoked_sharex_delete_capabilities_past_retention(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    user_id, api_key = create_user_and_api_key(capsys, username="prunecapowner5", email="prunecapowner5@example.com")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/upload",
            headers={"Authorization": f"Bearer {api_key}"},
            files=[("file", ("oldrevoked.png", BytesIO(PNG_1X1), "image/png"))],
        )
        payload = created.json()
        capability = client.portal.call(
            lambda: _create_sharex_capability(
                client.app.state.imghost,
                album_id=payload["album_id"],
                user_id=user_id,
                revoked_at=utcnow() - timedelta(days=SHAREX_DELETE_REVOKED_RETENTION_DAYS + 1),
            )
        )
        client.portal.call(lambda: client.app.state.imghost.uploads.prune_expired_albums(dry_run=False))
        assert client.portal.call(client.app.state.imghost.repository.get_sharex_delete_capability, capability.selector) is None


def test_app_scheduler_cleanup_prunes_sharex_capabilities_without_redis(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("REDIS_MODE", "disabled")
    monkeypatch.setenv("TASK_QUEUE_MODE", "async")
    monkeypatch.setenv("APP_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    user_id, api_key = create_user_and_api_key(capsys, username="prunecapowner6", email="prunecapowner6@example.com")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/upload",
            headers={"Authorization": f"Bearer {api_key}"},
            files=[("file", ("sched.png", BytesIO(PNG_1X1), "image/png"))],
        )
        payload = created.json()
        capability = client.portal.call(
            lambda: _create_sharex_capability(
                client.app.state.imghost,
                album_id=payload["album_id"],
                user_id=user_id,
                expires_at=utcnow() - timedelta(seconds=1),
            )
        )
        state = client.app.state.imghost
        client.portal.call(lambda: state.scheduler.tick(now_monotonic=10**12))
        client.portal.call(state.tasks.join)
        assert client.portal.call(state.repository.get_sharex_delete_capability, capability.selector) is None


def test_retry_thumbnails_cli_recovers_failed_thumbnail(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("TASK_QUEUE_MODE", "async")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
        )
        assert response.status_code == 200
        payload = response.json()
        media_id = payload["media_id"]
        wait_for_thumbnail(client, media_id)
        update_media_record(
            client,
            media_id,
            thumb_status="failed",
            thumb_key=None,
            thumb_size=None,
            thumb_is_orig=False,
        )

    media_path = next((tmp_path / "originals" / "anon").glob(f"{media_id}.*"))
    media_path.write_bytes(b"broken")
    for existing in (tmp_path / "thumbnails").glob(f"{media_id}.*"):
        existing.unlink()

    media_path.write_bytes(PNG_1X1)
    exit_code = cli_main(["retry-thumbnails"])
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "re-enqueued thumbnails: 1" in output

    with TestClient(app) as client:
        wait_for_thumbnail(client, media_id)


def test_failed_video_thumbnail_does_not_block_next_job_and_original_media_stays_accessible(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("TASK_QUEUE_MODE", "async")

    async def fake_validate(self, payload: bytes) -> ValidationResult:
        return ValidationResult(ok=True)

    async def fake_extract_metadata(self, payload: bytes, format_hint: str) -> MediaMetadata:
        return MediaMetadata(
            width=640,
            height=360,
            duration_secs=2.0,
            codec_hint=None,
            is_animated=True,
            mime_type="video/mp4",
            format="mp4",
        )

    async def fake_sanitize(self, payload: bytes, metadata: MediaMetadata) -> SanitizedFile:
        return SanitizedFile(data=payload, mime_type="video/mp4", format="mp4")

    async def fake_generate_thumbnail(self, payload: bytes, metadata: MediaMetadata) -> ThumbnailResult:
        if payload == b"bad-video":
            raise RuntimeError("thumbnail generate failed")
        return ThumbnailResult(data=b"jpg-thumb", thumb_is_orig=False, format="jpg", size=len(b"jpg-thumb"))

    monkeypatch.setattr("imghost.processors.Mp4Processor.validate", fake_validate)
    monkeypatch.setattr("imghost.processors.Mp4Processor.extract_metadata", fake_extract_metadata)
    monkeypatch.setattr("imghost.processors.Mp4Processor.sanitize", fake_sanitize)
    monkeypatch.setattr("imghost.processors.Mp4Processor.generate_thumbnail", fake_generate_thumbnail)

    with TestClient(app) as client:
        failed_upload = client.post(
            "/api/v1/upload",
            files=[("file", ("bad.mp4", BytesIO(b"bad-video"), "video/mp4"))],
        )
        assert failed_upload.status_code == 200
        failed_media_id = failed_upload.json()["media_id"]

        successful_upload = client.post(
            "/api/v1/upload",
            files=[("file", ("good.mp4", BytesIO(b"good-video"), "video/mp4"))],
        )
        assert successful_upload.status_code == 200
        success_media_id = successful_upload.json()["media_id"]

        wait_for_failed_thumbnail(client, failed_media_id)
        wait_for_thumbnail(client, success_media_id)

        failed_media = client.portal.call(client.app.state.imghost.repository.get_media, failed_media_id)
        success_media = client.portal.call(client.app.state.imghost.repository.get_media, success_media_id)
        assert failed_media is not None
        assert success_media is not None
        assert failed_media.thumb_status == "failed"
        assert success_media.thumb_status == "done"

        original = client.get(f"/i/{failed_media_id}.mp4")
        assert original.status_code == 200

        failed_thumb = client.get(f"/t/{failed_media_id}.jpg")
        assert failed_thumb.status_code == 404


def test_thumbnail_timeout_marks_only_that_media_failed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("TASK_QUEUE_MODE", "async")

    async def fake_validate(self, payload: bytes) -> ValidationResult:
        return ValidationResult(ok=True)

    async def fake_extract_metadata(self, payload: bytes, format_hint: str) -> MediaMetadata:
        return MediaMetadata(
            width=640,
            height=360,
            duration_secs=2.0,
            codec_hint=None,
            is_animated=True,
            mime_type="video/mp4",
            format="mp4",
        )

    async def fake_sanitize(self, payload: bytes, metadata: MediaMetadata) -> SanitizedFile:
        return SanitizedFile(data=payload, mime_type="video/mp4", format="mp4")

    async def fake_generate_thumbnail(self, payload: bytes, metadata: MediaMetadata) -> ThumbnailResult:
        if payload == b"timeout-video":
            raise VideoProcessingError(tool="ffmpeg", timed_out=True, stderr_excerpt="timeout tail")
        return ThumbnailResult(data=b"jpg-thumb", thumb_is_orig=False, format="jpg", size=len(b"jpg-thumb"))

    monkeypatch.setattr("imghost.processors.Mp4Processor.validate", fake_validate)
    monkeypatch.setattr("imghost.processors.Mp4Processor.extract_metadata", fake_extract_metadata)
    monkeypatch.setattr("imghost.processors.Mp4Processor.sanitize", fake_sanitize)
    monkeypatch.setattr("imghost.processors.Mp4Processor.generate_thumbnail", fake_generate_thumbnail)

    with TestClient(app) as client:
        failed_upload = client.post(
            "/api/v1/upload",
            files=[("file", ("timeout.mp4", BytesIO(b"timeout-video"), "video/mp4"))],
        )
        assert failed_upload.status_code == 200
        failed_media_id = failed_upload.json()["media_id"]

        successful_upload = client.post(
            "/api/v1/upload",
            files=[("file", ("good.mp4", BytesIO(b"good-video"), "video/mp4"))],
        )
        assert successful_upload.status_code == 200
        success_media_id = successful_upload.json()["media_id"]

        wait_for_failed_thumbnail(client, failed_media_id)
        wait_for_thumbnail(client, success_media_id)

        failed_media = client.portal.call(client.app.state.imghost.repository.get_media, failed_media_id)
        success_media = client.portal.call(client.app.state.imghost.repository.get_media, success_media_id)
        assert failed_media is not None
        assert success_media is not None
        assert failed_media.thumb_status == "failed"
        assert success_media.thumb_status == "done"


def test_failed_video_thumbnail_can_recover_on_retry_after_processor_fix(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("TASK_QUEUE_MODE", "async")

    should_fail = {"value": True}

    async def fake_validate(self, payload: bytes) -> ValidationResult:
        return ValidationResult(ok=True)

    async def fake_extract_metadata(self, payload: bytes, format_hint: str) -> MediaMetadata:
        return MediaMetadata(
            width=640,
            height=360,
            duration_secs=2.0,
            codec_hint=None,
            is_animated=True,
            mime_type="video/mp4",
            format="mp4",
        )

    async def fake_sanitize(self, payload: bytes, metadata: MediaMetadata) -> SanitizedFile:
        return SanitizedFile(data=payload, mime_type="video/mp4", format="mp4")

    async def fake_generate_thumbnail(self, payload: bytes, metadata: MediaMetadata) -> ThumbnailResult:
        if should_fail["value"]:
            raise VideoProcessingError(tool="ffmpeg", timed_out=True, stderr_excerpt="timeout tail")
        return ThumbnailResult(data=b"jpg-thumb", thumb_is_orig=False, format="jpg", size=len(b"jpg-thumb"))

    monkeypatch.setattr("imghost.processors.Mp4Processor.validate", fake_validate)
    monkeypatch.setattr("imghost.processors.Mp4Processor.extract_metadata", fake_extract_metadata)
    monkeypatch.setattr("imghost.processors.Mp4Processor.sanitize", fake_sanitize)
    monkeypatch.setattr("imghost.processors.Mp4Processor.generate_thumbnail", fake_generate_thumbnail)

    with TestClient(app) as client:
        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("flaky.mp4", BytesIO(b"flaky-video"), "video/mp4"))],
        )
        assert upload.status_code == 200
        media_id = upload.json()["media_id"]

        wait_for_failed_thumbnail(client, media_id)
        assert client.get(f"/t/{media_id}.jpg").status_code == 404

        should_fail["value"] = False
        requeued = client.portal.call(lambda: client.app.state.imghost.recover_thumbnails(include_failed=True))
        assert requeued >= 1
        wait_for_thumbnail(client, media_id)
        assert client.get(f"/t/{media_id}.jpg").status_code == 200

        media = client.portal.call(client.app.state.imghost.repository.get_media, media_id)
        assert media is not None
        assert media.thumb_status == "done"


def test_failed_video_thumbnail_does_not_block_image_uploads(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("TASK_QUEUE_MODE", "async")

    async def fake_validate(self, payload: bytes) -> ValidationResult:
        return ValidationResult(ok=True)

    async def fake_extract_metadata(self, payload: bytes, format_hint: str) -> MediaMetadata:
        return MediaMetadata(
            width=640,
            height=360,
            duration_secs=2.0,
            codec_hint=None,
            is_animated=True,
            mime_type="video/mp4",
            format="mp4",
        )

    async def fake_sanitize(self, payload: bytes, metadata: MediaMetadata) -> SanitizedFile:
        return SanitizedFile(data=payload, mime_type="video/mp4", format="mp4")

    async def fake_generate_thumbnail(self, payload: bytes, metadata: MediaMetadata) -> ThumbnailResult:
        raise VideoProcessingError(tool="ffmpeg", timed_out=True, stderr_excerpt="timeout tail")

    monkeypatch.setattr("imghost.processors.Mp4Processor.validate", fake_validate)
    monkeypatch.setattr("imghost.processors.Mp4Processor.extract_metadata", fake_extract_metadata)
    monkeypatch.setattr("imghost.processors.Mp4Processor.sanitize", fake_sanitize)
    monkeypatch.setattr("imghost.processors.Mp4Processor.generate_thumbnail", fake_generate_thumbnail)

    with TestClient(app) as client:
        failed_upload = client.post(
            "/api/v1/upload",
            files=[("file", ("bad.mp4", BytesIO(b"bad-video"), "video/mp4"))],
        )
        assert failed_upload.status_code == 200
        failed_media_id = failed_upload.json()["media_id"]

        image_upload = client.post(
            "/api/v1/upload",
            files=[("file", ("good.png", BytesIO(PNG_1X1), "image/png"))],
        )
        assert image_upload.status_code == 200
        image_media_id = image_upload.json()["media_id"]

        wait_for_failed_thumbnail(client, failed_media_id)
        wait_for_thumbnail(client, image_media_id)

        failed_media = client.portal.call(client.app.state.imghost.repository.get_media, failed_media_id)
        image_media = client.portal.call(client.app.state.imghost.repository.get_media, image_media_id)
        assert failed_media is not None
        assert image_media is not None
        assert failed_media.thumb_status == "failed"
        assert image_media.thumb_status == "done"
        assert client.get(f"/i/{image_media_id}.png").status_code == 200
        assert client.get(f"/t/{image_media_id}.jpg").status_code == 200
