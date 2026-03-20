from io import BytesIO

from fastapi.testclient import TestClient

from imghost.main import app

from .helpers import PNG_1X1, create_admin_and_api_key, create_user_and_api_key


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
