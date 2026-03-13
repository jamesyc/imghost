from io import BytesIO
from zipfile import ZipFile

from fastapi.testclient import TestClient

from imghost.main import app


def test_upload_album_and_media_serving(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(b"fake-image-bytes"), "image/png"))],
            data={"title": "V1 Album"},
        )

        assert response.status_code == 200
        payload = response.json()
        album_id = payload["album_id"]
        media_id = payload["items"][0]["media_id"]
        delete_url = payload["delete_url"]

        album_response = client.get(f"/api/v1/album/{album_id}")
        assert album_response.status_code == 200
        assert album_response.json()["title"] == "V1 Album"
        assert album_response.json()["item_count"] == 1

        media_response = client.get(f"/i/{media_id}.png")
        assert media_response.status_code == 200
        assert media_response.content == b"fake-image-bytes"

        range_response = client.get(f"/i/{media_id}.png", headers={"Range": "bytes=0-3"})
        assert range_response.status_code == 206
        assert range_response.headers["content-range"] == f"bytes 0-3/{len(b'fake-image-bytes')}"
        assert range_response.content == b"fake"

        zip_response = client.get(f"/api/v1/album/{album_id}/zip")
        assert zip_response.status_code == 200
        with ZipFile(BytesIO(zip_response.content)) as archive:
            assert archive.namelist() == ["sample.png"]
            assert archive.read("sample.png") == b"fake-image-bytes"

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
                ("file", ("one.png", BytesIO(b"one"), "image/png")),
                ("file", ("two.png", BytesIO(b"two"), "image/png")),
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
                ("file", ("one.png", BytesIO(b"one"), "image/png")),
                ("file", ("two.png", BytesIO(b"two"), "image/png")),
                ("file", ("three.png", BytesIO(b"three"), "image/png")),
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
            files=[("file", ("solo.png", BytesIO(b"solo"), "image/png"))],
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
