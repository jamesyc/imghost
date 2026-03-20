from datetime import datetime, timedelta
from io import BytesIO
from time import monotonic, sleep
from zipfile import ZipFile

import bcrypt
from fastapi.testclient import TestClient

from imghost.__main__ import main as cli_main
from imghost.main import app
from imghost.models import utcnow
from imghost.service import UserCreateInput

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


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def set_user_password(client: TestClient, user_id: str, password: str) -> None:
    state = client.app.state.imghost
    user = client.portal.call(state.repository.get_user, user_id)
    assert user is not None
    user.password_hash = _hash_password(password)
    user.updated_at = utcnow()
    client.portal.call(state.repository.update_user, user)


def update_album_record(client: TestClient, album_id: str, **updates) -> None:
    state = client.app.state.imghost
    album = client.portal.call(state.repository.get_album, album_id)
    assert album is not None
    for key, value in updates.items():
        setattr(album, key, value)
    album.updated_at = utcnow()
    client.portal.call(state.repository.update_album, album)


def update_media_record(client: TestClient, media_id: str, **updates) -> None:
    state = client.app.state.imghost
    media = client.portal.call(state.repository.get_media, media_id)
    assert media is not None
    for key, value in updates.items():
        setattr(media, key, value)
    client.portal.call(state.repository.update_media, media)


def get_album_record(client: TestClient, album_id: str):
    return client.portal.call(client.app.state.imghost.repository.get_album, album_id)


def get_media_record(client: TestClient, media_id: str):
    return client.portal.call(client.app.state.imghost.repository.get_media, media_id)


def get_user_record(client: TestClient, user_id: str):
    return client.portal.call(client.app.state.imghost.repository.get_user, user_id)


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
        assert zip_response.headers["content-disposition"] == f'attachment; filename="{album_id}.zip"'
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


def test_upload_uses_forwarded_public_origin_for_generated_urls(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://fallback.example.com")
    monkeypatch.setenv("TRUSTED_PUBLIC_ORIGINS", "https://imghost.b.example")

    with TestClient(app, base_url="http://backend") as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Forwarded Album"},
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "imghost.b.example",
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["album_url"].startswith("https://imghost.b.example/")
        assert payload["media_url"].startswith("https://imghost.b.example/")
        assert payload["thumb_url"].startswith("https://imghost.b.example/")
        assert payload["delete_url"].startswith("https://imghost.b.example/")


def test_upload_rejects_untrusted_forwarded_public_origin_and_falls_back_to_base_url(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://fallback.example.com")
    monkeypatch.setenv("TRUSTED_PUBLIC_ORIGINS", "https://trusted.example")

    with TestClient(app, base_url="http://backend") as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Fallback Album"},
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "evil.example",
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["album_url"].startswith("https://fallback.example.com/")
        assert payload["media_url"].startswith("https://fallback.example.com/")
        assert payload["thumb_url"].startswith("https://fallback.example.com/")
        assert payload["delete_url"].startswith("https://fallback.example.com/")


def test_upload_rejects_malformed_forwarded_public_origin_and_falls_back_to_base_url(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://fallback.example.com")
    monkeypatch.setenv("TRUSTED_PUBLIC_ORIGINS", "https://trusted.example")

    with TestClient(app, base_url="http://backend") as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Malformed Album"},
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "bad/path.example/evil",
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["album_url"].startswith("https://fallback.example.com/")


def test_upload_uses_direct_request_origin_when_trusted(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://fallback.example.com")
    monkeypatch.setenv("TRUSTED_PUBLIC_ORIGINS", "https://imghost.002015.xyz")

    with TestClient(app, base_url="https://imghost.002015.xyz") as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Direct Trusted Album"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["album_url"].startswith("https://imghost.002015.xyz/")


def test_upload_falls_back_to_base_url_when_direct_request_origin_is_untrusted(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://fallback.example.com")
    monkeypatch.setenv("TRUSTED_PUBLIC_ORIGINS", "https://trusted.example")

    with TestClient(app, base_url="https://imghost.002015.xyz") as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Direct Untrusted Album"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["album_url"].startswith("https://fallback.example.com/")


def test_index_page_reflects_runtime_config_and_session_state(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="indexadmin", email="indexadmin@example.com")

    with TestClient(app) as client:
        anonymous = client.get("/")
        assert anonymous.status_code == 200
        assert 'id="login-form"' in anonymous.text
        assert 'action="/api/v1/auth/login"' in anonymous.text
        assert 'method="post"' in anonymous.text
        assert 'id="register-form"' in anonymous.text
        assert 'action="/api/v1/auth/register"' in anonymous.text
        assert "Anonymous uploads currently expire after 24 hour(s)." in anonymous.text
        assert "const showMessage = (message) => {" in anonymous.text
        assert "const showMessage = (message) => {{" not in anonymous.text

        updated = client.patch(
            "/api/v1/admin/config",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"allow_registration": False, "anon_upload_enabled": False},
        )
        assert updated.status_code == 200

        disabled = client.get("/")
        assert disabled.status_code == 200
        assert "Registration is currently disabled." in disabled.text
        assert "Anonymous uploads are currently disabled. Sign in to upload." in disabled.text
        assert 'action="/api/v1/upload"' not in disabled.text


def test_index_page_shows_session_upload_state_when_logged_in(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")

    with TestClient(app, base_url="https://testserver") as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "browseruser",
                "email": "browser@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200

        page = client.get("/")
        assert page.status_code == 200
        assert "Logged in as <strong>browseruser</strong>." in page.text
        assert 'id="logout-form"' in page.text
        assert 'action="/api/v1/auth/logout"' in page.text
        assert 'method="post"' in page.text
        assert "Authenticated uploads do not expire by default." in page.text
        assert 'action="/api/v1/upload"' in page.text


def test_dashboard_page_focuses_on_uploads_albums_and_links_to_settings(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, _ = create_user_and_api_key(capsys, username="dashuser", email="dash@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, user_id, "open-sesame")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "dash@example.com", "password": "open-sesame"},
        )
        assert login.status_code == 200

        page = client.get("/dashboard")
        assert page.status_code == 200
        assert "User Dashboard" in page.text
        assert "API Key Mode" in page.text
        assert 'id="dashboard-upload-form"' in page.text
        assert 'id="owned-albums"' in page.text
        assert 'href="/settings"' in page.text
        assert 'id="change-password-form"' not in page.text


def test_settings_page_includes_account_api_key_password_and_delete_ui(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, _ = create_user_and_api_key(capsys, username="settingsuser", email="settings@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, user_id, "open-sesame")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "settings@example.com", "password": "open-sesame"},
        )
        assert login.status_code == 200

        page = client.get("/settings")
        assert page.status_code == 200
        assert "Settings" in page.text
        assert 'id="settings-account-summary"' in page.text
        assert 'id="reveal-api-key"' in page.text
        assert 'id="download-sharex-settings"' in page.text
        assert 'id="settings-password-form"' in page.text
        assert 'id="settings-delete-account-form"' in page.text


def test_admin_page_includes_admin_tools_ui(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    admin_id, _ = create_admin_and_api_key(capsys, username="uiadmin", email="uiadmin@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, admin_id, "admin-pass")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "uiadmin@example.com", "password": "admin-pass"},
        )
        assert login.status_code == 200

        page = client.get("/admin")
        assert page.status_code == 200
        assert "Admin Dashboard" in page.text
        assert "Create User" in page.text
        assert "Runtime Config" in page.text
        assert "Audit Log" in page.text
        assert 'id="admin-users"' in page.text
        assert 'id="admin-albums"' in page.text


def test_admin_runtime_status_reports_observability_snapshot(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("TRUSTED_PUBLIC_ORIGINS", "https://testserver")
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS_ENABLED", "true")
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "127.0.0.1/32,172.16.0.0/12")

    _, admin_key = create_admin_and_api_key(capsys, username="statusadmin", email="statusadmin@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get(
            "/api/v1/admin/runtime-status",
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["database"]["ok"] is True
        assert payload["storage"]["ok"] is True
        assert "tasks" in payload
        assert "trusted_public_origins" in payload
        assert payload["forwarded_headers_policy"] == "trusted_proxies_only"
        assert payload["trusted_proxy_cidrs_enabled"] is True
        assert payload["trusted_proxy_cidrs"] == ["127.0.0.1/32", "172.16.0.0/12"]


def test_album_tools_page_includes_manual_album_controls(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        page = client.get("/album-tools")
        assert page.status_code == 200
        assert "Album Tools" in page.text
        assert "Load Album" in page.text
        assert 'name="album_id"' in page.text
        assert 'name="delete_token"' in page.text


def test_public_user_album_list_page_shows_owned_albums_sorted_by_recent_update(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, api_key = create_user_and_api_key(capsys, username="showcase", email="showcase@example.com")

    with TestClient(app) as client:
        first = client.post(
            "/api/v1/upload",
            files=[("file", ("first.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Older Album"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert first.status_code == 200

        second = client.post(
            "/api/v1/upload",
            files=[("file", ("second.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Newer Album"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert second.status_code == 200
        wait_for_thumbnail(client, first.json()["media_id"])
        wait_for_thumbnail(client, second.json()["media_id"])

        page = client.get("/u/showcase")
        assert page.status_code == 200
        assert "Public user album list." in page.text
        assert "Older Album" in page.text
        assert "Newer Album" in page.text
        assert f'/a/{first.json()["album_id"]}' in page.text
        assert f'/a/{second.json()["album_id"]}' in page.text
        assert page.text.index("Newer Album") < page.text.index("Older Album")


def test_public_user_album_list_page_hides_expired_albums_and_404s_for_missing_user(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="hidadmin", email="hidadmin@example.com")
    _, api_key = create_user_and_api_key(capsys, username="hidden", email="hidden@example.com")

    with TestClient(app) as client:
        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("expired.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Expired Album"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert upload.status_code == 200

        expired = client.patch(
            f"/api/v1/admin/albums/{upload.json()['album_id']}",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"expires_at": (utcnow() - timedelta(hours=1)).isoformat()},
        )
        assert expired.status_code == 200

        page = client.get("/u/hidden")
        assert page.status_code == 200
        assert "Expired Album" not in page.text
        assert "This user has no public albums yet." in page.text

        missing = client.get("/u/does-not-exist")
        assert missing.status_code == 404


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


def test_authenticated_owner_and_admin_can_manage_album_without_delete_token(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, owner_key = create_user_and_api_key(capsys, username="owner", email="owner@example.com")
    _, admin_key = create_admin_and_api_key(capsys, username="albumadmin", email="albumadmin@example.com")
    _, stranger_key = create_user_and_api_key(capsys, username="stranger", email="stranger@example.com")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("one.png", BytesIO(PNG_1X1), "image/png"))],
            headers={"Authorization": f"Bearer {owner_key}"},
        )

        assert response.status_code == 200
        payload = response.json()
        album_id = payload["album_id"]
        media_ids = [item["media_id"] for item in payload["items"]]
        assert payload["delete_url"].endswith(f"/api/v1/album/{album_id}/delete")
        assert "delete_token=" not in payload["delete_url"]

        stranger_patch = client.patch(
            f"/api/v1/album/{album_id}",
            headers={"Authorization": f"Bearer {stranger_key}"},
            json={"title": "Nope"},
        )
        assert stranger_patch.status_code == 403

        owner_patch = client.patch(
            f"/api/v1/album/{album_id}",
            headers={"Authorization": f"Bearer {owner_key}"},
            json={"title": "Owned", "cover_media_id": media_ids[0]},
        )
        assert owner_patch.status_code == 200
        assert owner_patch.json()["title"] == "Owned"
        assert owner_patch.json()["cover_media_id"] == media_ids[0]

        stranger_delete = client.delete(
            f"/api/v1/media/{media_ids[0]}",
            headers={"Authorization": f"Bearer {stranger_key}"},
        )
        assert stranger_delete.status_code == 403

        admin_reorder = client.patch(
            f"/api/v1/album/{album_id}/order",
            headers={"Authorization": f"Bearer {admin_key}"},
            json=[
                {"media_id": media_ids[0], "position": 10},
            ],
        )
        assert admin_reorder.status_code == 200
        reordered = admin_reorder.json()
        assert [item["id"] for item in reordered["items"]] == [media_ids[0]]

        owner_delete = client.delete(
            f"/api/v1/media/{media_ids[0]}",
            headers={"Authorization": f"Bearer {owner_key}"},
        )
        assert owner_delete.status_code == 200
        assert owner_delete.json()["album_deleted"] is True

        album_response = client.get(f"/api/v1/album/{album_id}")
        assert album_response.status_code == 404


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
        update_media_record(
            client,
            payload["media_id"],
            media_type="video",
            format="mov",
            mime_type="video/quicktime",
            codec_hint="hevc",
            thumb_status="done",
            thumb_key=None,
            thumb_is_orig=True,
        )

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
        assert me_payload["api_key_created_at"] is not None
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


def test_sharex_config_uses_forwarded_public_origin(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://fallback.example.com")
    monkeypatch.setenv("TRUSTED_PUBLIC_ORIGINS", "https://imghost.a.example,https://imghost.b.example")

    _, api_key = create_user_and_api_key(capsys, username="sharexforward", email="sharexforward@example.com")

    with TestClient(app, base_url="http://backend") as client:
        response = client.get(
            "/api/v1/user/me/sharex-config",
            headers={
                "Authorization": f"Bearer {api_key}",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "imghost.a.example",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["RequestURL"] == "https://imghost.a.example/api/v1/upload"


def test_sharex_config_rejects_untrusted_forwarded_public_origin(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://fallback.example.com")
    monkeypatch.setenv("TRUSTED_PUBLIC_ORIGINS", "https://imghost.a.example")

    _, api_key = create_user_and_api_key(capsys, username="sharexfallback", email="sharexfallback@example.com")

    with TestClient(app, base_url="http://backend") as client:
        response = client.get(
            "/api/v1/user/me/sharex-config",
            headers={
                "Authorization": f"Bearer {api_key}",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "evil.example",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["RequestURL"] == "https://fallback.example.com/api/v1/upload"


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


def test_user_quota_rejects_authenticated_upload_when_exceeded(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, api_key = create_user_and_api_key(capsys, username="frank", email="frank@example.com")

    with TestClient(app) as client:
        user = client.portal.call(client.app.state.imghost.repository.get_user_by_username, "frank")
        assert user is not None
        user.quota_bytes = 1
        user.updated_at = utcnow()
        client.portal.call(client.app.state.imghost.repository.update_user, user)
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


def test_anon_rate_limit_blocks_after_runtime_threshold(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        state = client.app.state.imghost
        admin = client.portal.call(
            lambda: state.uploads.create_user(
                UserCreateInput(
                username="anon-rate-admin",
                email="anon-rate-admin@example.com",
                password="secret",
                is_admin=True,
                quota_bytes=None,
            ),
                method="admin",
                correlation_id="anon-rate-admin",
                source="api",
            )
        )
        issued = client.portal.call(state.uploads.issue_api_key, admin)
        configured = client.patch(
            "/api/v1/admin/config",
            headers={"Authorization": f"Bearer {issued.raw_key}"},
            json={"rate_limit_anon_rpm": 1, "rate_limit_anon_bph": 1000000, "rate_limit_global_anon_rpm": 10},
        )
        assert configured.status_code == 200

        first = client.post(
            "/api/v1/upload",
            files=[("file", ("one.png", BytesIO(PNG_1X1), "image/png"))],
            headers={"User-Agent": "Anon-Agent", "CF-Connecting-IP": "198.51.100.10"},
        )
        assert first.status_code == 200

        second = client.post(
            "/api/v1/upload",
            files=[("file", ("two.png", BytesIO(PNG_1X1), "image/png"))],
            headers={"User-Agent": "Anon-Agent", "CF-Connecting-IP": "198.51.100.10"},
        )
        assert second.status_code == 429
        assert second.json()["detail"] == "Upload rate limit exceeded."


def test_authenticated_rate_limit_uses_user_identity(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="rateadmin", email="rateadmin@example.com")
    _, api_key = create_user_and_api_key(capsys, username="ratelimited", email="ratelimited@example.com")

    with TestClient(app) as client:
        configured = client.patch(
            "/api/v1/admin/config",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"rate_limit_user_rpm": 1, "rate_limit_user_bph": 1000000},
        )
        assert configured.status_code == 200

        first = client.post(
            "/api/v1/upload",
            files=[("file", ("one.png", BytesIO(PNG_1X1), "image/png"))],
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": "Agent-A"},
        )
        assert first.status_code == 200

        second = client.post(
            "/api/v1/upload",
            files=[("file", ("two.png", BytesIO(PNG_1X1), "image/png"))],
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": "Agent-B"},
        )
        assert second.status_code == 429
        assert second.json()["detail"] == "Upload rate limit exceeded."


def test_user_rate_limit_override_takes_precedence_over_runtime_default(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="overrideadmin", email="overrideadmin@example.com")
    user_id, api_key = create_user_and_api_key(capsys, username="overrideuser", email="overrideuser@example.com")

    with TestClient(app) as client:
        configured = client.patch(
            "/api/v1/admin/config",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"rate_limit_user_rpm": 5, "rate_limit_user_bph": 1000000},
        )
        assert configured.status_code == 200

        overridden = client.patch(
            f"/api/v1/admin/users/{user_id}",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"rate_limit_rpm": 1},
        )
        assert overridden.status_code == 200
        assert overridden.json()["rate_limit_rpm"] == 1

        first = client.post(
            "/api/v1/upload",
            files=[("file", ("one.png", BytesIO(PNG_1X1), "image/png"))],
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert first.status_code == 200

        second = client.post(
            "/api/v1/upload",
            files=[("file", ("two.png", BytesIO(PNG_1X1), "image/png"))],
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert second.status_code == 429
        assert second.json()["detail"] == "Upload rate limit exceeded."


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
                "rate_limit_rpm": 12,
                "rate_limit_bph": 34567,
            },
        )
        assert created.status_code == 201
        created_user = created.json()
        assert created_user["quota_bytes"] == 12345
        assert created_user["rate_limit_rpm"] == 12
        assert created_user["rate_limit_bph"] == 34567

        refreshed_users = client.get("/api/v1/admin/users", headers={"Authorization": f"Bearer {admin_key}"})
        assert refreshed_users.status_code == 200
        refreshed_list = {item["id"]: item for item in refreshed_users.json()}
        assert refreshed_list[created_user["id"]]["rate_limit_rpm"] == 12
        assert refreshed_list[created_user["id"]]["rate_limit_bph"] == 34567

        patched = client.patch(
            f"/api/v1/admin/users/{created_user['id']}",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"suspended": True, "quota_bytes": 999, "rate_limit_rpm": 7},
        )
        assert patched.status_code == 200
        assert patched.json()["suspended"] is True
        assert patched.json()["quota_bytes"] == 999
        assert patched.json()["rate_limit_rpm"] == 7

        refreshed_users = client.get("/api/v1/admin/users", headers={"Authorization": f"Bearer {admin_key}"})
        assert refreshed_users.status_code == 200
        refreshed_list = {item["id"]: item for item in refreshed_users.json()}
        assert refreshed_list[created_user["id"]]["rate_limit_rpm"] == 7

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


def test_admin_password_reset_requires_dedicated_endpoint_and_allows_new_login(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="pwadmin", email="pwadmin@example.com")
    user_id, _ = create_user_and_api_key(capsys, username="resetme", email="resetme@example.com")

    with TestClient(app) as client:
        rejected = client.patch(
            f"/api/v1/admin/users/{user_id}",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"password": "new-admin-pass"},
        )
        assert rejected.status_code == 400
        assert "dedicated admin password reset endpoint" in rejected.json()["detail"]

        reset = client.post(
            f"/api/v1/admin/users/{user_id}/reset-password",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"new_password": "new-admin-pass"},
        )
        assert reset.status_code == 200
        assert reset.json() == {"reset": True, "user_id": user_id}

        login = client.post(
            "/api/v1/auth/login",
            json={"login": "resetme", "password": "new-admin-pass"},
        )
        assert login.status_code == 200
        assert login.json()["authenticated"] is True


def test_user_can_change_password_with_current_password(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    user_id, api_key = create_user_and_api_key(capsys, username="iris", email="iris@example.com")

    with TestClient(app) as client:
        set_user_password(client, user_id, "old-pass")
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

        updated_user = get_user_record(client, user_id)
        assert updated_user is not None
        assert updated_user.password_hash != "new-pass"
        assert bcrypt.checkpw(b"new-pass", updated_user.password_hash.encode("utf-8"))


def test_local_login_sets_session_cookie_and_authenticates_browser_flow(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, _ = create_user_and_api_key(capsys, username="kira", email="kira@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, user_id, "open-sesame")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "kira@example.com", "password": "open-sesame"},
        )
        assert login.status_code == 200
        assert "imghost_session=" in login.headers["set-cookie"]
        assert "Secure" in login.headers["set-cookie"]
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

        album = get_album_record(client, payload["album_id"])
        assert album is not None
        assert album.user_id == user_id
        assert album.expires_at is None
        assert album.delete_token is None

        logout = client.post("/api/v1/auth/logout")
        assert logout.status_code == 200
        assert logout.json()["authenticated"] is False
        assert "Secure" in logout.headers["set-cookie"]

        after_logout = client.get("/api/v1/user/me")
        assert after_logout.status_code == 401


def test_local_http_login_uses_insecure_cookie_for_dev_refreshes(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, _ = create_user_and_api_key(capsys, username="devcookie", email="devcookie@example.com")

    with TestClient(app) as client:
        set_user_password(client, user_id, "open-sesame")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "devcookie@example.com", "password": "open-sesame"},
        )
        assert login.status_code == 200
        assert "imghost_session=" in login.headers["set-cookie"]
        assert "Secure" not in login.headers["set-cookie"]

        me = client.get("/api/v1/user/me")
        assert me.status_code == 200
        assert me.json()["id"] == user_id

        page = client.get("/")
        assert page.status_code == 200
        assert "Logged in as <strong>devcookie</strong>." in page.text

        logout = client.post("/api/v1/auth/logout")
        assert logout.status_code == 200
        assert "Secure" not in logout.headers["set-cookie"]


def test_home_page_clears_stale_session_cookie_and_renders_anonymous_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("TRUSTED_PUBLIC_ORIGINS", "https://testserver")

    with TestClient(app, base_url="https://testserver") as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "stalesession",
                "email": "stalesession@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200

        state = client.app.state.imghost
        user = client.portal.call(state.repository.get_user_by_username, "stalesession")
        assert user is not None
        client.portal.call(state.repository.delete_user, user.id)

        page = client.get("/")
        assert page.status_code == 200
        assert 'id="login-form"' in page.text
        assert "Invalid session." not in page.text
        assert "imghost_session=" in page.headers["set-cookie"]


def test_local_login_supports_username_session_cookie_and_browser_sharex_download_rotates_key(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, old_api_key = create_user_and_api_key(capsys, username="lena", email="lena@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, user_id, "letmein")
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
        assert "Secure" in login.headers["set-cookie"]
        assert "Max-Age=" not in login.headers["set-cookie"]

        sharex = client.get("/api/v1/user/me/sharex-config")
        assert sharex.status_code == 200
        payload = sharex.json()
        new_auth_header = payload["Headers"]["Authorization"]
        assert new_auth_header.startswith("Bearer ")
        new_api_key = new_auth_header.removeprefix("Bearer ")
        assert new_api_key != old_api_key

        old_me = client.get("/api/v1/user/me", headers={"Authorization": f"Bearer {old_api_key}"})
        assert old_me.status_code == 401

        new_me = client.get("/api/v1/user/me", headers={"Authorization": f"Bearer {new_api_key}"})
        assert new_me.status_code == 200
        assert new_me.json()["username"] == "lena"


def test_admin_local_login_writes_admin_login_audit_event(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    admin_id, admin_key = create_admin_and_api_key(capsys, username="auditloginadmin", email="auditloginadmin@example.com")

    with TestClient(app) as client:
        set_user_password(client, admin_id, "admin-pass")
        login = client.post(
            "/api/v1/auth/login",
            headers={"X-Correlation-ID": "admin-login-flow"},
            json={"login": "auditloginadmin", "password": "admin-pass"},
        )
        assert login.status_code == 200

        audit = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "admin_login", "actor_id": admin_id, "correlation_id": "admin-login-flow"},
        )
        assert audit.status_code == 200
        payload = audit.json()
        assert len(payload) == 1
        assert payload[0]["target_type"] == "user"
        assert payload[0]["target_id"] == admin_id
        assert payload[0]["metadata"]["source"] == "web"


def test_non_admin_local_login_does_not_write_admin_login_audit_event(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    admin_id, admin_key = create_admin_and_api_key(capsys, username="watcheradmin", email="watcheradmin@example.com")
    user_id, _ = create_user_and_api_key(capsys, username="plainuser", email="plainuser@example.com")

    with TestClient(app) as client:
        set_user_password(client, user_id, "user-pass")
        login = client.post(
            "/api/v1/auth/login",
            headers={"X-Correlation-ID": "plain-login-flow"},
            json={"login": "plainuser", "password": "user-pass"},
        )
        assert login.status_code == 200

        audit = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "admin_login", "correlation_id": "plain-login-flow"},
        )
        assert audit.status_code == 200
        assert audit.json() == []


def test_registration_creates_user_session_and_audit_entry(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")

    admin_id, admin_key = create_admin_and_api_key(capsys, username="regadmin", email="regadmin@example.com")

    with TestClient(app, base_url="https://testserver") as client:
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
        assert "Secure" in registered.headers["set-cookie"]
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


def test_session_cookie_secure_can_be_overridden_for_http_deployments(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, _ = create_user_and_api_key(capsys, username="forcedsecure", email="forcedsecure@example.com")

    with TestClient(app) as client:
        set_user_password(client, user_id, "open-sesame")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "forcedsecure@example.com", "password": "open-sesame"},
        )
        assert login.status_code == 200
        assert "Secure" in login.headers["set-cookie"]


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
        password_reset = client.post(
            f"/api/v1/admin/users/{user_id}/reset-password",
            headers={"Authorization": f"Bearer {admin_key}", "X-Correlation-ID": "audit-password-reset"},
            json={"new_password": "audit-pass"},
        )
        assert password_reset.status_code == 200

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

        password_reset_events = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "user_password_reset", "actor_id": admin_id, "correlation_id": "audit-password-reset"},
        )
        assert password_reset_events.status_code == 200
        password_reset_payload = password_reset_events.json()
        assert len(password_reset_payload) == 1
        assert password_reset_payload[0]["target_type"] == "user"
        assert password_reset_payload[0]["target_id"] == user_id
        assert password_reset_payload[0]["metadata"]["target_user_id"] == user_id

        user_filtered = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"user_id": user_id},
        )
        assert user_filtered.status_code == 200
        user_filtered_payload = user_filtered.json()
        assert {item["event_type"] for item in user_filtered_payload} >= {
            "album_created",
            "media_uploaded",
            "user_suspended",
            "user_password_reset",
        }
        assert all(item["actor_id"] == user_id or item["target_id"] == user_id for item in user_filtered_payload)

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
