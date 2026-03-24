from __future__ import annotations

from datetime import timedelta
from uuid import uuid4
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from ..oauth import OAuthStatePayload, build_code_challenge, generate_code_verifier
from ..public_origin import public_base_url
from ..sessions import SessionBackendUnavailable
from ..models import OAuthStateNonce, utcnow
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
    await state.telemetry.record_oauth_denied(request, reason=reason, object_id=object_id)


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
    await state.repository.delete_expired_oauth_state_nonces()
    code_verifier = generate_code_verifier()
    nonce = await state.repository.create_oauth_state_nonce(
        OAuthStateNonce(
            jti=str(uuid4()),
            mode=mode,
            user_id=user.id if mode == "link" and user is not None else None,
            code_verifier=code_verifier,
            created_at=utcnow(),
            expires_at=utcnow() + timedelta(minutes=10),
        )
    )
    signed_state = state.oauth_state.dumps(
        OAuthStatePayload(
            mode=mode,
            next_path=next_path,
            jti=nonce.jti,
            user_id=user.id if mode == "link" and user is not None else None,
        )
    )
    return RedirectResponse(
        url=provider.authorization_url(
            redirect_uri=_callback_url(request),
            state=signed_state,
            code_challenge=build_code_challenge(code_verifier),
            code_challenge_method="S256",
        ),
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
    if not oauth_state.jti:
        await _audit_oauth_denied(request, reason="invalid_state")
        return _query_redirect("/login", oauth_error="Google sign-in could not be verified.")
    nonce = await app_state.repository.consume_oauth_state_nonce(oauth_state.jti)
    if nonce is None:
        await _audit_oauth_denied(request, reason="invalid_state_nonce")
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
        identity = await provider.exchange_code(
            code=code,
            redirect_uri=_callback_url(request),
            code_verifier=nonce.code_verifier,
        )
        user, outcome = await app_state.uploads.complete_oauth_login(
            identity,
            current_user=current_user,
            allow_registration=bool(await app_state.runtime_config.get_value("allow_registration")),
            correlation_id=correlation_id(request),
            source="web",
        )
    except HTTPException as exc:
        await app_state.telemetry.record_oauth_denied(
            request,
            reason=str(exc.detail),
            actor=current_user,
            object_id="google",
        )
        target = "/settings" if oauth_state.mode == "link" else "/login"
        key = "oauth_status" if oauth_state.mode == "link" else "oauth_error"
        tone = {"oauth_tone": "error"} if oauth_state.mode == "link" else {}
        return _query_redirect(target, next=next_path if target == "/login" else None, **{key: str(exc.detail)}, **tone)

    await app_state.telemetry.record_oauth_succeeded(
        request,
        user=user,
        provider="google",
        provider_uid=identity.provider_uid,
        outcome=outcome,
    )

    if oauth_state.mode == "link":
        return _query_redirect("/settings", oauth_status="Google account connected.", oauth_tone="success")

    try:
        token, expires_at = await app_state.session_backend.create_session(user, remember_me=True)
    except SessionBackendUnavailable:
        await app_state.telemetry.record_oauth_denied(
            request,
            reason="session_unavailable",
            actor=user,
            object_id="google",
        )
        return _query_redirect("/login", oauth_error="Google sign-in is temporarily unavailable.")
    response = _query_redirect(next_path)
    apply_session_cookie(response, app_state.settings, token, expires_at=expires_at)
    return response
