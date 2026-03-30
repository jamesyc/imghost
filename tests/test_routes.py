from collections import defaultdict

from imghost.main import app


def test_expected_routes_are_registered() -> None:
    routes_by_path: dict[str, set[str]] = defaultdict(set)
    for route in app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if not methods or not path:
            continue
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            routes_by_path[path].add(method)

    expected = {
        "/": {"GET"},
        "/login": {"GET"},
        "/register": {"GET"},
        "/dashboard": {"GET"},
        "/albums": {"GET"},
        "/albums/{album_id}": {"GET"},
        "/manage/{album_id}": {"GET"},
        "/settings": {"GET"},
        "/admin": {"GET"},
        "/admin/users": {"GET"},
        "/admin/users/{user_id}": {"GET"},
        "/admin/users/new": {"GET"},
        "/admin/albums": {"GET"},
        "/admin/config": {"GET"},
        "/admin/ops": {"GET"},
        "/a/{album_id}": {"GET"},
        "/u/{username}": {"GET"},
        "/sharex/delete/{album_id}": {"GET"},
        "/sharex/delete/{album_id}/confirm": {"GET", "POST"},
        "/api/v1/auth/login": {"POST"},
        "/api/v1/auth/register": {"POST"},
        "/api/v1/auth/logout": {"POST"},
        "/auth/google/start": {"GET"},
        "/auth/google/callback": {"GET"},
        "/api/v1/upload": {"POST"},
        "/api/v1/album/{album_id}": {"GET", "DELETE", "PATCH"},
        "/api/v1/album/{album_id}/zip": {"GET"},
        "/api/v1/album/{album_id}/order": {"PATCH"},
        "/api/v1/media/{media_id}": {"DELETE"},
        "/api/v1/user/me": {"GET", "DELETE"},
        "/api/v1/user/me/albums": {"GET"},
        "/api/v1/user/me/api-key": {"POST"},
        "/api/v1/user/me/password": {"PATCH"},
        "/api/v1/user/me/sharex-config": {"POST"},
        "/api/v1/user/me/oauth/google/disconnect": {"POST"},
        "/api/v1/admin/users/{user_id}": {"GET", "PATCH", "DELETE"},
        "/api/v1/admin/users/{user_id}/stats": {"GET"},
        "/api/v1/admin/users/{user_id}/albums": {"GET"},
        "/api/v1/admin/users/{user_id}/reset-password": {"POST"},
        "/api/v1/admin/albums": {"GET"},
        "/api/v1/admin/albums/{album_id}": {"PATCH", "DELETE"},
        "/api/v1/admin/audit": {"GET"},
        "/api/v1/admin/config": {"GET", "PATCH"},
        "/api/v1/admin/stats": {"GET"},
        "/api/v1/admin/runtime-status": {"GET"},
        "/i/{raw_id}": {"GET"},
        "/t/{raw_id}": {"GET"},
        "/health/live": {"GET"},
        "/health/ready": {"GET"},
    }

    for path, methods in expected.items():
        assert path in routes_by_path
        assert methods.issubset(routes_by_path[path])


def test_static_files_route_is_registered() -> None:
    assert any(getattr(route, "path", None) == "/static" for route in app.routes)
