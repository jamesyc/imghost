from __future__ import annotations

from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..audit import actions
from ..audit.context import anonymous_actor, build_request_context, build_runtime_process_context, hash_client_ip, user_actor
from ..audit.models import AuditObject
from ..events import AdminLoggedIn
from ..service import LocalLoginInput, UserCreateInput
from ..sessions import SessionBackendUnavailable
from .auth_context import (
    apply_session_cookie,
    authenticated_user,
    clear_session_cookie,
)
from .request_context import correlation_id, get_state

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
    request_context = build_request_context(request, auth_method="password")
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
        await state.audit.emit_action(
            event_type=actions.LOGIN_FAILED,
            action="auth.login.failed",
            result="denied",
            actor=anonymous_actor(),
            object=AuditObject(type="auth", id=normalized_login),
            metadata={
                "login_identifier": normalized_login,
                "reason": reason,
                "source": "web",
                "correlation_id": cid,
            },
            request=request_context,
            process=build_runtime_process_context("web"),
            reason=reason,
            actor_ip_hash=hash_client_ip(request_context.client_ip),
        )
        raise
    await state.audit.emit_action(
        event_type=actions.USER_LOGIN,
        action="auth.login.success",
        result="success",
        actor=user_actor(user),
        object=AuditObject(type="user", id=user.id),
        metadata={
            "target_user_id": user.id,
            "remember_me": payload.remember_me,
            "source": "web",
            "correlation_id": cid,
        },
        request=request_context,
        process=build_runtime_process_context("web"),
        actor_ip_hash=hash_client_ip(request_context.client_ip),
    )
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
    if not await state.runtime_config.get_value("allow_registration"):
        request_context = build_request_context(request, auth_method="anonymous")
        await state.audit.emit_action(
            event_type=actions.REGISTRATION_DENIED,
            action="auth.registration.denied",
            result="denied",
            actor=anonymous_actor(),
            object=AuditObject(type="registration", id=payload.username.strip() or None),
            metadata={
                "username": payload.username.strip(),
                "email": payload.email.strip().lower(),
                "source": "web",
                "correlation_id": cid,
            },
            request=request_context,
            process=build_runtime_process_context("web"),
            reason="registration_disabled",
            actor_ip_hash=hash_client_ip(request_context.client_ip),
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
    request_context = build_request_context(request, auth_method="session")
    user = None
    try:
        user = await authenticated_user(request, required=False)
    except HTTPException:
        user = None
    await state.session_backend.clear_session(request.cookies.get(state.settings.session_cookie_name))
    if user is not None:
        await state.audit.emit_action(
            event_type=actions.LOGOUT,
            action="auth.logout",
            result="success",
            actor=user_actor(user),
            object=AuditObject(type="user", id=user.id),
            metadata={"target_user_id": user.id, "source": "web", "correlation_id": cid},
            request=request_context,
            process=build_runtime_process_context("web"),
            actor_ip_hash=hash_client_ip(request_context.client_ip),
        )
    response = JSONResponse({"authenticated": False}, headers={"X-Correlation-ID": cid})
    clear_session_cookie(response, state.settings)
    return response
