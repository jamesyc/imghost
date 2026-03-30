from __future__ import annotations

from fastapi import Request
from fastapi.responses import Response

from .request_context import get_state

ACCOUNT_DELETE_REAUTH_COOKIE_NAME = "imghost_delete_reauth"


def load_account_delete_reauth_cookie(request: Request) -> str | None:
    value = request.cookies.get(ACCOUNT_DELETE_REAUTH_COOKIE_NAME, "").strip()
    return value or None


def set_account_delete_reauth_cookie(response: Response, request: Request, token: str, *, max_age: int = 600) -> None:
    response.set_cookie(
        key=ACCOUNT_DELETE_REAUTH_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=get_state(request).settings.session_cookie_secure,
        max_age=max_age,
        path="/",
    )


def clear_account_delete_reauth_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        key=ACCOUNT_DELETE_REAUTH_COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=get_state(request).settings.session_cookie_secure,
        path="/",
    )
