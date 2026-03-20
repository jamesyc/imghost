from datetime import datetime, timedelta
from io import BytesIO

from fastapi.testclient import TestClient

from imghost.main import app
from imghost.models import utcnow
from imghost.service import UserCreateInput

from .helpers import PNG_1X1, create_admin_and_api_key, create_user_and_api_key


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
