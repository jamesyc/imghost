from __future__ import annotations

import logging
from dataclasses import dataclass
from hashlib import sha256

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from ..audit import actions
from ..audit.context import anonymous_actor, build_request_context, build_runtime_process_context, hash_client_ip, user_actor
from ..audit.models import AuditObject
from ..config import Settings
from ..models import User, utcnow
from .page_context import login_redirect
from .request_context import correlation_id, get_state


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


async def authenticated_principal(request: Request, *, required: bool = False) -> ResolvedPrincipal | None:
    state = get_state(request)
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and token:
        request.state.audit_auth_method = "api_key"
        api_key = await state.repository.get_api_key_by_hash(sha256(token.encode("utf-8")).hexdigest())
        if api_key is None:
            request_context = build_request_context(request, auth_method="api_key")
            await state.audit.emit_action(
                event_type=actions.API_KEY_INVALID,
                action="apikey.auth.failed",
                result="denied",
                actor=anonymous_actor(),
                object=AuditObject(type="auth", id="api_key"),
                metadata={"source": "api", "correlation_id": correlation_id(request)},
                request=request_context,
                process=build_runtime_process_context("api"),
                reason="invalid_api_key",
                actor_ip_hash=hash_client_ip(request_context.client_ip),
            )
            raise HTTPException(status_code=401, detail="Invalid API key.")
        user = await state.repository.get_user(api_key.user_id)
        if user is None or user.suspended:
            request_context = build_request_context(request, auth_method="api_key")
            await state.audit.emit_action(
                event_type=actions.ADMIN_ACCESS_DENIED if user is not None and user.is_admin else actions.API_KEY_INVALID,
                action="apikey.auth.failed",
                result="denied",
                actor=user_actor(user) if user is not None else anonymous_actor(),
                object=AuditObject(type="user", id=api_key.user_id),
                metadata={"source": "api", "correlation_id": correlation_id(request)},
                request=request_context,
                process=build_runtime_process_context("api"),
                reason="suspended" if user is not None and user.suspended else "missing_user",
                actor_ip_hash=hash_client_ip(request_context.client_ip),
            )
            raise HTTPException(status_code=403, detail="User is not allowed to authenticate.")
        api_key.last_used_at = utcnow()
        await state.repository.update_api_key(api_key)
        request_context = build_request_context(request, auth_method="api_key")
        await state.audit.emit_action(
            event_type=actions.API_KEY_AUTHENTICATED,
            action="apikey.auth.success",
            result="success",
            actor=user_actor(user),
            object=AuditObject(type="user", id=user.id),
            metadata={"api_key_id": api_key.id, "source": "api", "correlation_id": correlation_id(request)},
            request=request_context,
            process=build_runtime_process_context("api"),
            actor_ip_hash=hash_client_ip(request_context.client_ip),
        )
        return ResolvedPrincipal(user=user, raw_api_key=token)

    session_token = request.cookies.get(state.settings.session_cookie_name)
    if session_token:
        request.state.audit_auth_method = "session"
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
    try:
        user = await authenticated_user(request, required=True)
    except HTTPException as exc:
        if exc.status_code in {401, 403}:
            request_context = build_request_context(request)
            await state.audit.emit_action(
                event_type=actions.ADMIN_ACCESS_DENIED,
                action="auth.admin.denied",
                result="denied",
                actor=anonymous_actor(),
                object=AuditObject(type="admin", id=request.url.path),
                metadata={"source": "api", "correlation_id": correlation_id(request)},
                request=request_context,
                process=build_runtime_process_context("api"),
                reason="authentication_required" if exc.status_code == 401 else "forbidden",
                actor_ip_hash=hash_client_ip(request_context.client_ip),
            )
        raise
    if user is None or not user.is_admin:
        request_context = build_request_context(request)
        await state.audit.emit_action(
            event_type=actions.ADMIN_ACCESS_DENIED,
            action="auth.admin.denied",
            result="denied",
            actor=user_actor(user) if user is not None else anonymous_actor(),
            object=AuditObject(type="admin", id=request.url.path),
            metadata={"source": "api", "correlation_id": correlation_id(request)},
            request=request_context,
            process=build_runtime_process_context("api"),
            reason="admin_required",
            actor_ip_hash=hash_client_ip(request_context.client_ip),
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
    user = await authenticated_user(request, required=False)
    if user is None:
        request_context = build_request_context(request)
        await state.audit.emit_action(
            event_type=actions.ADMIN_ACCESS_DENIED,
            action="auth.admin.denied",
            result="denied",
            actor=anonymous_actor(),
            object=AuditObject(type="admin_page", id=request.url.path),
            metadata={"source": "web", "correlation_id": correlation_id(request)},
            request=request_context,
            process=build_runtime_process_context("web"),
            reason="authentication_required",
            actor_ip_hash=hash_client_ip(request_context.client_ip),
        )
        return login_redirect(str(request.url.path))
    if not user.is_admin:
        request_context = build_request_context(request)
        await state.audit.emit_action(
            event_type=actions.ADMIN_ACCESS_DENIED,
            action="auth.admin.denied",
            result="denied",
            actor=user_actor(user),
            object=AuditObject(type="admin_page", id=request.url.path),
            metadata={"source": "web", "correlation_id": correlation_id(request)},
            request=request_context,
            process=build_runtime_process_context("web"),
            reason="admin_required",
            actor_ip_hash=hash_client_ip(request_context.client_ip),
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
