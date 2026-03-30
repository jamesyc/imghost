from __future__ import annotations

from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..auth_rate_limits import hash_auth_identifier
from ..events import AdminLoggedIn
from ..service import LocalLoginInput, UserCreateInput
from ..sessions import SessionBackendUnavailable
from .auth_context import (
    apply_session_cookie,
    authenticated_user,
    clear_browser_session,
)
from .request_context import correlation_id, get_state
from .request_helpers import auth_rate_limit_ip_key

router = APIRouter()


class LoginRequest(BaseModel):
    login: str
    password: str
    remember_me: bool = True


class RegistrationRequest(BaseModel):
    username: str
    email: str
    password: str
    remember_me: bool = True


@router.post("/api/v1/auth/login")
async def login(request: Request, payload: LoginRequest) -> JSONResponse:
    state = get_state(request)
    cid = correlation_id(request)
    normalized_login = payload.login.strip()
    ip_key = auth_rate_limit_ip_key(request)
    login_key = hash_auth_identifier(normalized_login)
    try:
        await state.auth_rate_limiter.enforce_login_attempt(ip_key=ip_key, login_key=login_key)
    except HTTPException as exc:
        if exc.status_code == 429:
            await state.telemetry.record_auth_rate_limited(request, scope="login", method="password")
        raise
    try:
        user = await state.uploads.authenticate_local_user(
            LocalLoginInput(login=payload.login, password=payload.password)
        )
    except HTTPException as exc:
        reason = "invalid_credentials"
        if exc.status_code == 400:
            reason = "missing_credentials"
        elif exc.status_code == 403:
            reason = "suspended"
        await state.telemetry.record_login_failed(request, login_identifier=normalized_login, reason=reason)
        await state.auth_rate_limiter.record_login_failure(ip_key=ip_key, login_key=login_key)
        raise
    await state.auth_rate_limiter.record_login_success(login_key=login_key)
    await state.telemetry.record_login_succeeded(request, user=user, remember_me=payload.remember_me)
    if user.is_admin:
        await state.event_bus.emit(
            AdminLoggedIn(
                admin_id=user.id,
                source="web",
                correlation_id=cid,
            )
        )
    try:
        token, expires_at = await state.session_backend.create_session(user, remember_me=payload.remember_me)
    except SessionBackendUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    summary = await state.uploads.get_current_user_summary(user)
    response = JSONResponse({"authenticated": True, "user": summary}, headers={"X-Correlation-ID": cid})
    apply_session_cookie(response, state.settings, token, expires_at=expires_at)
    return response


@router.post("/api/v1/auth/register")
async def register(request: Request, payload: RegistrationRequest) -> JSONResponse:
    state = get_state(request)
    cid = correlation_id(request)
    try:
        await state.auth_rate_limiter.enforce_registration_attempt(ip_key=auth_rate_limit_ip_key(request))
    except HTTPException as exc:
        if exc.status_code == 429:
            await state.telemetry.record_auth_rate_limited(request, scope="registration", method="password")
        raise
    if not await state.runtime_config.get_value("allow_registration"):
        await state.telemetry.record_registration_denied(
            request,
            username=payload.username.strip(),
            email=payload.email.strip().lower(),
        )
        raise HTTPException(status_code=403, detail="Registration is disabled.")
    created = await state.uploads.create_user(
        UserCreateInput(
            username=payload.username,
            email=payload.email,
            password=payload.password,
            is_admin=False,
            quota_bytes=None,
        ),
        method="registration",
        correlation_id=cid,
        source="web",
    )
    try:
        token, expires_at = await state.session_backend.create_session(created, remember_me=payload.remember_me)
    except SessionBackendUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    summary = await state.uploads.get_current_user_summary(created)
    response = JSONResponse({"authenticated": True, "user": summary}, headers={"X-Correlation-ID": cid})
    apply_session_cookie(response, state.settings, token, expires_at=expires_at)
    return response


@router.post("/api/v1/auth/logout")
async def logout(request: Request) -> JSONResponse:
    state = get_state(request)
    cid = correlation_id(request)
    user = None
    try:
        user = await authenticated_user(request, required=False)
    except HTTPException:
        user = None
    if user is not None:
        await state.telemetry.record_logout_succeeded(request, user=user)
    response = JSONResponse({"authenticated": False}, headers={"X-Correlation-ID": cid})
    await clear_browser_session(request, response)
    return response
