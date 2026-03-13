import json
from io import BytesIO
from time import monotonic, sleep
from zipfile import ZipFile

from fastapi.testclient import TestClient

from imghost.__main__ import main as cli_main
from imghost.main import app
from imghost.models import utcnow

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDAT\x08\x99c\xf8\xcf"
    b"\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
)


def wait_for_thumbnail(client: TestClient, media_id: str, *, suffix: str = "jpg", timeout: float = 2.0) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        response = client.get(f"/t/{media_id}.{suffix}")
        if response.status_code == 200:
            return
        assert response.status_code == 202
        sleep(0.02)
    raise AssertionError(f"thumbnail for {media_id} was not ready within {timeout} seconds")


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
        delete_url = payload["delete_url"]
        assert payload["items"][0]["thumb_status"] in {"pending", "processing", "done"}

        album_response = client.get(f"/api/v1/album/{album_id}")
        assert album_response.status_code == 200
        assert album_response.json()["title"] == "V1 Album"
        assert album_response.json()["item_count"] == 1

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
        with ZipFile(BytesIO(zip_response.content)) as archive:
            assert archive.namelist() == ["sample.png"]
            assert archive.read("sample.png") == stored_bytes

        forbidden_delete = client.delete(f"/api/v1/album/{album_id}")
        assert forbidden_delete.status_code == 403

        delete_response = client.get(delete_url.replace("http://testserver", ""))
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
            params={"delete_token": payload["delete_url"].split("delete_token=")[1]},
        )
        assert delete_response.status_code == 200

        for item in payload["items"]:
            media_id = item["media_id"]
            assert client.get(f"/i/{media_id}.png").status_code == 404


def test_album_patch_reorder_and_media_delete_require_token(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[
                ("file", ("one.png", BytesIO(PNG_1X1), "image/png")),
                ("file", ("two.png", BytesIO(PNG_1X1), "image/png")),
                ("file", ("three.png", BytesIO(PNG_1X1), "image/png")),
            ],
            data={"title": "Batch"},
        )

        assert response.status_code == 200
        payload = response.json()
        album_id = payload["album_id"]
        delete_token = payload["delete_url"].split("delete_token=")[1]
        media_ids = [item["media_id"] for item in payload["items"]]

        forbidden_patch = client.patch(f"/api/v1/album/{album_id}", json={"title": "Edited"})
        assert forbidden_patch.status_code == 403

        patch_response = client.patch(
            f"/api/v1/album/{album_id}",
            params={"delete_token": delete_token},
            json={"title": "Edited", "cover_media_id": media_ids[2]},
        )
        assert patch_response.status_code == 200
        patched = patch_response.json()
        assert patched["title"] == "Edited"
        assert patched["cover_media_id"] == media_ids[2]
        assert patched["cover_url"].endswith(f"/i/{media_ids[2]}.png")

        order_response = client.patch(
            f"/api/v1/album/{album_id}/order",
            params={"delete_token": delete_token},
            json=[
                {"media_id": media_ids[2], "position": 999},
                {"media_id": media_ids[0], "position": 1000},
                {"media_id": media_ids[1], "position": 1001},
            ],
        )
        assert order_response.status_code == 200
        reordered = order_response.json()
        assert [item["id"] for item in reordered["items"]] == [media_ids[2], media_ids[0], media_ids[1]]
        assert [item["position"] for item in reordered["items"]] == [1000, 2000, 3000]

        forbidden_delete = client.delete(f"/api/v1/media/{media_ids[2]}")
        assert forbidden_delete.status_code == 403

        delete_response = client.delete(
            f"/api/v1/media/{media_ids[2]}",
            params={"delete_token": delete_token},
        )
        assert delete_response.status_code == 200
        assert delete_response.json()["album_deleted"] is False
        assert client.get(f"/i/{media_ids[2]}.png").status_code == 404

        album_response = client.get(f"/api/v1/album/{album_id}")
        assert album_response.status_code == 200
        album_payload = album_response.json()
        assert album_payload["item_count"] == 2
        assert album_payload["cover_media_id"] is None
        assert album_payload["cover_url"].endswith(f"/i/{media_ids[0]}.png")


def test_deleting_only_media_deletes_album(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("solo.png", BytesIO(PNG_1X1), "image/png"))],
        )

        assert response.status_code == 200
        payload = response.json()
        media_id = payload["media_id"]
        album_id = payload["album_id"]
        delete_token = payload["delete_url"].split("delete_token=")[1]

        delete_response = client.delete(
            f"/api/v1/media/{media_id}",
            params={"delete_token": delete_token},
        )
        assert delete_response.status_code == 200
        assert delete_response.json()["album_deleted"] is True

        assert client.get(f"/api/v1/album/{album_id}").status_code == 404


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

    monkeypatch.setenv("TASK_QUEUE_MODE", "async")
    state_path = tmp_path / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    media = state["media"][media_id]
    media["thumb_status"] = "processing"
    media["thumb_key"] = None
    media["thumb_size"] = None
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

    thumb_path = tmp_path / "thumbnails"
    for existing in thumb_path.glob(f"{media_id}.*"):
        existing.unlink()

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

    state_path = tmp_path / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    album = state["albums"][payload["album_id"]]
    album["expires_at"] = (utcnow().replace(microsecond=0)).isoformat()
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

    exit_code = cli_main(["prune", "--dry-run"])
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "prune dry-run: albums=1 items=1" in output
    assert payload["album_id"] in output

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["album_id"] in persisted["albums"]
    assert payload["media_id"] in persisted["media"]
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

    state_path = tmp_path / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["albums"][payload["album_id"]]["expires_at"] = (utcnow().replace(microsecond=0)).isoformat()
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

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

    media_path = next((tmp_path / "originals" / "anon").glob(f"{media_id}.*"))
    media_path.write_bytes(b"broken")
    state_path = tmp_path / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    media = state["media"][media_id]
    media["thumb_status"] = "failed"
    media["thumb_key"] = None
    media["thumb_size"] = None
    media["thumb_is_orig"] = False
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    for existing in (tmp_path / "thumbnails").glob(f"{media_id}.*"):
        existing.unlink()

    media_path.write_bytes(PNG_1X1)
    exit_code = cli_main(["retry-thumbnails"])
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "re-enqueued thumbnails: 1" in output

    with TestClient(app) as client:
        wait_for_thumbnail(client, media_id)
