from io import BytesIO
from uuid import uuid4

from fastapi.testclient import TestClient

from imghost.main import app
from imghost.models import utcnow

from .helpers import (
    PNG_1X1,
    browser_session_headers,
    create_admin_and_api_key,
    create_user_and_api_key,
    set_user_password,
)


def assert_paginated_envelope(payload: dict, *, limit: int, offset: int, total: int, has_more: bool) -> None:
    assert isinstance(payload["items"], list)
    assert payload["limit"] == limit
    assert payload["offset"] == offset
    assert payload["total"] == total
    assert payload["has_more"] is has_more


def test_admin_runtime_status_reports_observability_snapshot(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("TRUSTED_PUBLIC_ORIGINS", "https://testserver")
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS_ENABLED", "true")
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "127.0.0.1/32,172.16.0.0/12")
    monkeypatch.setenv("SESSION_REDIS_FAIL_CLOSED", "true")

    _, admin_key = create_admin_and_api_key(capsys, username="statusadmin", email="statusadmin@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get(
            "/api/v1/admin/runtime-status",
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["process_role"] == "app"
        assert payload["database"]["ok"] is True
        assert payload["storage"]["ok"] is True
        assert "tasks" in payload
        assert payload["services"]["app"]["enabled_in_this_process"] is True
        assert payload["services"]["worker"]["enabled_in_this_process"] is False
        assert payload["services"]["worker"]["queues"] == []
        assert payload["services"]["scheduler"]["enabled_in_this_process"] is False
        assert payload["services"]["scheduler"]["poll_seconds"] == 30
        assert payload["services"]["scheduler"]["jobs"]["prune_expired_albums"]["queue"] == "cleanup"
        assert payload["public_origin_enabled"] is True
        assert payload["public_origin_mode"] == "strict"
        assert "trusted_public_origins" in payload
        assert payload["forwarded_headers_policy"] == "trusted_proxies_only"
        assert payload["proxy_trust_warning"] is None
        assert payload["trusted_proxy_cidrs_enabled"] is True
        assert payload["trusted_proxy_cidrs"] == ["127.0.0.1/32", "172.16.0.0/12"]
        assert payload["redis"]["session_fail_closed"] is True
        assert "subsystems" in payload["redis"]
        assert "services" in payload
        assert "tasks" in payload
        assert payload["bootstrap_admin"] == {
            "enabled": False,
            "configured_username": None,
            "matched": False,
            "already_admin": False,
            "promoted": False,
            "user_id": None,
            "warning": None,
        }


def test_admin_runtime_status_reports_worker_service_view_for_worker_role(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("TASK_WORKER_ENABLED", "true")
    monkeypatch.setenv("TASK_WORKER_QUEUES", "thumbnails,cleanup")

    _, admin_key = create_admin_and_api_key(capsys, username="statusworkeradmin", email="statusworkeradmin@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get(
            "/api/v1/admin/runtime-status",
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["process_role"] == "app"
        assert payload["services"]["worker"]["enabled_in_this_process"] is False
        assert payload["services"]["worker"]["queues"] == []


def test_admin_runtime_status_reports_bootstrap_admin_promotion_state(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="statusbootstrapadmin", email="statusbootstrapadmin@example.com")
    _, user_key = create_user_and_api_key(capsys, username="statusbootstrapuser", email="statusbootstrapuser@example.com")

    monkeypatch.setenv("PROMOTE_USERNAME_TO_ADMIN", "statusbootstrapuser")

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get(
            "/api/v1/admin/runtime-status",
            headers={"Authorization": f"Bearer {user_key}"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["bootstrap_admin"]["enabled"] is True
        assert payload["bootstrap_admin"]["configured_username"] == "statusbootstrapuser"
        assert payload["bootstrap_admin"]["matched"] is True
        assert payload["bootstrap_admin"]["already_admin"] is False
        assert payload["bootstrap_admin"]["promoted"] is True
        assert payload["bootstrap_admin"]["user_id"] is not None
        assert payload["bootstrap_admin"]["warning"] is None


def test_admin_runtime_status_reports_bootstrap_admin_already_admin_state(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("PROMOTE_USERNAME_TO_ADMIN", "statusalreadyadmin")

    _, admin_key = create_admin_and_api_key(capsys, username="statusalreadyadmin", email="statusalreadyadmin@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get(
            "/api/v1/admin/runtime-status",
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["bootstrap_admin"]["enabled"] is True
        assert payload["bootstrap_admin"]["configured_username"] == "statusalreadyadmin"
        assert payload["bootstrap_admin"]["matched"] is True
        assert payload["bootstrap_admin"]["already_admin"] is True
        assert payload["bootstrap_admin"]["promoted"] is False
        assert payload["bootstrap_admin"]["user_id"] is not None
        assert payload["bootstrap_admin"]["warning"] is None


def test_admin_runtime_status_reports_missing_bootstrap_admin_username(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("PROMOTE_USERNAME_TO_ADMIN", "missing-bootstrap-user")

    _, admin_key = create_admin_and_api_key(capsys, username="statusmissingadmin", email="statusmissingadmin@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get(
            "/api/v1/admin/runtime-status",
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["bootstrap_admin"]["enabled"] is True
        assert payload["bootstrap_admin"]["configured_username"] == "missing-bootstrap-user"
        assert payload["bootstrap_admin"]["matched"] is False
        assert payload["bootstrap_admin"]["already_admin"] is False
        assert payload["bootstrap_admin"]["promoted"] is False
        assert payload["bootstrap_admin"]["user_id"] is None
        assert payload["bootstrap_admin"]["warning"] == (
            "PROMOTE_USERNAME_TO_ADMIN set to 'missing-bootstrap-user', but no matching user exists."
        )


def test_admin_runtime_status_warns_when_proxy_trust_is_permissive(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.delenv("TRUSTED_PROXY_CIDRS_ENABLED", raising=False)
    monkeypatch.delenv("TRUSTED_PROXY_CIDRS", raising=False)

    _, admin_key = create_admin_and_api_key(capsys, username="proxywarnadmin", email="proxywarnadmin@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        response = client.get(
            "/api/v1/admin/runtime-status",
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["public_origin_enabled"] is True
        assert payload["forwarded_headers_policy"] == "permissive"
        assert payload["trusted_proxy_cidrs_enabled"] is False
        assert "local development" in payload["proxy_trust_warning"]
        assert "nginx" in payload["proxy_trust_warning"]


def test_admin_runtime_status_reports_direct_request_public_origin_mode(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://192.168.0.100:8000")
    monkeypatch.setenv("PUBLIC_ORIGIN_ENABLED", "false")

    _, admin_key = create_admin_and_api_key(capsys, username="originmodeadmin", email="originmodeadmin@example.com")

    with TestClient(app, base_url="http://192.168.0.100:8000") as client:
        response = client.get(
            "/api/v1/admin/runtime-status",
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["public_origin_enabled"] is False
        assert payload["public_origin_mode"] == "direct_request"
        assert payload["redis"]["session_fail_closed"] is False


def test_admin_runtime_status_reports_redis_disabled_when_redis_queue_mode_is_not_backed_by_redis(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("TASK_QUEUE_MODE", "redis")
    monkeypatch.delenv("REDIS_URL", raising=False)

    _, admin_key = create_admin_and_api_key(capsys, username="noredisadmin", email="noredisadmin@example.com")

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/admin/runtime-status",
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["tasks"]["mode"] == "redis"
        assert payload["redis"]["configured"] is False
        assert payload["redis"]["reachable"] is False
        assert payload["services"]["scheduler"]["lease_enabled"] is False


def test_admin_browser_session_can_patch_runtime_config_used_by_admin_ui(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    admin_id, _ = create_admin_and_api_key(capsys, username="sessioncfgadmin", email="sessioncfgadmin@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, admin_id, "admin-pass")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "sessioncfgadmin@example.com", "password": "admin-pass"},
        )
        assert login.status_code == 200

        patched = client.patch(
            "/api/v1/admin/config",
            json={
                "allow_registration": False,
                "anon_upload_enabled": False,
                "anon_expiry_hours": 72,
            },
            headers=browser_session_headers("https://testserver", "/admin/config"),
        )
        assert patched.status_code == 200
        patched_payload = patched.json()
        assert patched_payload["allow_registration"]["value"] is False
        assert patched_payload["anon_upload_enabled"]["value"] is False
        assert patched_payload["anon_expiry_hours"]["value"] == 72

        fetched = client.get("/api/v1/admin/config")
        assert fetched.status_code == 200
        fetched_payload = fetched.json()
        assert fetched_payload["allow_registration"]["value"] is False
        assert fetched_payload["anon_upload_enabled"]["value"] is False
        assert fetched_payload["anon_expiry_hours"]["value"] == 72


def test_admin_config_reports_allow_registration_locked_to_env_default(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ALLOW_REGISTRATION", "false")
    monkeypatch.setenv("LOCK_ALLOW_REGISTRATION", "true")

    _, admin_key = create_admin_and_api_key(capsys, username="lockedregadmin", email="lockedregadmin@example.com")

    with TestClient(app) as client:
        read = client.get("/api/v1/admin/config", headers={"Authorization": f"Bearer {admin_key}"})
        assert read.status_code == 200
        payload = read.json()
        assert payload["allow_registration"]["value"] is False
        assert payload["allow_registration"]["default"] is False
        assert payload["allow_registration"]["locked"] is True
        assert payload["allow_registration"]["source"] == "locked"


def test_admin_config_rejects_allow_registration_update_when_locked_by_env(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("ALLOW_REGISTRATION", "false")
    monkeypatch.setenv("LOCK_ALLOW_REGISTRATION", "true")

    _, admin_key = create_admin_and_api_key(capsys, username="lockedpatchadmin", email="lockedpatchadmin@example.com")

    with TestClient(app) as client:
        update = client.patch(
            "/api/v1/admin/config",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"allow_registration": True},
        )
        assert update.status_code == 403
        assert update.json()["detail"] == "allow_registration is locked by environment configuration."


def test_admin_user_management_and_stats(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="admin", email="admin@example.com")
    user_id, user_key = create_user_and_api_key(capsys, username="grace", email="grace@example.com")

    with TestClient(app) as client:
        upload = client.post(
            "/api/v1/upload",
            files=[("file", ("sample.png", BytesIO(PNG_1X1), "image/png"))],
            data={"title": "Grace Album"},
            headers={"Authorization": f"Bearer {user_key}"},
        )
        assert upload.status_code == 200
        album_id = upload.json()["album_id"]

        users = client.get("/api/v1/admin/users", headers={"Authorization": f"Bearer {admin_key}"})
        assert users.status_code == 200
        users_payload = users.json()
        listed = {item["id"]: item for item in users_payload["items"]}
        assert listed[user_id]["storage_used_bytes"] > 0
        assert listed[user_id]["album_count"] == 1
        assert listed[user_id]["suspended"] is False

        user_detail = client.get(f"/api/v1/admin/users/{user_id}", headers={"Authorization": f"Bearer {admin_key}"})
        assert user_detail.status_code == 200
        user_detail_payload = user_detail.json()
        assert user_detail_payload["id"] == user_id
        assert user_detail_payload["username"] == "grace"
        assert user_detail_payload["storage_used_bytes"] > 0
        assert user_detail_payload["album_count"] == 1
        assert user_detail_payload["media_count"] == 1

        user_stats = client.get(f"/api/v1/admin/users/{user_id}/stats", headers={"Authorization": f"Bearer {admin_key}"})
        assert user_stats.status_code == 200
        user_stats_payload = user_stats.json()
        assert user_stats_payload["user_id"] == user_id
        assert user_stats_payload["username"] == "grace"
        assert user_stats_payload["quota_bytes"] > 0
        assert user_stats_payload["storage_used_bytes"] > 0
        assert user_stats_payload["album_count"] == 1
        assert user_stats_payload["media_count"] == 1

        user_albums = client.get(f"/api/v1/admin/users/{user_id}/albums", headers={"Authorization": f"Bearer {admin_key}"})
        assert user_albums.status_code == 200
        albums_payload = user_albums.json()
        assert_paginated_envelope(albums_payload, limit=10, offset=0, total=1, has_more=False)
        assert len(albums_payload["items"]) == 1
        assert albums_payload["items"][0]["id"] == album_id
        assert albums_payload["items"][0]["title"] == "Grace Album"
        assert albums_payload["items"][0]["owner_username"] == "grace"
        assert albums_payload["items"][0]["item_count"] == 1
        assert albums_payload["items"][0]["cover_url"].endswith(f"/i/{albums_payload['items'][0]['items'][0]['id']}.png")
        assert f"/t/{albums_payload['items'][0]['items'][0]['id']}." in albums_payload["items"][0]["items"][0]["thumb_url"]

        missing_user_albums = client.get(
            f"/api/v1/admin/users/{uuid4()}/albums",
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        assert missing_user_albums.status_code == 404

        missing_user_detail = client.get(
            f"/api/v1/admin/users/{uuid4()}",
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        assert missing_user_detail.status_code == 404

        missing_user_stats = client.get(
            f"/api/v1/admin/users/{uuid4()}/stats",
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        assert missing_user_stats.status_code == 404

        created = client.post(
            "/api/v1/admin/users",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={
                "username": "harry",
                "email": "harry@example.com",
                "password": "secret-pass",
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
        refreshed_list = {item["id"]: item for item in refreshed_users.json()["items"]}
        assert refreshed_list[created_user["id"]]["rate_limit_rpm"] == 12
        assert refreshed_list[created_user["id"]]["rate_limit_bph"] == 34567

        patched = client.patch(
            f"/api/v1/admin/users/{created_user['id']}",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"is_admin": True, "suspended": True, "quota_bytes": 999, "rate_limit_rpm": 7},
        )
        assert patched.status_code == 200
        assert patched.json()["is_admin"] is True
        assert patched.json()["suspended"] is True
        assert patched.json()["quota_bytes"] == 999
        assert patched.json()["rate_limit_rpm"] == 7

        refreshed_users = client.get("/api/v1/admin/users", headers={"Authorization": f"Bearer {admin_key}"})
        assert refreshed_users.status_code == 200
        refreshed_list = {item["id"]: item for item in refreshed_users.json()["items"]}
        assert refreshed_list[created_user["id"]]["is_admin"] is True
        assert refreshed_list[created_user["id"]]["rate_limit_rpm"] == 7

        demoted = client.patch(
            f"/api/v1/admin/users/{created_user['id']}",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"is_admin": False},
        )
        assert demoted.status_code == 200
        assert demoted.json()["is_admin"] is False

        refreshed_users = client.get("/api/v1/admin/users", headers={"Authorization": f"Bearer {admin_key}"})
        assert refreshed_users.status_code == 200
        refreshed_list = {item["id"]: item for item in refreshed_users.json()["items"]}
        assert refreshed_list[created_user["id"]]["is_admin"] is False

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


def test_admin_user_listing_supports_search_filters_and_pagination(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="adminsearch", email="adminsearch@example.com")
    create_user_and_api_key(capsys, username="alphauser", email="alpha@example.com")
    create_admin_and_api_key(capsys, username="betadmin", email="beta@example.com")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/admin/users",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={
                "username": "suspendeduser",
                "email": "suspended@example.com",
                "password": "secret-pass",
            },
        )
        assert created.status_code == 201
        suspended_user_id = created.json()["id"]
        suspended_patch = client.patch(
            f"/api/v1/admin/users/{suspended_user_id}",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"suspended": True},
        )
        assert suspended_patch.status_code == 200

        first_page = client.get(
            "/api/v1/admin/users",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"limit": 1, "offset": 0},
        )
        assert first_page.status_code == 200
        first_payload = first_page.json()
        assert_paginated_envelope(
            first_payload,
            limit=1,
            offset=0,
            total=first_payload["total"],
            has_more=True,
        )
        assert first_payload["total"] >= 3
        assert len(first_payload["items"]) == 1

        second_page = client.get(
            "/api/v1/admin/users",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"limit": 1, "offset": 1},
        )
        assert second_page.status_code == 200
        second_payload = second_page.json()
        assert_paginated_envelope(
            second_payload,
            limit=1,
            offset=1,
            total=first_payload["total"],
            has_more=True,
        )
        assert len(second_payload["items"]) == 1
        assert second_payload["items"][0]["id"] != first_payload["items"][0]["id"]

        search = client.get(
            "/api/v1/admin/users",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"q": "alpha"},
        )
        assert search.status_code == 200
        search_payload = search.json()
        assert any(item["username"] == "alphauser" for item in search_payload["items"])

        admins_only = client.get(
            "/api/v1/admin/users",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"is_admin": True},
        )
        assert admins_only.status_code == 200
        assert admins_only.json()["items"]
        assert all(item["is_admin"] is True for item in admins_only.json()["items"])

        suspended_only = client.get(
            "/api/v1/admin/users",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"suspended": True},
        )
        assert suspended_only.status_code == 200
        suspended_payload = suspended_only.json()
        assert suspended_payload["items"]
        assert any(item["id"] == suspended_user_id for item in suspended_payload["items"])
        assert all(item["suspended"] is True for item in suspended_payload["items"])

        bad_limit = client.get(
            "/api/v1/admin/users",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"limit": 5000},
        )
        assert bad_limit.status_code == 400


def test_admin_create_user_rejects_password_shorter_than_eight_characters(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="shortadmincreate", email="shortadmincreate@example.com")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/admin/users",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={
                "username": "shortcreated",
                "email": "shortcreated@example.com",
                "password": "short7!",
            },
        )
        assert created.status_code == 400
        assert created.json()["detail"] == "Password must be at least 8 characters."


def test_admin_user_listing_uses_default_envelope_exact_boundary_and_empty_page(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="adminusersdefaults", email="adminusersdefaults@example.com")
    for index in range(3):
        create_user_and_api_key(capsys, username=f"boundaryuser{index}", email=f"boundaryuser{index}@example.com")

    with TestClient(app) as client:
        exact = client.get(
            "/api/v1/admin/users",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"q": "boundaryuser", "limit": 3, "offset": 0},
        )
        assert exact.status_code == 200
        exact_payload = exact.json()
        assert_paginated_envelope(exact_payload, limit=3, offset=0, total=3, has_more=False)
        assert [item["username"] for item in exact_payload["items"]] == [
            "boundaryuser2",
            "boundaryuser1",
            "boundaryuser0",
        ]

        beyond = client.get(
            "/api/v1/admin/users",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"q": "boundaryuser", "limit": 3, "offset": 10},
        )
        assert beyond.status_code == 200
        beyond_payload = beyond.json()
        assert_paginated_envelope(beyond_payload, limit=3, offset=10, total=3, has_more=False)
        assert beyond_payload["items"] == []


def test_admin_user_listing_rejects_invalid_pagination_values(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="adminusersinvalid", email="adminusersinvalid@example.com")

    with TestClient(app) as client:
        for params, detail in (
            ({"limit": 0}, "limit must be between 1 and 200."),
            ({"limit": -1}, "limit must be between 1 and 200."),
            ({"limit": 201}, "limit must be between 1 and 200."),
            ({"offset": -1}, "offset must be non-negative."),
        ):
            response = client.get("/api/v1/admin/users", headers={"Authorization": f"Bearer {admin_key}"}, params=params)
            assert response.status_code == 400
            assert response.json()["detail"] == detail

        bad_limit_type = client.get(
            "/api/v1/admin/users",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"limit": "abc"},
        )
        assert bad_limit_type.status_code == 422
        assert any(item["loc"][-1] == "limit" for item in bad_limit_type.json()["detail"])

        bad_offset_type = client.get(
            "/api/v1/admin/users",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"offset": "abc"},
        )
        assert bad_offset_type.status_code == 422
        assert any(item["loc"][-1] == "offset" for item in bad_offset_type.json()["detail"])


def test_admin_user_album_listing_supports_pagination_and_validation(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="adminuseralbums", email="adminuseralbums@example.com")
    user_id, user_key = create_user_and_api_key(capsys, username="pagedalbums", email="pagedalbums@example.com")

    with TestClient(app) as client:
        for index in range(12):
            created = client.post(
                "/api/v1/upload",
                files=[("file", (f"{index}.png", BytesIO(PNG_1X1), "image/png"))],
                data={"title": f"Paged Album {index}"},
                headers={"Authorization": f"Bearer {user_key}"},
            )
            assert created.status_code == 200

        first_page = client.get(
            f"/api/v1/admin/users/{user_id}/albums",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"limit": 10, "offset": 0},
        )
        assert first_page.status_code == 200
        first_payload = first_page.json()
        assert_paginated_envelope(first_payload, limit=10, offset=0, total=12, has_more=True)
        assert len(first_payload["items"]) == 10

        second_page = client.get(
            f"/api/v1/admin/users/{user_id}/albums",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"limit": 10, "offset": 10},
        )
        assert second_page.status_code == 200
        second_payload = second_page.json()
        assert_paginated_envelope(second_payload, limit=10, offset=10, total=12, has_more=False)
        assert len(second_payload["items"]) == 2

        empty_page = client.get(
            f"/api/v1/admin/users/{user_id}/albums",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"limit": 10, "offset": 20},
        )
        assert empty_page.status_code == 200
        empty_payload = empty_page.json()
        assert_paginated_envelope(empty_payload, limit=10, offset=20, total=12, has_more=False)
        assert empty_payload["items"] == []

        for params, detail in (
            ({"limit": 0}, "limit must be between 1 and 200."),
            ({"limit": -1}, "limit must be between 1 and 200."),
            ({"limit": 201}, "limit must be between 1 and 200."),
            ({"offset": -1}, "offset must be non-negative."),
        ):
            response = client.get(
                f"/api/v1/admin/users/{user_id}/albums",
                headers={"Authorization": f"Bearer {admin_key}"},
                params=params,
            )
            assert response.status_code == 400
            assert response.json()["detail"] == detail

        bad_limit_type = client.get(
            f"/api/v1/admin/users/{user_id}/albums",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"limit": "abc"},
        )
        assert bad_limit_type.status_code == 422
        assert any(item["loc"][-1] == "limit" for item in bad_limit_type.json()["detail"])

        bad_offset_type = client.get(
            f"/api/v1/admin/users/{user_id}/albums",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"offset": "abc"},
        )
        assert bad_offset_type.status_code == 422
        assert any(item["loc"][-1] == "offset" for item in bad_offset_type.json()["detail"])


def test_admin_user_album_listing_returns_empty_envelope_for_user_with_no_albums(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="adminemptyalbums", email="adminemptyalbums@example.com")
    user_id, _ = create_user_and_api_key(capsys, username="emptyalbums", email="emptyalbums@example.com")

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/admin/users/{user_id}/albums",
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert_paginated_envelope(payload, limit=10, offset=0, total=0, has_more=False)
        assert payload["items"] == []


def test_admin_user_album_listing_uses_album_cover_for_preview_payload(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="admincoveralbums", email="admincoveralbums@example.com")
    user_id, user_key = create_user_and_api_key(capsys, username="coverowner", email="coverowner@example.com")

    with TestClient(app) as client:
        created = client.post(
            "/api/v1/upload",
            files=[
                ("file", ("one.png", BytesIO(PNG_1X1), "image/png")),
                ("file", ("two.png", BytesIO(PNG_1X1), "image/png")),
            ],
            data={"title": "Cover Album"},
            headers={"Authorization": f"Bearer {user_key}"},
        )
        assert created.status_code == 200
        payload = created.json()
        album_id = payload["album_id"]
        media_ids = [item["media_id"] for item in payload["items"]]

        patched = client.patch(
            f"/api/v1/album/{album_id}",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"cover_media_id": media_ids[1]},
        )
        assert patched.status_code == 200

        listed = client.get(
            f"/api/v1/admin/users/{user_id}/albums",
            headers={"Authorization": f"Bearer {admin_key}"},
        )
        assert listed.status_code == 200
        album_payload = listed.json()["items"][0]
        assert album_payload["id"] == album_id
        assert album_payload["cover_media_id"] == media_ids[1]
        assert album_payload["cover_url"].endswith(f"/i/{media_ids[1]}.png")
        assert [item["id"] for item in album_payload["items"]] == media_ids


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
        album = next(item for item in albums.json()["items"] if item["id"] == payload["album_id"])
        assert album["owner_username"] == "jules"
        assert album["user_id"] == user_id
        assert album["item_count"] == 1
        assert album["cover_url"].endswith(f"/i/{payload['media_id']}.png")
        assert f"/t/{payload['media_id']}." in album["items"][0]["thumb_url"]

        expiry = utcnow().replace(microsecond=0).isoformat()
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


def test_admin_album_listing_supports_pagination_and_filters(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="adminalbums", email="adminalbums@example.com")
    _, owner_key = create_user_and_api_key(capsys, username="ownersearch", email="ownersearch@example.com")

    with TestClient(app) as client:
        for index in range(12):
            created = client.post(
                "/api/v1/upload",
                files=[("file", (f"{index}.png", BytesIO(PNG_1X1), "image/png"))],
                data={"title": f"Managed Album {index}"},
                headers={"Authorization": f"Bearer {owner_key}"},
            )
            assert created.status_code == 200

        first_page = client.get(
            "/api/v1/admin/albums",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"limit": 10, "offset": 0},
        )
        assert first_page.status_code == 200
        first_payload = first_page.json()
        assert_paginated_envelope(first_payload, limit=10, offset=0, total=12, has_more=True)
        assert len(first_payload["items"]) == 10

        second_page = client.get(
            "/api/v1/admin/albums",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"limit": 10, "offset": 10},
        )
        assert second_page.status_code == 200
        second_payload = second_page.json()
        assert_paginated_envelope(second_payload, limit=10, offset=10, total=12, has_more=False)
        assert len(second_payload["items"]) == 2

        owner_filtered = client.get(
            "/api/v1/admin/albums",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"owner": "ownersearch"},
        )
        assert owner_filtered.status_code == 200
        assert owner_filtered.json()["items"]
        assert all(item["owner_username"] == "ownersearch" for item in owner_filtered.json()["items"])


def test_admin_album_listing_uses_default_envelope_exact_boundary_and_empty_page(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="adminalbumsdefaults", email="adminalbumsdefaults@example.com")
    _, owner_key = create_user_and_api_key(capsys, username="albumboundary", email="albumboundary@example.com")

    with TestClient(app) as client:
        for index in range(10):
            created = client.post(
                "/api/v1/upload",
                files=[("file", (f"{index}.png", BytesIO(PNG_1X1), "image/png"))],
                data={"title": f"Boundary Album {index}"},
                headers={"Authorization": f"Bearer {owner_key}"},
            )
            assert created.status_code == 200

        exact = client.get(
            "/api/v1/admin/albums",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"owner": "albumboundary"},
        )
        assert exact.status_code == 200
        exact_payload = exact.json()
        assert_paginated_envelope(exact_payload, limit=10, offset=0, total=10, has_more=False)
        assert len(exact_payload["items"]) == 10
        assert all(item["owner_username"] == "albumboundary" for item in exact_payload["items"])

        beyond = client.get(
            "/api/v1/admin/albums",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"owner": "albumboundary", "limit": 10, "offset": 15},
        )
        assert beyond.status_code == 200
        beyond_payload = beyond.json()
        assert_paginated_envelope(beyond_payload, limit=10, offset=15, total=10, has_more=False)
        assert beyond_payload["items"] == []


def test_admin_album_listing_rejects_invalid_pagination_values(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    _, admin_key = create_admin_and_api_key(capsys, username="adminalbumsinvalid", email="adminalbumsinvalid@example.com")

    with TestClient(app) as client:
        for params, detail in (
            ({"limit": 0}, "limit must be between 1 and 200."),
            ({"limit": -1}, "limit must be between 1 and 200."),
            ({"limit": 201}, "limit must be between 1 and 200."),
            ({"offset": -1}, "offset must be non-negative."),
        ):
            response = client.get("/api/v1/admin/albums", headers={"Authorization": f"Bearer {admin_key}"}, params=params)
            assert response.status_code == 400
            assert response.json()["detail"] == detail

        bad_limit_type = client.get(
            "/api/v1/admin/albums",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"limit": "abc"},
        )
        assert bad_limit_type.status_code == 422
        assert any(item["loc"][-1] == "limit" for item in bad_limit_type.json()["detail"])

        bad_offset_type = client.get(
            "/api/v1/admin/albums",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"offset": "abc"},
        )
        assert bad_offset_type.status_code == 422
        assert any(item["loc"][-1] == "offset" for item in bad_offset_type.json()["detail"])


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
        assert [item["event_type"] for item in upload_payload] == ["media_uploaded", "album_created", "api_key_authenticated"]
        domain_events = [item for item in upload_payload if item["event_type"] != "api_key_authenticated"]
        assert all(item["actor_id"] == user_id for item in domain_events)
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


def test_admin_user_role_and_limits_changes_are_audited(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    admin_id, admin_key = create_admin_and_api_key(capsys, username="auditchangeadmin", email="auditchangeadmin@example.com")
    user_id, _ = create_user_and_api_key(capsys, username="auditroleuser", email="auditroleuser@example.com")

    with TestClient(app) as client:
        patched = client.patch(
            f"/api/v1/admin/users/{user_id}",
            headers={"Authorization": f"Bearer {admin_key}", "X-Correlation-ID": "audit-role-limits"},
            json={"is_admin": True, "quota_bytes": 2048, "rate_limit_rpm": 12, "rate_limit_bph": 3456},
        )
        assert patched.status_code == 200
        assert patched.json()["is_admin"] is True

        role_events = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "user_admin_status_changed", "correlation_id": "audit-role-limits"},
        )
        assert role_events.status_code == 200
        role_payload = role_events.json()
        assert len(role_payload) == 1
        assert role_payload[0]["actor_id"] == admin_id
        assert role_payload[0]["target_id"] == user_id
        assert role_payload[0]["metadata"]["old_is_admin"] is False
        assert role_payload[0]["metadata"]["new_is_admin"] is True

        limits_events = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "user_limits_changed", "correlation_id": "audit-role-limits"},
        )
        assert limits_events.status_code == 200
        limits_payload = limits_events.json()
        assert len(limits_payload) == 1
        changes = limits_payload[0]["metadata"]["changes"]
        assert changes["quota_bytes"] == {"old": None, "new": 2048}
        assert changes["rate_limit_rpm"] == {"old": None, "new": 12}
        assert changes["rate_limit_bph"] == {"old": None, "new": 3456}


def test_admin_audit_listing_supports_limit_offset_and_validation_contract(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    admin_id, admin_key = create_admin_and_api_key(
        capsys, username="auditpagingadmin", email="auditpagingadmin@example.com"
    )
    user_id, _ = create_user_and_api_key(capsys, username="auditpageuser", email="auditpageuser@example.com")

    with TestClient(app) as client:
        for index in range(3):
            response = client.patch(
                f"/api/v1/admin/users/{user_id}",
                headers={"Authorization": f"Bearer {admin_key}", "X-Correlation-ID": f"audit-page-{index}"},
                json={"suspended": bool(index % 2)},
            )
            assert response.status_code == 200

        first_page = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"actor_id": admin_id, "limit": 1, "offset": 0},
        )
        assert first_page.status_code == 200
        first_payload = first_page.json()
        assert isinstance(first_payload, list)
        assert len(first_payload) == 1
        assert first_payload[0]["actor_id"] == admin_id

        second_page = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"actor_id": admin_id, "limit": 1, "offset": 1},
        )
        assert second_page.status_code == 200
        second_payload = second_page.json()
        assert isinstance(second_payload, list)
        assert len(second_payload) == 1
        assert second_payload[0]["id"] != first_payload[0]["id"]

        beyond = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"actor_id": admin_id, "limit": 1, "offset": 10},
        )
        assert beyond.status_code == 200
        assert beyond.json() == []

        for params, detail in (
            ({"limit": 0}, "limit must be between 1 and 500."),
            ({"limit": -1}, "limit must be between 1 and 500."),
            ({"limit": 501}, "limit must be between 1 and 500."),
            ({"offset": -1}, "offset must be non-negative."),
        ):
            response = client.get("/api/v1/admin/audit", headers={"Authorization": f"Bearer {admin_key}"}, params=params)
            assert response.status_code == 400
            assert response.json()["detail"] == detail

        bad_limit_type = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"limit": "abc"},
        )
        assert bad_limit_type.status_code == 422
        assert any(item["loc"][-1] == "limit" for item in bad_limit_type.json()["detail"])

        bad_offset_type = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"offset": "abc"},
        )
        assert bad_offset_type.status_code == 422
        assert any(item["loc"][-1] == "offset" for item in bad_offset_type.json()["detail"])


def test_admin_audit_listing_supports_action_result_source_and_request_id_filters(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "audit-filter-secret")

    admin_id, admin_key = create_admin_and_api_key(capsys, username="auditfilteradmin", email="auditfilteradmin@example.com")
    user_id, user_key = create_user_and_api_key(capsys, username="auditfilteruser", email="auditfilteruser@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, admin_id, "admin-pass")
        set_user_password(client, user_id, "user-pass")

        admin_login = client.post(
            "/api/v1/auth/login",
            headers={"X-Correlation-ID": "audit-filter-admin-login"},
            json={"login": "auditfilteradmin", "password": "admin-pass"},
        )
        assert admin_login.status_code == 200

        user_login = client.post(
            "/api/v1/auth/login",
            headers={"X-Correlation-ID": "audit-filter-user-login"},
            json={"login": "auditfilteruser", "password": "user-pass"},
        )
        assert user_login.status_code == 200
        user_request_id = user_login.headers["X-Request-ID"]

        denied = client.get(
            "/api/v1/admin/users",
            headers={"Authorization": f"Bearer {user_key}", "X-Correlation-ID": "audit-filter-denied"},
        )
        assert denied.status_code == 403
        denied_request_id = denied.headers["X-Request-ID"]

        cli_events = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"event_type": "cli_command_executed", "source": "cli"},
        )
        assert cli_events.status_code == 200
        cli_payload = cli_events.json()
        assert cli_payload
        assert all(item["source"] == "cli" for item in cli_payload)

        login_success = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={"action": "auth.login.success", "result": "success", "request_id": user_request_id},
        )
        assert login_success.status_code == 200
        success_payload = login_success.json()
        assert len(success_payload) == 1
        assert success_payload[0]["event_type"] == "user_login"
        assert success_payload[0]["source"] == "web"
        assert success_payload[0]["request_id"] == user_request_id
        assert success_payload[0]["actor_id"] == user_id

        denial = client.get(
            "/api/v1/admin/audit",
            headers={"Authorization": f"Bearer {admin_key}"},
            params={
                "action": "auth.admin.denied",
                "result": "denied",
                "source": "api",
                "request_id": denied_request_id,
            },
        )
        assert denial.status_code == 200
        denial_payload = denial.json()
        assert len(denial_payload) == 1
        assert denial_payload[0]["event_type"] == "admin_access_denied"
        assert denial_payload[0]["request_id"] == denied_request_id
        assert denial_payload[0]["reason"] == "admin_required"
        assert denial_payload[0]["actor_id"] == user_id


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
