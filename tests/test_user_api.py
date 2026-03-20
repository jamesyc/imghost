from io import BytesIO

from fastapi.testclient import TestClient

from imghost.main import app

from .helpers import (
    PNG_1X1,
    create_user_and_api_key,
    get_album_record,
    get_media_record,
    get_user_record,
    wait_for_thumbnail,
)


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
        assert me_payload["api_key_created_at"] is not None
        assert me_payload["album_count"] == 1
        assert me_payload["media_count"] == 1
        assert me_payload["storage_used_bytes"] > 0

        album = get_album_record(client, payload["album_id"])
        media = get_media_record(client, payload["media_id"])
        assert album is not None
        assert media is not None
        assert album.user_id == user_id
        assert album.expires_at is None
        assert media.user_id == user_id


def test_current_user_albums_endpoint_lists_owned_albums_for_dashboard(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, api_key = create_user_and_api_key(capsys, username="albumsfeed", email="albumsfeed@example.com")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/upload",
            files=[("file", ("one.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Owned One"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert created.status_code == 200
        album_id = created.json()["album_id"]

        listed = client.get("/api/v1/user/me/albums", headers={"Authorization": f"Bearer {api_key}"})
        assert listed.status_code == 200
        payload = listed.json()
        assert len(payload) == 1
        assert payload[0]["id"] == album_id
        assert payload[0]["title"] == "Owned One"
        assert payload[0]["item_count"] == 1


def test_api_key_upload_can_create_multi_file_album_and_append_to_existing_owned_album(
    tmp_path, monkeypatch, capsys
) -> None:
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
            data={"title": "Owned Batch"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert response.status_code == 200
        payload = response.json()
        album_id = payload["album_id"]
        media_ids = [item["media_id"] for item in payload["items"]]
        assert len(media_ids) == 2
        assert payload["delete_url"].endswith(f"/api/v1/album/{album_id}/delete")
        assert "delete_token=" not in payload["delete_url"]

        album_response = client.get(f"/api/v1/album/{album_id}")
        assert album_response.status_code == 200
        album_payload = album_response.json()
        assert album_payload["title"] == "Owned Batch"
        assert album_payload["item_count"] == 2
        assert [item["id"] for item in album_payload["items"]] == media_ids

        appended = client.post(
            "/api/v1/upload",
            files=[("file", ("three.png", BytesIO(PNG_1X1), "image/png"))],
            data={"album_id": album_id},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert appended.status_code == 200
        appended_payload = appended.json()
        assert appended_payload["album_id"] == album_id
        assert len(appended_payload["items"]) == 1

        refreshed = client.get(f"/api/v1/album/{album_id}")
        assert refreshed.status_code == 200
        refreshed_payload = refreshed.json()
        assert refreshed_payload["item_count"] == 3
        assert len(refreshed_payload["items"]) == 3
        assert refreshed_payload["items"][-1]["id"] == appended_payload["media_id"]

    _, stranger_key = create_user_and_api_key(capsys, username="eve", email="eve@example.com")

    with TestClient(app) as client:
        forbidden = client.post(
            "/api/v1/upload",
            files=[("file", ("evil.png", BytesIO(PNG_1X1), "image/png"))],
            data={"album_id": album_id},
            headers={"Authorization": f"Bearer {stranger_key}"},
        )
        assert forbidden.status_code == 403
        assert forbidden.json()["detail"] == "Album does not belong to authenticated user."


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


def test_sharex_config_download_from_browser_session_auto_issues_api_key(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    with TestClient(app, base_url="https://testserver") as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "sharexsession",
                "email": "sharexsession@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200

        response = client.get("/api/v1/user/me/sharex-config")
        assert response.status_code == 200
        payload = response.json()
        auth_header = payload["Headers"]["Authorization"]
        assert auth_header.startswith("Bearer ")
        issued_api_key = auth_header.removeprefix("Bearer ")

        me = client.get("/api/v1/user/me", headers={"Authorization": f"Bearer {issued_api_key}"})
        assert me.status_code == 200
        assert me.json()["username"] == "sharexsession"


def test_current_user_summary_without_api_key_reports_null_api_key_metadata(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    with TestClient(app, base_url="https://testserver") as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "nokeysummary",
                "email": "nokeysummary@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200

        me = client.get("/api/v1/user/me")
        assert me.status_code == 200
        payload = me.json()
        assert payload["username"] == "nokeysummary"
        assert payload["has_api_key"] is False
        assert payload["album_count"] == 0
        assert payload["media_count"] == 0
        assert payload["api_key_created_at"] is None
        assert payload["api_key_last_used_at"] is None


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

        assert get_user_record(client, user_id) is None
        assert get_album_record(client, payload["album_id"]) is None
        assert get_media_record(client, payload["media_id"]) is None
        assert client.get(f"/i/{payload['media_id']}.png").status_code == 404
