from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from fastapi.testclient import TestClient
from PIL import Image
from pillow_heif import register_heif_opener

from imghost.main import app
from imghost.processors import MediaMetadata, SanitizedFile, ValidationResult, VideoProcessingError

from .helpers import PNG_1X1, create_user_and_api_key, wait_for_thumbnail

register_heif_opener(thumbnails=False)


def jpeg_bytes(color: str = "red", size: tuple[int, int] = (8, 8)) -> bytes:
    image = Image.new("RGB", size, color)
    output = BytesIO()
    image.save(output, format="JPEG")
    return output.getvalue()


def modern_image_bytes(
    *,
    mode: str = "RGB",
    size: tuple[int, int] = (8, 8),
    color: str | tuple[int, int, int] | tuple[int, int, int, int] = "red",
    format_name: str = "HEIF",
) -> bytes:
    image = Image.new(mode, size, color)
    output = BytesIO()
    image.save(output, format=format_name)
    return output.getvalue()


def test_upload_album_and_media_serving(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "V1 Album"},
        )

        assert response.status_code == 200
        payload = response.json()
        album_id = payload["album_id"]
        media_id = payload["items"][0]["media_id"]
        manage_url = payload["manage_url"]
        assert payload["items"][0]["thumb_status"] in {"pending", "processing", "done"}
        assert manage_url.startswith(f"http://testserver/manage/{album_id}?token=")
        delete_token = manage_url.split("token=")[1]

        album_response = client.get(f"/api/v1/album/{album_id}")
        assert album_response.status_code == 200
        assert album_response.json()["title"] == "V1 Album"
        assert album_response.json()["item_count"] == 1
        assert "delete_url" not in album_response.json()

        media_response = client.get(f"/i/{media_id}.png")
        assert media_response.status_code == 200
        assert media_response.headers["content-type"] == "image/png"

        stored_bytes = media_response.content

        range_response = client.get(f"/i/{media_id}.png", headers={"Range": "bytes=0-3"})
        assert range_response.status_code == 206
        assert range_response.headers["content-range"] == f"bytes 0-3/{len(stored_bytes)}"
        assert range_response.content == stored_bytes[:4]

        open_ended_range = client.get(f"/i/{media_id}.png", headers={"Range": "bytes=2-"})
        assert open_ended_range.status_code == 206
        assert open_ended_range.headers["content-range"] == f"bytes 2-{len(stored_bytes) - 1}/{len(stored_bytes)}"
        assert open_ended_range.content == stored_bytes[2:]

        suffix_range = client.get(f"/i/{media_id}.png", headers={"Range": "bytes=-4"})
        assert suffix_range.status_code == 206
        assert suffix_range.headers["content-range"] == f"bytes {len(stored_bytes) - 4}-{len(stored_bytes) - 1}/{len(stored_bytes)}"
        assert suffix_range.content == stored_bytes[-4:]

        invalid_range = client.get(f"/i/{media_id}.png", headers={"Range": "bytes=4-1"})
        assert invalid_range.status_code == 416
        assert invalid_range.headers["content-range"] == f"bytes */{len(stored_bytes)}"

        malformed_range = client.get(f"/i/{media_id}.png", headers={"Range": "bytes=nope"})
        assert malformed_range.status_code == 416
        assert malformed_range.headers["content-range"] == f"bytes */{len(stored_bytes)}"

        wait_for_thumbnail(client, media_id)
        thumb_response = client.get(f"/t/{media_id}.jpg")
        assert thumb_response.status_code == 200
        assert thumb_response.headers["content-type"] == "image/jpeg"
        assert thumb_response.content.startswith(b"\xff\xd8")

        zip_response = client.get(f"/api/v1/album/{album_id}/zip")
        assert zip_response.status_code == 200
        assert zip_response.headers["content-disposition"] == f'attachment; filename="{album_id}.zip"'
        with ZipFile(BytesIO(zip_response.content)) as archive:
            assert archive.namelist() == ["sample.png"]
            assert archive.read("sample.png") == stored_bytes

        forbidden_delete = client.delete(f"/api/v1/album/{album_id}")
        assert forbidden_delete.status_code == 403

        delete_response = client.delete(f"/api/v1/album/{album_id}", params={"delete_token": delete_token})
        assert delete_response.status_code == 200
        assert delete_response.json()["deleted"] is True

        deleted_album_response = client.get(f"/api/v1/album/{album_id}")
        assert deleted_album_response.status_code == 404


def test_multi_file_upload_reuses_album_and_delete_removes_media(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[
                ("file", ("one.png", BytesIO(PNG_1X1), "image/png")),
                ("file", ("two.png", BytesIO(PNG_1X1), "image/png")),
            ],
            data={"title": "Batch"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert len(payload["items"]) == 2

        album_response = client.get(f"/api/v1/album/{payload['album_id']}")
        assert album_response.status_code == 200
        album_payload = album_response.json()
        assert album_payload["item_count"] == 2
        assert [item["position"] for item in album_payload["items"]] == [1000, 2000]

        delete_response = client.delete(
            f"/api/v1/album/{payload['album_id']}",
            params={"delete_token": payload["manage_url"].split("token=")[1]},
        )
        assert delete_response.status_code == 200

        for item in payload["items"]:
            media_id = item["media_id"]
            assert client.get(f"/i/{media_id}.png").status_code == 404


def test_anonymous_manage_token_can_append_and_edit_album(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/upload",
            files=[("file", ("one.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Anonymous Album"},
        )
        assert created.status_code == 200
        payload = created.json()
        delete_token = payload["manage_url"].split("token=")[1]

        appended = client.post(
            "/api/v1/upload",
            files=[("file", ("two.png", BytesIO(PNG_1X1), "image/png"))],
            data={"album_id": payload["album_id"], "delete_token": delete_token},
        )
        assert appended.status_code == 200
        assert appended.json()["album_id"] == payload["album_id"]

        patched = client.patch(
            f"/api/v1/album/{payload['album_id']}",
            params={"delete_token": delete_token},
            json={"title": "Anonymous Album Edited"},
        )
        assert patched.status_code == 200
        assert patched.json()["title"] == "Anonymous Album Edited"
        assert patched.json()["item_count"] == 2


def test_api_key_upload_returns_sharex_delete_url_and_delete_endpoint_works(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("SECRET_KEY", "sharex-delete-secret")

    _, api_key = create_user_and_api_key(capsys, username="sharexdelete", email="sharexdelete@example.com")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            headers={"Authorization": f"Bearer {api_key}"},
            files=[("file", ("one.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "ShareX upload"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["manage_url"] is None
        assert payload["delete_url"].startswith(f"http://testserver/api/v1/album/{payload['album_id']}/delete?token=")

        deleted = client.get(payload["delete_url"])
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True

        missing = client.get(f"/api/v1/album/{payload['album_id']}")
        assert missing.status_code == 404


def test_sharex_delete_endpoint_rejects_tampered_token(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("SECRET_KEY", "sharex-delete-secret")

    _, api_key = create_user_and_api_key(capsys, username="sharexreject", email="sharexreject@example.com")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            headers={"Authorization": f"Bearer {api_key}"},
            files=[("file", ("one.png", BytesIO(PNG_1X1), "image/png"))],
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["delete_url"] is not None

        tampered = payload["delete_url"][:-1] + ("0" if payload["delete_url"][-1] != "0" else "1")
        rejected = client.get(tampered)
        assert rejected.status_code == 403
        assert rejected.json()["detail"] == "Invalid ShareX deletion URL."


def test_invalid_image_upload_is_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("bad.png", BytesIO(b"not-an-image"), "image/png"))],
        )

        assert response.status_code == 415
        assert response.json()["detail"] == "Unsupported or invalid image file."


def test_truncated_jpeg_upload_is_rejected_without_500(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    payload = jpeg_bytes()[:-20]

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("bad.jpg", BytesIO(payload), "image/jpeg"))],
        )

        assert response.status_code == 415
        assert response.json()["detail"] == "Unsupported or invalid image file."


def test_invalid_video_upload_does_not_leak_ffmpeg_stderr_excerpt(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    def fake_probe(self, payload: bytes):
        raise VideoProcessingError(
            tool="ffprobe",
            exit_code=1,
            stderr_excerpt="ffmpeg-internal-secret-tail",
        )

    monkeypatch.setattr("imghost.processors.Mp4Processor._probe", fake_probe)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("bad.mp4", BytesIO(b"not-a-real-video"), "video/mp4"))],
        )

        assert response.status_code == 415
        assert response.json()["detail"] == "Unsupported or invalid video file."
        assert "ffmpeg-internal-secret-tail" not in response.text


def test_invalid_weird_video_metadata_still_returns_coarse_415(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    async def fake_validate(self, payload: bytes):
        from imghost.processors import ValidationResult

        return ValidationResult(ok=False, rejection_reason="Unsupported or invalid video file.")

    monkeypatch.setattr("imghost.processors.Mp4Processor.validate", fake_validate)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("weird.mp4", BytesIO(b"weird-video"), "video/mp4"))],
        )

        assert response.status_code == 415
        assert response.json()["detail"] == "Unsupported or invalid video file."


def test_malformed_video_upload_returns_415_and_persists_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    async def fake_validate(self, payload: bytes) -> ValidationResult:
        return ValidationResult(ok=False, rejection_reason="Unsupported or invalid video file.")

    monkeypatch.setattr("imghost.processors.Mp4Processor.validate", fake_validate)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("bad.mp4", BytesIO(b"bad-video"), "video/mp4"))],
            data={"title": "Should Not Persist"},
        )

        assert response.status_code == 415
        assert response.json()["detail"] == "Unsupported or invalid video file."

        state = client.app.state.imghost
        albums = client.portal.call(state.repository.list_albums)
        media_items = client.portal.call(state.repository.list_all_media)
        assert albums == []
        assert media_items == []

    assert not list(tmp_path.rglob("originals/**/*"))
    assert not list(tmp_path.rglob("thumbnails/**/*"))


def test_video_sanitize_timeout_returns_415_and_stores_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

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
        raise VideoProcessingError(tool="ffmpeg", timed_out=True, stderr_excerpt="slow tail")

    monkeypatch.setattr("imghost.processors.Mp4Processor.validate", fake_validate)
    monkeypatch.setattr("imghost.processors.Mp4Processor.extract_metadata", fake_extract_metadata)
    monkeypatch.setattr("imghost.processors.Mp4Processor.sanitize", fake_sanitize)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("slow.mp4", BytesIO(b"slow-video"), "video/mp4"))],
            data={"title": "Should Not Persist"},
        )

        assert response.status_code == 415
        assert response.json()["detail"] == "Unsupported or invalid video file."
        assert "slow tail" not in response.text

        state = client.app.state.imghost
        albums = client.portal.call(state.repository.list_albums)
        media_items = client.portal.call(state.repository.list_all_media)
        assert albums == []
        assert media_items == []

    assert not list(tmp_path.rglob("originals/**/*"))
    assert not list(tmp_path.rglob("thumbnails/**/*"))


def test_upload_over_limit_is_rejected_without_processing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "8")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("too-big.bin", BytesIO(b"0123456789"), "application/octet-stream"))],
        )

        assert response.status_code == 413
        assert response.json()["detail"] == "Upload exceeds V1 size limit."


def test_mpo_backed_jpeg_upload_is_normalized_to_jpg_and_thumbnails_succeed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("TASK_QUEUE_MODE", "sync")

    sample_path = Path(__file__).resolve().parent / "fixtures" / "IMG_1238.jpg"
    payload_bytes = sample_path.read_bytes()

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("IMG_1238.jpg", BytesIO(payload_bytes), "image/jpeg"))],
            data={"title": "MPO-backed JPEG"},
        )

        assert response.status_code == 200
        payload = response.json()
        media_id = payload["items"][0]["media_id"]
        media_url = payload["items"][0]["media_url"]
        assert media_url.endswith(".jpg")
        assert payload["items"][0]["thumb_status"] == "done"

        album_response = client.get(f"/api/v1/album/{payload['album_id']}")
        assert album_response.status_code == 200
        item = album_response.json()["items"][0]
        assert item["media_url"].endswith(".jpg")
        assert item["thumb_status"] == "done"

        state = client.app.state.imghost
        media = client.portal.call(state.repository.get_media, media_id)
        assert media is not None
        assert media.format == "jpeg"
        assert media.mime_type == "image/jpeg"
        assert item["thumb_url"].endswith(".jpg")

        media_response = client.get(f"/i/{media_id}.jpg")
        assert media_response.status_code == 200
        assert media_response.headers["content-type"] == "image/jpeg"

        thumb_response = client.get(f"/t/{media_id}.jpg")
        assert thumb_response.status_code == 200
        assert thumb_response.headers["content-type"] == "image/jpeg"


def test_mislabeled_jpeg_upload_is_stored_as_sanitized_png_output(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("TASK_QUEUE_MODE", "sync")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("mislabeled.png", BytesIO(jpeg_bytes()), "image/png"))],
            data={"title": "Mislabeled JPEG"},
        )

        assert response.status_code == 200
        payload = response.json()
        item = payload["items"][0]
        media_id = item["media_id"]
        assert item["media_url"].endswith(".png")

        state = client.app.state.imghost
        media = client.portal.call(state.repository.get_media, media_id)
        assert media is not None
        assert media.format == "png"
        assert media.mime_type == "image/png"

        media_response = client.get(f"/i/{media_id}.png")
        assert media_response.status_code == 200
        assert media_response.headers["content-type"] == "image/png"


def test_mislabeled_png_upload_is_stored_as_sanitized_jpeg_output(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("TASK_QUEUE_MODE", "sync")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("mislabeled.jpg", BytesIO(PNG_1X1), "image/jpeg"))],
            data={"title": "Mislabeled PNG"},
        )

        assert response.status_code == 200
        payload = response.json()
        item = payload["items"][0]
        media_id = item["media_id"]
        assert item["media_url"].endswith(".jpg")

        state = client.app.state.imghost
        media = client.portal.call(state.repository.get_media, media_id)
        assert media is not None
        assert media.format == "jpeg"
        assert media.mime_type == "image/jpeg"

        media_response = client.get(f"/i/{media_id}.jpg")
        assert media_response.status_code == 200
        assert media_response.headers["content-type"] == "image/jpeg"


def test_heic_upload_is_normalized_to_browser_safe_jpeg_output(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("TASK_QUEUE_MODE", "sync")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.heic", BytesIO(modern_image_bytes(format_name="HEIF")), "image/heic"))],
            data={"title": "HEIC"},
        )

        assert response.status_code == 200
        payload = response.json()
        item = payload["items"][0]
        media_id = item["media_id"]
        assert item["media_url"].endswith(".jpg")

        state = client.app.state.imghost
        media = client.portal.call(state.repository.get_media, media_id)
        assert media is not None
        assert media.format == "jpeg"
        assert media.mime_type == "image/jpeg"

        media_response = client.get(f"/i/{media_id}.jpg")
        assert media_response.status_code == 200
        assert media_response.headers["content-type"] == "image/jpeg"

        thumb_response = client.get(f"/t/{media_id}.jpg")
        assert thumb_response.status_code == 200
        assert thumb_response.headers["content-type"] == "image/jpeg"


def test_avif_upload_with_alpha_is_normalized_to_png_output(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("TASK_QUEUE_MODE", "sync")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.avif", BytesIO(modern_image_bytes(mode="RGBA", color=(255, 0, 0, 96), format_name="AVIF")), "image/avif"))],
            data={"title": "AVIF"},
        )

        assert response.status_code == 200
        payload = response.json()
        item = payload["items"][0]
        media_id = item["media_id"]
        assert item["media_url"].endswith(".png")

        state = client.app.state.imghost
        media = client.portal.call(state.repository.get_media, media_id)
        assert media is not None
        assert media.format == "png"
        assert media.mime_type == "image/png"

        media_response = client.get(f"/i/{media_id}.png")
        assert media_response.status_code == 200
        assert media_response.headers["content-type"] == "image/png"

        thumb_response = client.get(f"/t/{media_id}.jpg")
        assert thumb_response.status_code == 200
        assert thumb_response.headers["content-type"] == "image/jpeg"


def test_album_payload_and_page_show_video_compatibility_warning(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("TASK_QUEUE_MODE", "sync")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Compat"},
        )
        assert response.status_code == 200
        payload = response.json()
        state = client.app.state.imghost
        media = client.portal.call(state.repository.get_media, payload["media_id"])
        assert media is not None
        media.media_type = "video"
        media.format = "mov"
        media.mime_type = "video/quicktime"
        media.codec_hint = "hevc"
        media.thumb_status = "done"
        media.thumb_key = None
        media.thumb_is_orig = True
        client.portal.call(state.repository.update_media, media)

        album_response = client.get(f"/api/v1/album/{payload['album_id']}")
        assert album_response.status_code == 200
        item = album_response.json()["items"][0]
        assert item["codec_hint"] == "hevc"
        assert "HEVC encoding" in item["compat_warning"]

        page_response = client.get(f"/a/{payload['album_id']}")
        assert page_response.status_code == 200
        assert "HEVC encoding" in page_response.text
