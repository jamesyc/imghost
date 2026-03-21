from io import BytesIO

from fastapi.testclient import TestClient

from imghost.main import app

from .helpers import (
    PNG_1X1,
    browser_session_headers,
    create_admin_and_api_key,
    create_user_and_api_key,
    set_user_password,
    wait_for_thumbnail,
)


def test_smoke_anonymous_upload_album_and_media_serving(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Smoke Album"},
        )

        assert response.status_code == 200
        payload = response.json()

        album = client.get(f"/api/v1/album/{payload['album_id']}")
        assert album.status_code == 200
        assert album.json()["item_count"] == 1

        media = client.get(f"/i/{payload['media_id']}.png")
        assert media.status_code == 200

        wait_for_thumbnail(client, payload["media_id"])
        thumb = client.get(f"/t/{payload['media_id']}.jpg")
        assert thumb.status_code == 200


def test_smoke_browser_login_and_authenticated_upload(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, _ = create_user_and_api_key(capsys, username="smokeuser", email="smokeuser@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, user_id, "open-sesame")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "smokeuser@example.com", "password": "open-sesame"},
        )
        assert login.status_code == 200

        me = client.get("/api/v1/user/me")
        assert me.status_code == 200
        assert me.json()["id"] == user_id

        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            headers=browser_session_headers("https://testserver", "/dashboard"),
        )
        assert upload.status_code == 200
        assert upload.json()["album_id"]


def test_smoke_admin_can_read_and_update_runtime_config(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="smokecfgadmin", email="smokecfgadmin@example.com")

    with TestClient(app) as client:
        initial = client.get("/api/v1/admin/config", headers={"Authorization": f"Bearer {admin_key}"})
        assert initial.status_code == 200

        updated = client.patch(
            "/api/v1/admin/config",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"allow_registration": False},
        )
        assert updated.status_code == 200
        assert updated.json()["allow_registration"]["value"] is False
