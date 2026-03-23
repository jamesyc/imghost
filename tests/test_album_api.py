from io import BytesIO
from datetime import timedelta

from fastapi.testclient import TestClient

from imghost.main import app
from imghost.models import utcnow

from .helpers import PNG_1X1, browser_session_headers, create_admin_and_api_key, create_user_and_api_key, update_album_record


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
        delete_token = payload["manage_url"].split("token=")[1]
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
        assert payload["manage_url"] is None

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
            json=[{"media_id": media_ids[0], "position": 10}],
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


def test_suspended_owner_cannot_mutate_owned_album_with_api_key(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    owner_id, owner_key = create_user_and_api_key(capsys, username="suspendedowner", email="suspendedowner@example.com")
    _, admin_key = create_admin_and_api_key(capsys, username="suspendadmin", email="suspendadmin@example.com")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/upload",
            files=[("file", ("owned.png", BytesIO(PNG_1X1), "image/png"))],
            headers={"Authorization": f"Bearer {owner_key}"},
        )
        assert created.status_code == 200
        album_id = created.json()["album_id"]

        suspended = client.patch(
            f"/api/v1/admin/users/{owner_id}",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"suspended": True},
        )
        assert suspended.status_code == 200

        patch_response = client.patch(
            f"/api/v1/album/{album_id}",
            headers={"Authorization": f"Bearer {owner_key}"},
            json={"title": "No longer allowed"},
        )
        assert patch_response.status_code == 403
        assert patch_response.json()["detail"] == "User is not allowed to authenticate."


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
        delete_token = payload["manage_url"].split("token=")[1]

        delete_response = client.delete(
            f"/api/v1/media/{media_id}",
            params={"delete_token": delete_token},
        )
        assert delete_response.status_code == 200
        assert delete_response.json()["album_deleted"] is True

        assert client.get(f"/api/v1/album/{album_id}").status_code == 404


def test_expired_anonymous_album_mutations_are_denied_even_with_valid_delete_token(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/upload",
            files=[("file", ("expired.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Expires Soon"},
        )
        assert created.status_code == 200
        payload = created.json()
        album_id = payload["album_id"]
        delete_token = payload["manage_url"].split("token=")[1]
        media_id = payload["media_id"]

        update_album_record(client, album_id, expires_at=utcnow() - timedelta(minutes=1))

        patch_response = client.patch(
            f"/api/v1/album/{album_id}",
            params={"delete_token": delete_token},
            json={"title": "Should Not Work"},
        )
        assert patch_response.status_code == 404

        reorder_response = client.patch(
            f"/api/v1/album/{album_id}/order",
            params={"delete_token": delete_token},
            json=[{"media_id": media_id, "position": 10}],
        )
        assert reorder_response.status_code == 404

        append_response = client.post(
            "/api/v1/upload",
            files=[("file", ("append.png", BytesIO(PNG_1X1), "image/png"))],
            data={"album_id": album_id, "delete_token": delete_token},
        )
        assert append_response.status_code == 404

        media_delete_response = client.delete(
            f"/api/v1/media/{media_id}",
            params={"delete_token": delete_token},
        )
        assert media_delete_response.status_code == 404

        delete_response = client.delete(
            f"/api/v1/album/{album_id}",
            params={"delete_token": delete_token},
        )
        assert delete_response.status_code == 404


def test_admin_can_delete_expired_anonymous_album_for_cleanup(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="expiredcleanupadmin", email="expiredcleanupadmin@example.com")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/upload",
            files=[("file", ("expired.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Expired Cleanup"},
        )
        assert created.status_code == 200
        payload = created.json()
        album_id = payload["album_id"]

        update_album_record(client, album_id, expires_at=utcnow() - timedelta(minutes=1))

        deleted = client.delete(
            f"/api/v1/album/{album_id}",
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        assert deleted.status_code == 200
        assert deleted.json()["album_id"] == album_id
        assert client.get(f"/api/v1/album/{album_id}").status_code == 404


def test_anonymous_token_mutations_are_audited_with_delete_token_actor_kind(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="tokenauditadmin", email="tokenauditadmin@example.com")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/upload",
            files=[
                ("file", ("one.png", BytesIO(PNG_1X1), "image/png")),
                ("file", ("two.png", BytesIO(PNG_1X1), "image/png")),
            ],
            data={"title": "Token Audit Album"},
            headers={"X-Correlation-ID": "anon-upload-audit"},
        )
        assert created.status_code == 200
        payload = created.json()
        album_id = payload["album_id"]
        delete_token = payload["manage_url"].split("token=")[1]
        media_id = payload["items"][0]["media_id"]

        media_delete = client.delete(
            f"/api/v1/media/{media_id}",
            params={"delete_token": delete_token},
            headers={"X-Correlation-ID": "anon-media-delete"},
        )
        assert media_delete.status_code == 200

        album_delete = client.delete(
            f"/api/v1/album/{album_id}",
            params={"delete_token": delete_token},
            headers={"X-Correlation-ID": "anon-album-delete"},
        )
        assert album_delete.status_code == 200

        media_events = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "media_deleted", "correlation_id": "anon-media-delete"},
        )
        assert media_events.status_code == 200
        media_payload = media_events.json()
        assert len(media_payload) == 1
        assert media_payload[0]["actor_id"] is None
        assert media_payload[0]["metadata"]["actor_kind"] == "delete_token"

        album_events = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "album_deleted", "correlation_id": "anon-album-delete"},
        )
        assert album_events.status_code == 200
        album_payload = album_events.json()
        assert len(album_payload) == 1
        assert album_payload[0]["actor_id"] is None
        assert album_payload[0]["metadata"]["actor_kind"] == "delete_token"


def test_anonymous_manage_token_can_append_without_csrf_headers_when_no_session_exists(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    with TestClient(app, base_url="https://testserver") as client:
        created = client.post(
            "/api/v1/upload",
            files=[("file", ("one.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Anonymous Workspace"},
        )
        assert created.status_code == 200
        payload = created.json()
        album_id = payload["album_id"]
        delete_token = payload["manage_url"].split("token=")[1]

        appended = client.post(
            "/api/v1/upload",
            files=[("file", ("two.png", BytesIO(PNG_1X1), "image/png"))],
            data={"album_id": album_id, "delete_token": delete_token},
        )
        assert appended.status_code == 200
        assert appended.json()["album_id"] == album_id

        album = client.get(f"/api/v1/album/{album_id}")
        assert album.status_code == 200
        assert album.json()["item_count"] == 2


def test_delete_token_cannot_mutate_a_different_album(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        first = client.post(
            "/api/v1/upload",
            files=[("file", ("first.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "First"},
        )
        second = client.post(
            "/api/v1/upload",
            files=[("file", ("second.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Second"},
        )
        assert first.status_code == 200
        assert second.status_code == 200

        first_payload = first.json()
        second_payload = second.json()
        wrong_token = first_payload["manage_url"].split("token=")[1]
        second_album_id = second_payload["album_id"]
        second_media_id = second_payload["media_id"]

        patch = client.patch(
            f"/api/v1/album/{second_album_id}",
            params={"delete_token": wrong_token},
            json={"title": "Should Fail"},
        )
        assert patch.status_code == 403

        delete_media = client.delete(
            f"/api/v1/media/{second_media_id}",
            params={"delete_token": wrong_token},
        )
        assert delete_media.status_code == 403

        untouched = client.get(f"/api/v1/album/{second_album_id}")
        assert untouched.status_code == 200
        assert untouched.json()["title"] == "Second"


def test_manage_token_with_browser_session_but_no_same_origin_headers_is_blocked(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    with TestClient(app, base_url="https://testserver") as client:
        created = client.post(
            "/api/v1/upload",
            files=[("file", ("one.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Anonymous Workspace"},
        )
        assert created.status_code == 200
        payload = created.json()
        delete_token = payload["manage_url"].split("token=")[1]

        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "tokenbrowser",
                "email": "tokenbrowser@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200

        blocked = client.patch(
            f"/api/v1/album/{payload['album_id']}",
            params={"delete_token": delete_token},
            json={"title": "Blocked"},
        )
        assert blocked.status_code == 403
        assert blocked.json()["detail"] == "CSRF protection blocked the request."


def test_manage_token_with_browser_session_and_same_origin_headers_still_works(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    with TestClient(app, base_url="https://testserver") as client:
        created = client.post(
            "/api/v1/upload",
            files=[
                ("file", ("one.png", BytesIO(PNG_1X1), "image/png")),
                ("file", ("two.png", BytesIO(PNG_1X1), "image/png")),
            ],
            data={"title": "Anonymous Workspace"},
        )
        assert created.status_code == 200
        payload = created.json()
        album_id = payload["album_id"]
        delete_token = payload["manage_url"].split("token=")[1]
        media_ids = [item["media_id"] for item in payload["items"]]

        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "tokenbrowserok",
                "email": "tokenbrowserok@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200

        patched = client.patch(
            f"/api/v1/album/{album_id}",
            params={"delete_token": delete_token},
            json={"title": "Managed In Browser"},
            headers=browser_session_headers("https://testserver", f"/manage/{album_id}"),
        )
        assert patched.status_code == 200
        assert patched.json()["title"] == "Managed In Browser"

        reordered = client.patch(
            f"/api/v1/album/{album_id}/order",
            params={"delete_token": delete_token},
            json=[
                {"media_id": media_ids[1], "position": 50},
                {"media_id": media_ids[0], "position": 60},
            ],
            headers=browser_session_headers("https://testserver", f"/manage/{album_id}"),
        )
        assert reordered.status_code == 200
        assert [item["id"] for item in reordered.json()["items"]] == [media_ids[1], media_ids[0]]


def test_browser_session_owner_can_append_reorder_and_delete_owned_album(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    with TestClient(app, base_url="https://testserver") as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "browserowner",
                "email": "browserowner@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200

        created = client.post(
            "/api/v1/upload",
            files=[
                ("file", ("one.png", BytesIO(PNG_1X1), "image/png")),
                ("file", ("two.png", BytesIO(PNG_1X1), "image/png")),
            ],
            data={"title": "Workspace Album"},
            headers=browser_session_headers("https://testserver", "/dashboard"),
        )
        assert created.status_code == 200
        created_payload = created.json()
        album_id = created_payload["album_id"]
        media_ids = [item["media_id"] for item in created_payload["items"]]

        appended = client.post(
            "/api/v1/upload",
            files=[("file", ("three.png", BytesIO(PNG_1X1), "image/png"))],
            data={"album_id": album_id},
            headers=browser_session_headers("https://testserver", f"/albums/{album_id}"),
        )
        assert appended.status_code == 200
        appended_payload = appended.json()
        assert appended_payload["album_id"] == album_id

        patched = client.patch(
            f"/api/v1/album/{album_id}",
            json={"title": "Workspace Album Edited", "cover_media_id": appended_payload["media_id"]},
            headers=browser_session_headers("https://testserver", f"/albums/{album_id}"),
        )
        assert patched.status_code == 200
        assert patched.json()["title"] == "Workspace Album Edited"
        assert patched.json()["cover_media_id"] == appended_payload["media_id"]

        reordered = client.patch(
            f"/api/v1/album/{album_id}/order",
            json=[
                {"media_id": appended_payload["media_id"], "position": 100},
                {"media_id": media_ids[0], "position": 200},
                {"media_id": media_ids[1], "position": 300},
            ],
            headers=browser_session_headers("https://testserver", f"/albums/{album_id}"),
        )
        assert reordered.status_code == 200
        assert [item["id"] for item in reordered.json()["items"]] == [appended_payload["media_id"], media_ids[0], media_ids[1]]

        listed = client.get("/api/v1/user/me/albums")
        assert listed.status_code == 200
        listed_payload = listed.json()
        assert listed_payload["total"] == 1
        assert listed_payload["limit"] == 10
        assert listed_payload["offset"] == 0
        assert listed_payload["has_more"] is False
        assert len(listed_payload["items"]) == 1
        assert listed_payload["items"][0]["id"] == album_id
        assert listed_payload["items"][0]["item_count"] == 3

        deleted = client.delete(
            f"/api/v1/album/{album_id}",
            headers=browser_session_headers("https://testserver", f"/albums/{album_id}"),
        )
        assert deleted.status_code == 200
        assert deleted.json()["album_id"] == album_id

        assert client.get(f"/api/v1/album/{album_id}").status_code == 404
        assert client.get("/api/v1/user/me/albums").json()["items"] == []
