from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from ..audit import actions
from ..audit.context import (
    anonymous_actor,
    build_request_context,
    build_runtime_process_context,
    hash_client_ip,
    user_actor,
)
from ..audit.models import AuditObject
from ..oauth import OAuthStatePayload
from ..public_origin import public_base_url
from .auth_context import apply_session_cookie, authenticated_user
from .page_context import login_redirect, normalize_next_path
from .request_context import correlation_id, get_state

router = APIRouter()


def _query_redirect(path: str, **params: str | None) -> RedirectResponse:
    filtered = {key: value for key, value in params.items() if value}
    location = path if not filtered else f"{path}?{urlencode(filtered)}"
    return RedirectResponse(url=location, status_code=303)


def _callback_url(request: Request) -> str:
    state = get_state(request)
    return f"{public_base_url(request, state.settings)}/auth/google/callback"


async def _audit_oauth_denied(request: Request, *, reason: str, object_id: str | None = None) -> None:
    state = get_state(request)
    request_context = build_request_context(request, auth_method="oauth")
    await state.audit.emit_action(
        event_type=actions.OAUTH_DENIED,
        action="oauth.denied",
        result="denied",
        actor=anonymous_actor(),
        object=AuditObject(type="oauth", id=object_id or "google"),
        metadata={"provider": "google", "source": "web", "correlation_id": correlation_id(request)},
        request=request_context,
        process=build_runtime_process_context("web"),
        reason=reason,
        actor_ip_hash=hash_client_ip(request_context.client_ip),
    )


@router.get("/auth/google/start")
async def start_google_oauth(request: Request, next: str | None = None, mode: str = "login") -> RedirectResponse:
    state = get_state(request)
    provider = state.oauth_providers.get("google")
    if provider is None:
        raise HTTPException(status_code=404)
    mode = (mode or "login").strip().lower()
    if mode not in {"login", "link"}:
        mode = "login"
    next_path = normalize_next_path(next, default="/dashboard" if mode == "login" else "/settings")
    user = await authenticated_user(request, required=False)
    if mode == "link" and user is None:
        return login_redirect("/settings")
    signed_state = state.oauth_state.dumps(
        OAuthStatePayload(
            mode=mode,
            next_path=next_path,
            user_id=user.id if mode == "link" and user is not None else None,
        )
    )
    return RedirectResponse(
        url=provider.authorization_url(redirect_uri=_callback_url(request), state=signed_state),
        status_code=303,
    )


@router.get("/auth/google/callback")
async def google_oauth_callback(
    request: Request,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    app_state = get_state(request)
    provider = app_state.oauth_providers.get("google")
    if provider is None:
        raise HTTPException(status_code=404)

    try:
        oauth_state = app_state.oauth_state.loads(state or "")
    except ValueError:
        await _audit_oauth_denied(request, reason="invalid_state")
        return _query_redirect("/login", oauth_error="Google sign-in could not be verified.")

    next_path = normalize_next_path(
        oauth_state.next_path,
        default="/dashboard" if oauth_state.mode == "login" else "/settings",
    )
    current_user = await authenticated_user(request, required=False)
    if oauth_state.mode == "link":
        if current_user is None or current_user.id != oauth_state.user_id:
            await _audit_oauth_denied(request, reason="link_session_mismatch")
            return _query_redirect("/settings", oauth_status="Please sign in again before connecting Google.", oauth_tone="error")
    else:
        current_user = None

    if error:
        await _audit_oauth_denied(request, reason="provider_error")
        target = "/settings" if oauth_state.mode == "link" else "/login"
        key = "oauth_status" if oauth_state.mode == "link" else "oauth_error"
        tone = {"oauth_tone": "error"} if oauth_state.mode == "link" else {}
        return _query_redirect(
            target,
            next=next_path if target == "/login" else None,
            **{key: "Google sign-in was cancelled or denied."},
            **tone,
        )

    if not code:
        await _audit_oauth_denied(request, reason="missing_code")
        target = "/settings" if oauth_state.mode == "link" else "/login"
        key = "oauth_status" if oauth_state.mode == "link" else "oauth_error"
        tone = {"oauth_tone": "error"} if oauth_state.mode == "link" else {}
        return _query_redirect(
            target,
            next=next_path if target == "/login" else None,
            **{key: "Google sign-in did not return a code."},
            **tone,
        )

    try:
        identity = await provider.exchange_code(code=code, redirect_uri=_callback_url(request))
        user, outcome = await app_state.uploads.complete_oauth_login(
            identity,
            current_user=current_user,
            allow_registration=bool(await app_state.runtime_config.get_value("allow_registration")),
            correlation_id=correlation_id(request),
            source="web",
        )
    except HTTPException as exc:
        request_context = build_request_context(request, auth_method="oauth")
        actor = user_actor(current_user) if current_user is not None else anonymous_actor()
        await app_state.audit.emit_action(
            event_type=actions.OAUTH_DENIED,
            action="oauth.denied",
            result="denied",
            actor=actor,
            object=AuditObject(type="oauth", id="google"),
            metadata={"provider": "google", "source": "web", "correlation_id": correlation_id(request)},
            request=request_context,
            process=build_runtime_process_context("web"),
            reason=str(exc.detail),
            actor_ip_hash=hash_client_ip(request_context.client_ip),
        )
        target = "/settings" if oauth_state.mode == "link" else "/login"
        key = "oauth_status" if oauth_state.mode == "link" else "oauth_error"
        tone = {"oauth_tone": "error"} if oauth_state.mode == "link" else {}
        return _query_redirect(target, next=next_path if target == "/login" else None, **{key: str(exc.detail)}, **tone)

    event_type = actions.OAUTH_LINKED if outcome == "linked" else actions.OAUTH_LOGIN
    action = "oauth.linked" if outcome == "linked" else "oauth.login.success"
    request_context = build_request_context(request, auth_method="oauth")
    await app_state.audit.emit_action(
        event_type=event_type,
        action=action,
        result="success",
        actor=user_actor(user),
        object=AuditObject(type="user", id=user.id),
        metadata={
            "provider": "google",
            "provider_uid": identity.provider_uid,
            "outcome": outcome,
            "source": "web",
            "correlation_id": correlation_id(request),
        },
        request=request_context,
        process=build_runtime_process_context("web"),
        actor_ip_hash=hash_client_ip(request_context.client_ip),
    )

    if oauth_state.mode == "link":
        return _query_redirect("/settings", oauth_status="Google account connected.", oauth_tone="success")

    token, expires_at = await app_state.session_backend.create_session(user, remember_me=True)
    response = _query_redirect(next_path)
    apply_session_cookie(response, app_state.settings, token, expires_at=expires_at)
    return response
