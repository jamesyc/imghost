from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Scope

from imghost.web.account_delete_reauth_cookie import (
    ACCOUNT_DELETE_REAUTH_COOKIE_NAME,
    clear_account_delete_reauth_cookie,
    load_account_delete_reauth_cookie,
    set_account_delete_reauth_cookie,
)


def _request_with_settings(*, secure: bool, cookie_header: str | None = None) -> Request:
    scope: Scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https" if secure else "http",
        "path": "/settings",
        "raw_path": b"/settings",
        "query_string": b"",
        "headers": ([] if cookie_header is None else [(b"cookie", cookie_header.encode("latin-1"))]),
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 443 if secure else 80),
        "app": type("App", (), {"state": type("State", (), {"imghost": type("Imghost", (), {"settings": type("Settings", (), {"session_cookie_secure": secure})()})()})()})(),
    }
    return Request(scope)


def test_set_account_delete_reauth_cookie_uses_expected_flags() -> None:
    request = _request_with_settings(secure=True)
    response = Response()

    set_account_delete_reauth_cookie(response, request, "reauth-token")

    set_cookie = response.headers["set-cookie"]
    assert f"{ACCOUNT_DELETE_REAUTH_COOKIE_NAME}=reauth-token" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Secure" in set_cookie
    assert "Max-Age=600" in set_cookie
    assert "Path=/" in set_cookie


def test_clear_account_delete_reauth_cookie_uses_matching_cookie_scope() -> None:
    request = _request_with_settings(secure=True)
    response = Response()

    clear_account_delete_reauth_cookie(response, request)

    set_cookie = response.headers["set-cookie"]
    assert f"{ACCOUNT_DELETE_REAUTH_COOKIE_NAME}=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Secure" in set_cookie
    assert "Path=/" in set_cookie
    assert "Max-Age=0" in set_cookie


def test_load_account_delete_reauth_cookie_returns_stripped_value() -> None:
    request = _request_with_settings(secure=False, cookie_header=f"{ACCOUNT_DELETE_REAUTH_COOKIE_NAME}=  reauth-token  ")

    assert load_account_delete_reauth_cookie(request) == "reauth-token"
