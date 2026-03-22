from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from fastapi.testclient import TestClient

from imghost.main import app

from .helpers import PNG_1X1, wait_for_thumbnail


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
