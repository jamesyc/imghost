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
