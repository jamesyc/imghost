from __future__ import annotations

import logging
from dataclasses import dataclass
from hashlib import sha256

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from ..config import Settings
from ..models import User, utcnow
from .page_context import login_redirect
from .request_context import get_state
from .request_helpers import auth_rate_limit_ip_key


@dataclass
class ResolvedPrincipal:
    user: User
    raw_api_key: str | None = None


def apply_session_cookie(response: Response, settings: Settings, token: str, *, expires_at) -> None:
    max_age = None
    if expires_at is not None:
        max_age = max(1, int((expires_at - utcnow()).total_seconds()))
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        max_age=max_age,
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=settings.session_cookie_name,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
    )


async def clear_browser_session(request: Request, response: Response) -> None:
    state = get_state(request)
    await state.session_backend.clear_session(request.cookies.get(state.settings.session_cookie_name))
    clear_session_cookie(response, state.settings)


async def authenticated_principal(request: Request, *, required: bool = False) -> ResolvedPrincipal | None:
    state = get_state(request)
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and token:
        ip_key = auth_rate_limit_ip_key(request)
        try:
            await state.auth_rate_limiter.enforce_api_key_attempt(ip_key=ip_key)
        except HTTPException as exc:
            if exc.status_code == 429:
                await state.telemetry.record_auth_rate_limited(request, scope="api_key", method="api_key")
            raise
        request.state.telemetry_auth_method = "api_key"
        api_key = await state.repository.get_api_key_by_hash(sha256(token.encode("utf-8")).hexdigest())
        if api_key is None:
            await state.auth_rate_limiter.record_api_key_failure(ip_key=ip_key)
            await state.telemetry.record_api_key_auth_failed(
                request,
                actor=None,
                object_type="auth",
                object_id="api_key",
                reason="invalid_api_key",
            )
            raise HTTPException(status_code=401, detail="Invalid API key.")
        user = await state.repository.get_user(api_key.user_id)
        if user is None or user.suspended:
            await state.auth_rate_limiter.record_api_key_failure(ip_key=ip_key)
            await state.telemetry.record_api_key_auth_failed(
                request,
                actor=user,
                object_type="user",
                object_id=api_key.user_id,
                reason="suspended" if user is not None and user.suspended else "missing_user",
                admin_denial=bool(user is not None and user.is_admin),
            )
            raise HTTPException(status_code=403, detail="User is not allowed to authenticate.")
        await state.auth_rate_limiter.record_api_key_success(ip_key=ip_key)
        api_key.last_used_at = utcnow()
        await state.repository.update_api_key(api_key)
        await state.telemetry.record_api_key_authenticated(request, user=user, api_key_id=api_key.id)
        return ResolvedPrincipal(user=user, raw_api_key=token)

    session_token = request.cookies.get(state.settings.session_cookie_name)
    if session_token:
        request.state.telemetry_auth_method = "session"
        user_id = await state.session_backend.resolve_user(session_token)
        if not user_id:
            request.state.clear_session_cookie = True
        else:
            user = await state.repository.get_user(user_id)
            if user is None:
                request.state.clear_session_cookie = True
                if required:
                    raise HTTPException(status_code=401, detail="Invalid session.")
                return None
            if user.suspended:
                raise HTTPException(status_code=403, detail="User is not allowed to authenticate.")
            return ResolvedPrincipal(user=user)

    if required:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return None


async def authenticated_user(request: Request, *, required: bool = False) -> User | None:
    principal = await authenticated_principal(request, required=required)
    return principal.user if principal else None


async def require_admin_user(request: Request) -> User:
    state = get_state(request)
    ip_key = auth_rate_limit_ip_key(request)
    try:
        await state.auth_rate_limiter.enforce_admin_attempt(ip_key=ip_key)
    except HTTPException as exc:
        if exc.status_code == 429:
            await state.telemetry.record_auth_rate_limited(request, scope="admin", method="api_key")
        raise
    try:
        user = await authenticated_user(request, required=True)
    except HTTPException as exc:
        if exc.status_code in {401, 403}:
            await state.auth_rate_limiter.record_admin_denial(ip_key=ip_key)
            await state.telemetry.record_admin_access_denied(
                request,
                actor=None,
                object_type="admin",
                reason="authentication_required" if exc.status_code == 401 else "forbidden",
                source="api",
            )
        raise
    if user is None or not user.is_admin:
        await state.auth_rate_limiter.record_admin_denial(ip_key=ip_key)
        await state.telemetry.record_admin_access_denied(
            request,
            actor=user,
            object_type="admin",
            reason="admin_required",
            source="api",
        )
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


async def require_page_user(request: Request) -> User | RedirectResponse:
    user = await authenticated_user(request, required=False)
    if user is None:
        return login_redirect(str(request.url.path))
    return user


async def require_page_admin(request: Request) -> User | RedirectResponse:
    state = get_state(request)
    ip_key = auth_rate_limit_ip_key(request)
    try:
        await state.auth_rate_limiter.enforce_admin_attempt(ip_key=ip_key)
    except HTTPException as exc:
        if exc.status_code == 429:
            await state.telemetry.record_auth_rate_limited(request, scope="admin", method="session")
        raise
    user = await authenticated_user(request, required=False)
    if user is None:
        await state.auth_rate_limiter.record_admin_denial(ip_key=ip_key)
        await state.telemetry.record_admin_access_denied(
            request,
            actor=None,
            object_type="admin_page",
            reason="authentication_required",
            source="web",
        )
        return login_redirect(str(request.url.path))
    if not user.is_admin:
        await state.auth_rate_limiter.record_admin_denial(ip_key=ip_key)
        await state.telemetry.record_admin_access_denied(
            request,
            actor=user,
            object_type="admin_page",
            reason="admin_required",
            source="web",
        )
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


async def clear_stale_session_cookie(request: Request, call_next):
    request.state.clear_session_cookie = False
    response = await call_next(request)
    if getattr(request.state, "clear_session_cookie", False):
        clear_session_cookie(response, get_state(request).settings)
        logging.getLogger(__name__).info("session_cookie_cleared", extra={"path": request.url.path})
    return response
