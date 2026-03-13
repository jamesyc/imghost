import json
import hashlib
from datetime import datetime, timedelta
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


def create_user_and_api_key(capsys, *, username: str, email: str) -> tuple[str, str]:
    assert cli_main(["create-user", "--username", username, "--email", email]) == 0
    create_output = capsys.readouterr().out.strip().splitlines()
    user_id = create_output[-1].split(": ", 1)[1]
    assert cli_main(["issue-api-key", "--user-id", user_id]) == 0
    issue_lines = capsys.readouterr().out.strip().splitlines()
    api_key = issue_lines[-1].split(": ", 1)[1]
    return user_id, api_key


def create_admin_and_api_key(capsys, *, username: str, email: str) -> tuple[str, str]:
    assert cli_main(["create-user", "--username", username, "--email", email, "--admin"]) == 0
    create_output = capsys.readouterr().out.strip().splitlines()
    user_id = create_output[-1].split(": ", 1)[1]
    assert cli_main(["issue-api-key", "--user-id", user_id]) == 0
    issue_lines = capsys.readouterr().out.strip().splitlines()
    api_key = issue_lines[-1].split(": ", 1)[1]
    return user_id, api_key


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

        state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        media = state["media"][payload["media_id"]]
        media["media_type"] = "video"
        media["format"] = "mov"
        media["mime_type"] = "video/quicktime"
        media["codec_hint"] = "hevc"
        media["thumb_status"] = "done"
        media["thumb_key"] = None
        media["thumb_is_orig"] = True
        (tmp_path / "state.json").write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

        album_response = client.get(f"/api/v1/album/{payload['album_id']}")
        assert album_response.status_code == 200
        item = album_response.json()["items"][0]
        assert item["codec_hint"] == "hevc"
        assert "HEVC encoding" in item["compat_warning"]

        page_response = client.get(f"/a/{payload['album_id']}")
        assert page_response.status_code == 200
        assert "HEVC encoding" in page_response.text


def test_api_key_upload_creates_user_album_and_current_user_view(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    user_id, api_key = create_user_and_api_key(capsys, username="alice", email="alice@example.com")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert "delete_token=" not in payload["delete_url"]
        wait_for_thumbnail(client, payload["media_id"])

        me = client.get("/api/v1/user/me", headers={"Authorization": f"Bearer {api_key}"})
        assert me.status_code == 200
        me_payload = me.json()
        assert me_payload["id"] == user_id
        assert me_payload["username"] == "alice"
        assert me_payload["has_api_key"] is True
        assert me_payload["storage_used_bytes"] > 0

        state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert state["albums"][payload["album_id"]]["user_id"] == user_id
        assert state["albums"][payload["album_id"]]["expires_at"] is None
        assert state["media"][payload["media_id"]]["user_id"] == user_id


def test_api_key_upload_requires_single_new_album_request(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, api_key = create_user_and_api_key(capsys, username="bob", email="bob@example.com")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[
                ("file", ("one.png", BytesIO(PNG_1X1), "image/png")),
                ("file", ("two.png", BytesIO(PNG_1X1), "image/png")),
            ],
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "API key uploads must contain exactly one file."


def test_api_key_can_rotate_and_delete_album_via_get(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, api_key = create_user_and_api_key(capsys, username="carol", email="carol@example.com")

    with TestClient(app) as client:
        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert upload.status_code == 200
        payload = upload.json()

        rotated = client.post("/api/v1/user/me/api-key", headers={"Authorization": f"Bearer {api_key}"})
        assert rotated.status_code == 200
        new_api_key = rotated.json()["api_key"]
        assert new_api_key != api_key

        old_me = client.get("/api/v1/user/me", headers={"Authorization": f"Bearer {api_key}"})
        assert old_me.status_code == 401

        delete = client.get(
            f"/api/v1/album/{payload['album_id']}/delete",
            headers={"Authorization": f"Bearer {new_api_key}"},
        )
        assert delete.status_code == 200
        assert delete.json()["deleted"] is True


def test_sharex_config_download_embeds_active_api_key(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, api_key = create_user_and_api_key(capsys, username="dana", email="dana@example.com")

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/user/me/sharex-config",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert response.status_code == 200
        assert response.headers["content-disposition"] == 'attachment; filename="imghost.sxcu"'
        payload = response.json()
        assert payload["RequestURL"] == "http://testserver/api/v1/upload"
        assert payload["Headers"]["Authorization"] == f"Bearer {api_key}"
        assert payload["DeletionURL"] == "$json:delete_url$"


def test_delete_current_user_removes_content_and_invalidates_api_key(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    user_id, api_key = create_user_and_api_key(capsys, username="erin", email="erin@example.com")

    with TestClient(app) as client:
        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert upload.status_code == 200
        payload = upload.json()

        delete = client.delete("/api/v1/user/me", headers={"Authorization": f"Bearer {api_key}"})
        assert delete.status_code == 200
        deleted = delete.json()
        assert deleted["deleted"] is True
        assert deleted["user_id"] == user_id
        assert deleted["album_count"] == 1
        assert deleted["media_count"] == 1

        me = client.get("/api/v1/user/me", headers={"Authorization": f"Bearer {api_key}"})
        assert me.status_code == 401

        state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert user_id not in state["users"]
        assert not state["api_keys"]
        assert payload["album_id"] not in state["albums"]
        assert payload["media_id"] not in state["media"]
        assert client.get(f"/i/{payload['media_id']}.png").status_code == 404


def test_user_quota_rejects_authenticated_upload_when_exceeded(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, api_key = create_user_and_api_key(capsys, username="frank", email="frank@example.com")
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    user_id = next(iter(state["users"]))
    state["users"][user_id]["quota_bytes"] = 1
    (tmp_path / "state.json").write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert response.status_code == 413
        assert response.json()["detail"] == "User storage quota reached."


def test_server_quota_rejects_upload_when_exceeded(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("SERVER_QUOTA_BYTES", "1")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
        )
        assert response.status_code == 507
        assert response.json()["detail"] == "Server storage quota reached."


def test_admin_user_management_and_stats(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="admin", email="admin@example.com")
    user_id, user_key = create_user_and_api_key(capsys, username="grace", email="grace@example.com")

    with TestClient(app) as client:
        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            headers={"Authorization": f"Bearer {user_key}"},
        )
        assert upload.status_code == 200

        users = client.get("/api/v1/admin/users", headers={"Authorization": f"Bearer {admin_key}"})
        assert users.status_code == 200
        listed = {item["id"]: item for item in users.json()}
        assert listed[user_id]["storage_used_bytes"] > 0
        assert listed[user_id]["suspended"] is False

        created = client.post(
            "/api/v1/admin/users",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={
                "username": "harry",
                "email": "harry@example.com",
                "password": "secret",
                "quota_bytes": 12345,
            },
        )
        assert created.status_code == 201
        created_user = created.json()
        assert created_user["quota_bytes"] == 12345

        patched = client.patch(
            f"/api/v1/admin/users/{created_user['id']}",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"suspended": True, "quota_bytes": 999},
        )
        assert patched.status_code == 200
        assert patched.json()["suspended"] is True
        assert patched.json()["quota_bytes"] == 999

        stats = client.get("/api/v1/admin/stats", headers={"Authorization": f"Bearer {admin_key}"})
        assert stats.status_code == 200
        stats_payload = stats.json()
        assert stats_payload["user_count"] >= 2
        assert stats_payload["total_storage_used_bytes"] > 0

        deleted = client.delete(
            f"/api/v1/admin/users/{created_user['id']}",
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True

        forbidden = client.get("/api/v1/admin/users", headers={"Authorization": f"Bearer {user_key}"})
        assert forbidden.status_code == 403


def test_user_can_change_password_with_current_password(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    user_id, api_key = create_user_and_api_key(capsys, username="iris", email="iris@example.com")
    state_path = tmp_path / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["users"][user_id]["password_hash"] = hashlib.sha256(b"old-pass").hexdigest()
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

    with TestClient(app) as client:
        bad = client.patch(
            "/api/v1/user/me/password",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"current_password": "wrong", "new_password": "new-pass"},
        )
        assert bad.status_code == 403

        good = client.patch(
            "/api/v1/user/me/password",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"current_password": "old-pass", "new_password": "new-pass"},
        )
        assert good.status_code == 200
        assert good.json()["updated"] is True

        updated_state = json.loads(state_path.read_text(encoding="utf-8"))
        assert updated_state["users"][user_id]["password_hash"] == hashlib.sha256(b"new-pass").hexdigest()


def test_local_login_sets_session_cookie_and_authenticates_browser_flow(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, _ = create_user_and_api_key(capsys, username="kira", email="kira@example.com")
    state_path = tmp_path / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["users"][user_id]["password_hash"] = hashlib.sha256(b"open-sesame").hexdigest()
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "kira@example.com", "password": "open-sesame"},
        )
        assert login.status_code == 200
        assert "imghost_session=" in login.headers["set-cookie"]
        assert "Max-Age=" in login.headers["set-cookie"]
        assert login.json()["authenticated"] is True

        me = client.get("/api/v1/user/me")
        assert me.status_code == 200
        assert me.json()["id"] == user_id

        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
        )
        assert upload.status_code == 200
        payload = upload.json()
        wait_for_thumbnail(client, payload["media_id"])

        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        assert persisted["albums"][payload["album_id"]]["user_id"] == user_id
        assert persisted["albums"][payload["album_id"]]["expires_at"] is None
        assert persisted["albums"][payload["album_id"]]["delete_token"] is None

        logout = client.post("/api/v1/auth/logout")
        assert logout.status_code == 200
        assert logout.json()["authenticated"] is False

        after_logout = client.get("/api/v1/user/me")
        assert after_logout.status_code == 401


def test_local_login_supports_username_session_cookie_and_sharex_requires_api_key(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, _ = create_user_and_api_key(capsys, username="lena", email="lena@example.com")
    state_path = tmp_path / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["users"][user_id]["password_hash"] = hashlib.sha256(b"letmein").hexdigest()
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

    with TestClient(app) as client:
        bad = client.post(
            "/api/v1/auth/login",
            json={"login": "lena", "password": "wrong"},
        )
        assert bad.status_code == 401

        login = client.post(
            "/api/v1/auth/login",
            json={"login": "lena", "password": "letmein", "remember_me": False},
        )
        assert login.status_code == 200
        assert "imghost_session=" in login.headers["set-cookie"]
        assert "Max-Age=" not in login.headers["set-cookie"]

        sharex = client.get("/api/v1/user/me/sharex-config")
        assert sharex.status_code == 400
        assert sharex.json()["detail"] == "ShareX config download requires API key authentication."


def test_registration_creates_user_session_and_audit_entry(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    admin_id, admin_key = create_admin_and_api_key(capsys, username="regadmin", email="regadmin@example.com")

    with TestClient(app) as client:
        registered = client.post(
            "/api/v1/auth/register",
            headers={"X-Correlation-ID": "register-flow"},
            json={
                "username": "newuser",
                "email": "newuser@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200
        assert "imghost_session=" in registered.headers["set-cookie"]
        payload = registered.json()
        assert payload["authenticated"] is True
        user_id = payload["user"]["id"]
        assert payload["user"]["username"] == "newuser"

        me = client.get("/api/v1/user/me")
        assert me.status_code == 200
        assert me.json()["id"] == user_id

        audit = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "user_created", "correlation_id": "register-flow"},
        )
        assert audit.status_code == 200
        audit_payload = audit.json()
        assert len(audit_payload) == 1
        assert audit_payload[0]["actor_id"] == user_id
        assert audit_payload[0]["metadata"]["method"] == "registration"
        assert audit_payload[0]["target_id"] == user_id


def test_registration_respects_allow_registration_runtime_config(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="regcfgadmin", email="regcfgadmin@example.com")

    with TestClient(app) as client:
        disabled = client.patch(
            "/api/v1/admin/config",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"allow_registration": False},
        )
        assert disabled.status_code == 200

        response = client.post(
            "/api/v1/auth/register",
            json={
                "username": "blocked",
                "email": "blocked@example.com",
                "password": "secret-pass",
            },
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "Registration is disabled."


def test_admin_album_management_lists_sets_expiry_and_deletes(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="admin2", email="admin2@example.com")
    user_id, user_key = create_user_and_api_key(capsys, username="jules", email="jules@example.com")

    with TestClient(app) as client:
        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            headers={"Authorization": f"Bearer {user_key}"},
            data={"title": "Managed"},
        )
        assert upload.status_code == 200
        payload = upload.json()

        albums = client.get("/api/v1/admin/albums", headers={"Authorization": f"Bearer {admin_key}"})
        assert albums.status_code == 200
        album = next(item for item in albums.json() if item["id"] == payload["album_id"])
        assert album["owner_username"] == "jules"
        assert album["user_id"] == user_id
        assert album["item_count"] == 1

        expiry = (utcnow().replace(microsecond=0)).isoformat()
        patched = client.patch(
            f"/api/v1/admin/albums/{payload['album_id']}",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"expires_at": expiry},
        )
        assert patched.status_code == 200
        assert patched.json()["expires_at"] == expiry

        cleared = client.patch(
            f"/api/v1/admin/albums/{payload['album_id']}",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"expires_at": None},
        )
        assert cleared.status_code == 200
        assert cleared.json()["expires_at"] is None

        deleted = client.delete(
            f"/api/v1/admin/albums/{payload['album_id']}",
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True

        assert client.get("/api/v1/admin/albums", headers={"Authorization": f"Bearer {user_key}"}).status_code == 403


def test_admin_audit_log_tracks_events_and_supports_filters(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    admin_id, admin_key = create_admin_and_api_key(capsys, username="auditadmin", email="audit-admin@example.com")
    user_id, user_key = create_user_and_api_key(capsys, username="audited", email="audited@example.com")

    with TestClient(app) as client:
        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            headers={"Authorization": f"Bearer {user_key}", "X-Correlation-ID": "audit-upload"},
        )
        assert upload.status_code == 200
        album_id = upload.json()["album_id"]

        suspend = client.patch(
            f"/api/v1/admin/users/{user_id}",
            headers={"Authorization": f"Bearer {admin_key}", "X-Correlation-ID": "audit-suspend"},
            json={"suspended": True},
        )
        assert suspend.status_code == 200

        expiry = client.patch(
            f"/api/v1/admin/albums/{album_id}",
            headers={"Authorization": f"Bearer {admin_key}", "X-Correlation-ID": "audit-expiry"},
            json={"expires_at": utcnow().replace(microsecond=0).isoformat()},
        )
        assert expiry.status_code == 200

        upload_events = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"correlation_id": "audit-upload"},
        )
        assert upload_events.status_code == 200
        upload_payload = upload_events.json()
        assert [item["event_type"] for item in upload_payload] == ["media_uploaded", "album_created"]
        assert all(item["actor_id"] == user_id for item in upload_payload)
        assert all(item["correlation_id"] == "audit-upload" for item in upload_payload)

        suspended_events = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "user_suspended", "actor_id": admin_id},
        )
        assert suspended_events.status_code == 200
        suspended_payload = suspended_events.json()
        assert len(suspended_payload) == 1
        assert suspended_payload[0]["target_type"] == "user"
        assert suspended_payload[0]["target_id"] == user_id
        assert suspended_payload[0]["metadata"]["suspended"] is True

        ranged = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"after": "2999-01-01T00:00:00+00:00"},
        )
        assert ranged.status_code == 200
        assert ranged.json() == []

        forbidden = client.get("/api/v1/admin/audit", headers={"Authorization": f"Bearer {user_key}"})
        assert forbidden.status_code == 403


def test_admin_config_can_be_read_updated_and_audited(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    admin_id, admin_key = create_admin_and_api_key(capsys, username="cfgadmin", email="cfgadmin@example.com")

    with TestClient(app) as client:
        initial = client.get("/api/v1/admin/config", headers={"Authorization": f"Bearer {admin_key}"})
        assert initial.status_code == 200
        initial_payload = initial.json()
        assert initial_payload["allow_registration"]["value"] is True
        assert initial_payload["anon_upload_enabled"]["value"] is True
        assert initial_payload["anon_expiry_hours"]["value"] == 24

        updated = client.patch(
            "/api/v1/admin/config",
            headers={"Authorization": f"Bearer {admin_key}", "X-Correlation-ID": "cfg-patch"},
            json={
                "allow_registration": False,
                "anon_upload_enabled": False,
                "anon_expiry_hours": 72,
                "rate_limit_user_rpm": 99,
            },
        )
        assert updated.status_code == 200
        updated_payload = updated.json()
        assert updated_payload["allow_registration"]["value"] is False
        assert updated_payload["allow_registration"]["source"] == "runtime"
        assert updated_payload["anon_upload_enabled"]["value"] is False
        assert updated_payload["anon_expiry_hours"]["value"] == 72
        assert updated_payload["rate_limit_user_rpm"]["value"] == 99

        audit = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "config_changed", "actor_id": admin_id, "correlation_id": "cfg-patch"},
        )
        assert audit.status_code == 200
        audit_payload = audit.json()
        changed_keys = {item["metadata"]["key"] for item in audit_payload}
        assert {"allow_registration", "anon_upload_enabled", "anon_expiry_hours", "rate_limit_user_rpm"} <= changed_keys


def test_runtime_config_can_disable_anon_uploads_and_override_expiry(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="cfgadmin2", email="cfgadmin2@example.com")

    with TestClient(app) as client:
        expiry_config = client.patch(
            "/api/v1/admin/config",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"anon_expiry_hours": 48},
        )
        assert expiry_config.status_code == 200

        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
        )
        assert upload.status_code == 200
        album_id = upload.json()["album_id"]

        album = client.get(f"/api/v1/album/{album_id}")
        assert album.status_code == 200
        expires_at = datetime.fromisoformat(album.json()["expires_at"])
        delta = expires_at - utcnow()
        assert timedelta(hours=47, minutes=50) <= delta <= timedelta(hours=48, minutes=10)

        disabled = client.patch(
            "/api/v1/admin/config",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"anon_upload_enabled": False},
        )
        assert disabled.status_code == 200

        blocked = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
        )
        assert blocked.status_code == 403
        assert blocked.json()["detail"] == "Anonymous uploads are disabled."


def test_locked_runtime_config_cannot_be_overridden(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("LOCK_ANON_EXPIRY", "true")

    _, admin_key = create_admin_and_api_key(capsys, username="cfgadmin3", email="cfgadmin3@example.com")

    with TestClient(app) as client:
        read = client.get("/api/v1/admin/config", headers={"Authorization": f"Bearer {admin_key}"})
        assert read.status_code == 200
        assert read.json()["anon_expiry_hours"]["locked"] is True
        assert read.json()["anon_expiry_hours"]["source"] == "locked"

        update = client.patch(
            "/api/v1/admin/config",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"anon_expiry_hours": 99},
        )
        assert update.status_code == 403
        assert update.json()["detail"] == "anon_expiry_hours is locked by environment configuration."
