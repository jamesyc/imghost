from io import BytesIO
from time import monotonic, sleep

from fastapi.testclient import TestClient

from imghost.__main__ import main as cli_main
from imghost.main import app
from imghost.models import utcnow
from imghost.processors import MediaMetadata, SanitizedFile, ThumbnailResult, ValidationResult

from .helpers import (
    PNG_1X1,
    get_album_record,
    get_media_record,
    update_album_record,
    update_media_record,
    wait_for_thumbnail,
)


def wait_for_failed_thumbnail(client: TestClient, media_id: str, *, timeout: float = 2.0) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        media = client.portal.call(client.app.state.imghost.repository.get_media, media_id)
        assert media is not None
        if media.thumb_status == "failed":
            return
        sleep(0.02)
    raise AssertionError(f"thumbnail for {media_id} did not enter failed state within {timeout} seconds")


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
    with TestClient(app) as client:
        wait_for_thumbnail(client, media_id)
        album = client.get(f"/api/v1/album/{payload['album_id']}").json()
        assert album["items"][0]["thumb_status"] == "done"


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
