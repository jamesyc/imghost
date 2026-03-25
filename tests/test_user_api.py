from io import BytesIO
from uuid import uuid4

from fastapi.testclient import TestClient

from imghost.account_delete_reauth import AccountDeleteReauthPayload, AccountDeleteReauthTokenManager
from imghost.main import app
from imghost.models import User, UserSsoLink, utcnow

from .helpers import (
    PNG_1X1,
    browser_session_headers,
    create_admin_and_api_key,
    create_user_and_api_key,
    get_album_record,
    get_media_record,
    get_user_record,
    set_user_password,
    wait_for_thumbnail,
)


def assert_paginated_envelope(payload: dict, *, limit: int, offset: int, total: int, has_more: bool) -> None:
    assert isinstance(payload["items"], list)
    assert payload["limit"] == limit
    assert payload["offset"] == offset
    assert payload["total"] == total
    assert payload["has_more"] is has_more


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
        assert payload["manage_url"] is None
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
        assert payload["total"] == 1
        assert len(payload["items"]) == 1
        assert payload["items"][0]["id"] == album_id
        assert payload["items"][0]["title"] == "Owned One"
        assert payload["items"][0]["item_count"] == 1


def test_current_user_albums_endpoint_returns_recent_first_with_preview_items(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, api_key = create_user_and_api_key(capsys, username="albumsfeed2", email="albumsfeed2@example.com")

    with TestClient(app) as client:
        first = client.post(
            "/api/v1/upload",
            files=[("file", ("older.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Older Album"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert first.status_code == 200

        second = client.post(
            "/api/v1/upload",
            files=[
                ("file", ("newer-a.png", BytesIO(PNG_1X1), "image/png")),
                ("file", ("newer-b.png", BytesIO(PNG_1X1), "image/png")),
            ],
            data={"title": "Newer Album"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert second.status_code == 200

        listed = client.get("/api/v1/user/me/albums", headers={"Authorization": f"Bearer {api_key}"})
        assert listed.status_code == 200
        payload = listed.json()
        assert [entry["title"] for entry in payload["items"]] == ["Newer Album", "Older Album"]
        assert payload["items"][0]["item_count"] == 2
        assert len(payload["items"][0]["items"]) == 2
        assert payload["items"][0]["cover_url"].endswith(f'/i/{payload["items"][0]["items"][0]["id"]}.png')
        assert payload["items"][1]["item_count"] == 1
        assert len(payload["items"][1]["items"]) == 1


def test_current_user_albums_endpoint_supports_pagination(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, api_key = create_user_and_api_key(capsys, username="albumspage", email="albumspage@example.com")

    with TestClient(app) as client:
        for index in range(12):
            created = client.post(
                "/api/v1/upload",
                files=[("file", (f"{index}.png", BytesIO(PNG_1X1), "image/png"))],
                data={"title": f"Album {index}"},
                headers={"Authorization": f"Bearer {api_key}"},
            )
            assert created.status_code == 200

        first_page = client.get(
            "/api/v1/user/me/albums",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"limit": 10, "offset": 0},
        )
        assert first_page.status_code == 200
        first_payload = first_page.json()
        assert_paginated_envelope(first_payload, limit=10, offset=0, total=12, has_more=True)
        assert len(first_payload["items"]) == 10

        second_page = client.get(
            "/api/v1/user/me/albums",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"limit": 10, "offset": 10},
        )
        assert second_page.status_code == 200
        second_payload = second_page.json()
        assert_paginated_envelope(second_payload, limit=10, offset=10, total=12, has_more=False)
        assert len(second_payload["items"]) == 2


def test_current_user_albums_endpoint_uses_default_envelope_and_exact_page_boundary(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, api_key = create_user_and_api_key(capsys, username="albumsdefaults", email="albumsdefaults@example.com")

    with TestClient(app) as client:
        for index in range(10):
            created = client.post(
                "/api/v1/upload",
                files=[("file", (f"{index}.png", BytesIO(PNG_1X1), "image/png"))],
                data={"title": f"Album {index}"},
                headers={"Authorization": f"Bearer {api_key}"},
            )
            assert created.status_code == 200

        listed = client.get("/api/v1/user/me/albums", headers={"Authorization": f"Bearer {api_key}"})
        assert listed.status_code == 200
        payload = listed.json()
        assert_paginated_envelope(payload, limit=10, offset=0, total=10, has_more=False)
        assert len(payload["items"]) == 10


def test_current_user_albums_endpoint_returns_empty_page_beyond_total(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, api_key = create_user_and_api_key(capsys, username="albumsempty", email="albumsempty@example.com")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/upload",
            files=[("file", ("one.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Only Album"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert created.status_code == 200

        listed = client.get(
            "/api/v1/user/me/albums",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"limit": 10, "offset": 10},
        )
        assert listed.status_code == 200
        payload = listed.json()
        assert_paginated_envelope(payload, limit=10, offset=10, total=1, has_more=False)
        assert payload["items"] == []


def test_current_user_albums_endpoint_matches_browser_session_and_api_key_contract(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, api_key = create_user_and_api_key(capsys, username="albumsdual", email="albumsdual@example.com")

    with TestClient(app) as client:
        set_user_password(client, user_id, "open-sesame")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "albumsdual@example.com", "password": "open-sesame"},
        )
        assert login.status_code == 200

        for title in ("First Session Album", "Second Session Album"):
            created = client.post(
                "/api/v1/upload",
                files=[("file", (f"{title}.png", BytesIO(PNG_1X1), "image/png"))],
                data={"title": title},
                headers={"Authorization": f"Bearer {api_key}"},
            )
            assert created.status_code == 200

        api_key_response = client.get(
            "/api/v1/user/me/albums",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"limit": 1, "offset": 0},
        )
        assert api_key_response.status_code == 200
        api_key_payload = api_key_response.json()
        assert_paginated_envelope(api_key_payload, limit=1, offset=0, total=2, has_more=True)

        session_response = client.get("/api/v1/user/me/albums", params={"limit": 1, "offset": 0})
        assert session_response.status_code == 200
        session_payload = session_response.json()
        assert_paginated_envelope(session_payload, limit=1, offset=0, total=2, has_more=True)

        assert session_payload["items"][0]["id"] == api_key_payload["items"][0]["id"]
        assert session_payload["items"][0]["title"] == api_key_payload["items"][0]["title"]
        assert session_payload["items"][0]["item_count"] == api_key_payload["items"][0]["item_count"]


def test_current_user_albums_endpoint_preserves_item_order_after_reorder(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, api_key = create_user_and_api_key(capsys, username="albumsorder", email="albumsorder@example.com")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/upload",
            files=[
                ("file", ("one.png", BytesIO(PNG_1X1), "image/png")),
                ("file", ("two.png", BytesIO(PNG_1X1), "image/png")),
                ("file", ("three.png", BytesIO(PNG_1X1), "image/png")),
            ],
            data={"title": "Ordered Album"},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert created.status_code == 200
        payload = created.json()
        album_id = payload["album_id"]
        media_ids = [item["media_id"] for item in payload["items"]]

        reordered = client.patch(
            f"/api/v1/album/{album_id}/order",
            headers={"Authorization": f"Bearer {api_key}"},
            json=[
                {"media_id": media_ids[2], "position": 100},
                {"media_id": media_ids[0], "position": 200},
                {"media_id": media_ids[1], "position": 300},
            ],
        )
        assert reordered.status_code == 200

        listed = client.get("/api/v1/user/me/albums", headers={"Authorization": f"Bearer {api_key}"})
        assert listed.status_code == 200
        album_payload = listed.json()["items"][0]
        assert album_payload["id"] == album_id
        assert [item["id"] for item in album_payload["items"]] == [media_ids[2], media_ids[0], media_ids[1]]
        assert [item["position"] for item in album_payload["items"]] == [100, 200, 300]
        assert album_payload["cover_url"].endswith(f"/i/{media_ids[2]}.png")


def test_current_user_albums_endpoint_rejects_invalid_pagination_values(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, api_key = create_user_and_api_key(capsys, username="albumspagination", email="albumspagination@example.com")

    with TestClient(app) as client:
        zero_limit = client.get(
            "/api/v1/user/me/albums",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"limit": 0},
        )
        assert zero_limit.status_code == 400
        assert zero_limit.json()["detail"] == "limit must be between 1 and 200."

        large_limit = client.get(
            "/api/v1/user/me/albums",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"limit": 201},
        )
        assert large_limit.status_code == 400
        assert large_limit.json()["detail"] == "limit must be between 1 and 200."

        negative_limit = client.get(
            "/api/v1/user/me/albums",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"limit": -1},
        )
        assert negative_limit.status_code == 400
        assert negative_limit.json()["detail"] == "limit must be between 1 and 200."

        negative_offset = client.get(
            "/api/v1/user/me/albums",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"offset": -1},
        )
        assert negative_offset.status_code == 400
        assert negative_offset.json()["detail"] == "offset must be non-negative."


def test_current_user_albums_endpoint_rejects_non_integer_pagination_values(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, api_key = create_user_and_api_key(capsys, username="albumsbadtypes", email="albumsbadtypes@example.com")

    with TestClient(app) as client:
        bad_limit = client.get(
            "/api/v1/user/me/albums",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"limit": "abc"},
        )
        assert bad_limit.status_code == 422
        assert any(item["loc"][-1] == "limit" for item in bad_limit.json()["detail"])

        bad_offset = client.get(
            "/api/v1/user/me/albums",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"offset": "abc"},
        )
        assert bad_offset.status_code == 422
        assert any(item["loc"][-1] == "offset" for item in bad_offset.json()["detail"])


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
        assert payload["manage_url"] is None

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


def test_api_key_can_rotate_and_delete_album_via_delete(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="userauditwatcher", email="userauditwatcher@example.com")
    user_id, api_key = create_user_and_api_key(capsys, username="carol", email="carol@example.com")

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

        delete = client.delete(
            f"/api/v1/album/{payload['album_id']}",
            headers={"Authorization": f"Bearer {new_api_key}"},
        )
        assert delete.status_code == 200
        assert delete.json()["deleted"] is True

        audit = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "api_key_issued"},
        )
        assert audit.status_code == 200
        issued_events = [item for item in audit.json() if item["target_id"] == user_id]
        assert issued_events
        latest = issued_events[0]
        assert latest["actor_id"] == user_id
        assert latest["metadata"]["replaced_existing"] is True


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

    _, admin_key = create_admin_and_api_key(capsys, username="sharexauditadmin", email="sharexauditadmin@example.com")

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
        user_id = me.json()["id"]

        audit = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "api_key_issued"},
        )
        assert audit.status_code == 200
        sharex_events = [item for item in audit.json() if item["target_id"] == user_id]
        assert sharex_events
        assert sharex_events[0]["metadata"]["replaced_existing"] is False
        assert sharex_events[0]["metadata"]["source"] == "sharex"


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
        set_user_password(client, user_id, "open-sesame")
        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert upload.status_code == 200
        payload = upload.json()

        delete = client.request(
            "DELETE",
            "/api/v1/user/me",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"method": "password", "current_password": "open-sesame"},
        )
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


def test_browser_session_api_key_rotation_invalidates_previous_key(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, old_api_key = create_user_and_api_key(capsys, username="rotatebrowser", email="rotatebrowser@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, user_id, "open-sesame")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "rotatebrowser@example.com", "password": "open-sesame"},
        )
        assert login.status_code == 200

        rotated = client.post("/api/v1/user/me/api-key", headers=browser_session_headers("https://testserver", "/settings"))
        assert rotated.status_code == 200
        payload = rotated.json()
        assert payload["api_key"] != old_api_key
        assert payload["created_at"]

        old_me = client.get("/api/v1/user/me", headers={"Authorization": f"Bearer {old_api_key}"})
        assert old_me.status_code == 401

        new_me = client.get("/api/v1/user/me", headers={"Authorization": f"Bearer {payload['api_key']}"})
        assert new_me.status_code == 200
        assert new_me.json()["username"] == "rotatebrowser"


def test_browser_session_password_change_requires_old_password_and_enables_new_login(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    _, admin_key = create_admin_and_api_key(capsys, username="passwordauditadmin", email="passwordauditadmin@example.com")
    user_id, _ = create_user_and_api_key(capsys, username="passwordbrowser", email="passwordbrowser@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, user_id, "old-pass")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "passwordbrowser@example.com", "password": "old-pass"},
        )
        assert login.status_code == 200

        bad = client.patch(
            "/api/v1/user/me/password",
            json={"current_password": "wrong-pass", "new_password": "new-pass"},
            headers=browser_session_headers("https://testserver", "/settings"),
        )
        assert bad.status_code == 403

        good = client.patch(
            "/api/v1/user/me/password",
            json={"current_password": "old-pass", "new_password": "new-pass"},
            headers=browser_session_headers("https://testserver", "/settings"),
        )
        assert good.status_code == 200
        assert good.json()["updated"] is True

        audit = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "user_password_changed"},
        )
        assert audit.status_code == 200
        changed_events = [item for item in audit.json() if item["target_id"] == user_id]
        assert changed_events
        assert changed_events[0]["actor_id"] == user_id

        client.post("/api/v1/auth/logout", headers=browser_session_headers("https://testserver", "/"))

        old_login = client.post(
            "/api/v1/auth/login",
            json={"login": "passwordbrowser@example.com", "password": "old-pass"},
        )
        assert old_login.status_code == 401

        new_login = client.post(
            "/api/v1/auth/login",
            json={"login": "passwordbrowser@example.com", "password": "new-pass"},
        )
        assert new_login.status_code == 200


def test_browser_session_password_change_rejects_short_new_password(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, _ = create_user_and_api_key(capsys, username="shortpasswordbrowser", email="shortpasswordbrowser@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, user_id, "old-pass")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "shortpasswordbrowser@example.com", "password": "old-pass"},
        )
        assert login.status_code == 200

        rejected = client.patch(
            "/api/v1/user/me/password",
            json={"current_password": "old-pass", "new_password": "short7!"},
            headers=browser_session_headers("https://testserver", "/settings"),
        )
        assert rejected.status_code == 400
        assert rejected.json()["detail"] == "New password must be at least 8 characters."


def test_browser_session_delete_current_user_clears_session_and_removes_content(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, _ = create_user_and_api_key(capsys, username="deletebrowser", email="deletebrowser@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, user_id, "open-sesame")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "deletebrowser@example.com", "password": "open-sesame"},
        )
        assert login.status_code == 200

        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            headers=browser_session_headers("https://testserver", "/dashboard"),
        )
        assert upload.status_code == 200
        payload = upload.json()

        deleted = client.request(
            "DELETE",
            "/api/v1/user/me",
            headers=browser_session_headers("https://testserver", "/settings"),
            json={"method": "password", "current_password": "open-sesame"},
        )
        assert deleted.status_code == 200
        deleted_payload = deleted.json()
        assert deleted_payload["deleted"] is True
        assert deleted_payload["user_id"] == user_id
        assert "imghost_session=" in deleted.headers["set-cookie"]

        me = client.get("/api/v1/user/me")
        assert me.status_code == 401

        assert get_user_record(client, user_id) is None
        assert get_album_record(client, payload["album_id"]) is None
        assert get_media_record(client, payload["media_id"]) is None


def test_delete_current_user_requires_explicit_confirmation(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    user_id, api_key = create_user_and_api_key(capsys, username="deleteconfirm", email="deleteconfirm@example.com")

    with TestClient(app) as client:
        set_user_password(client, user_id, "open-sesame")

        missing = client.request(
            "DELETE",
            "/api/v1/user/me",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"method": "password", "current_password": ""},
        )
        assert missing.status_code == 400
        assert missing.json()["detail"] == "Current password is required."

        wrong = client.request(
            "DELETE",
            "/api/v1/user/me",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"method": "password", "current_password": "wrong-pass"},
        )
        assert wrong.status_code == 403
        assert wrong.json()["detail"] == "Current password is incorrect."


def test_delete_current_user_rejects_password_confirmation_when_account_has_no_password(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        now = utcnow()
        user = User(
            id=str(uuid4()),
            username="nopassworddelete",
            email="nopassworddelete@example.com",
            password_hash=None,
            is_admin=False,
            suspended=False,
            quota_bytes=None,
            rate_limit_rpm=None,
            rate_limit_bph=None,
            created_at=now,
            updated_at=now,
        )
        client.portal.call(client.app.state.imghost.repository.create_user, user)
        issued = client.portal.call(client.app.state.imghost.uploads.issue_api_key, user)
        api_key = issued.raw_key

        response = client.request(
            "DELETE",
            "/api/v1/user/me",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"method": "password", "current_password": "irrelevant"},
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "This account does not have a local password."


def test_delete_current_user_rejects_missing_oauth_reauth_token(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, api_key = create_user_and_api_key(capsys, username="missingreauth", email="missingreauth@example.com")

    with TestClient(app) as client:
        response = client.request(
            "DELETE",
            "/api/v1/user/me",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"method": "oauth_reauth", "reauth_token": ""},
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "OAuth re-authentication is required."


def test_delete_current_user_rejects_invalid_oauth_reauth_token(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("SECRET_KEY", "delete-reauth-secret")

    _, api_key = create_user_and_api_key(capsys, username="invalidreauth", email="invalidreauth@example.com")

    with TestClient(app) as client:
        response = client.request(
            "DELETE",
            "/api/v1/user/me",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"method": "oauth_reauth", "reauth_token": "not-a-real-token"},
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "OAuth re-authentication has expired or is invalid."


def test_delete_current_user_rejects_oauth_reauth_token_for_different_user(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("SECRET_KEY", "delete-reauth-secret")

    user_id, api_key = create_user_and_api_key(capsys, username="tokenowner", email="tokenowner@example.com")
    other_user_id, _ = create_user_and_api_key(capsys, username="othertokenowner", email="othertokenowner@example.com")

    with TestClient(app) as client:
        client.portal.call(
            client.app.state.imghost.repository.create_user_sso_link,
            UserSsoLink(
                id="11111111-1111-1111-1111-111111111111",
                user_id=other_user_id,
                provider="github",
                provider_uid="gh-user-1",
                linked_at=utcnow(),
            ),
        )
        token = AccountDeleteReauthTokenManager(client.app.state.imghost.settings.secret_key).dumps(
            AccountDeleteReauthPayload(user_id=other_user_id, provider="github", provider_uid="gh-user-1")
        )
        response = client.request(
            "DELETE",
            "/api/v1/user/me",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"method": "oauth_reauth", "reauth_token": token},
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "OAuth re-authentication has expired or is invalid."


def test_delete_current_user_rejects_oauth_reauth_token_when_link_no_longer_exists(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("SECRET_KEY", "delete-reauth-secret")

    user_id, api_key = create_user_and_api_key(capsys, username="staleprovider", email="staleprovider@example.com")

    with TestClient(app) as client:
        client.portal.call(
            client.app.state.imghost.repository.create_user_sso_link,
            UserSsoLink(
                id="22222222-2222-2222-2222-222222222222",
                user_id=user_id,
                provider="github",
                provider_uid="gh-user-2",
                linked_at=utcnow(),
            ),
        )
        token = AccountDeleteReauthTokenManager(client.app.state.imghost.settings.secret_key).dumps(
            AccountDeleteReauthPayload(user_id=user_id, provider="github", provider_uid="gh-user-2")
        )
        client.portal.call(client.app.state.imghost.repository.delete_user_sso_link, user_id, "github")
        response = client.request(
            "DELETE",
            "/api/v1/user/me",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"method": "oauth_reauth", "reauth_token": token},
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "OAuth re-authentication has expired or is invalid."
