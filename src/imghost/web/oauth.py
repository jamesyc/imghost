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


def _provider_label(provider: str) -> str:
    normalized = provider.strip().lower()
    return {
        "google": "Google",
        "github": "GitHub",
    }.get(normalized, normalized.capitalize() or "OAuth")


def _callback_url(request: Request, provider: str) -> str:
    state = get_state(request)
    return f"{public_base_url(request, state.settings)}/auth/{provider}/callback"


def _oauth_redirect_message(provider: str, suffix: str) -> str:
    return f"{_provider_label(provider)} {suffix}"


async def _audit_oauth_denied(request: Request, *, reason: str, object_id: str | None = None) -> None:
    state = get_state(request)
    await state.telemetry.record_oauth_denied(request, reason=reason, object_id=object_id)


async def _start_oauth(request: Request, provider_name: str, *, next: str | None = None, mode: str = "login") -> RedirectResponse:
    state = get_state(request)
    provider = state.oauth_providers.get(provider_name)
    if provider is None:
        raise HTTPException(status_code=404)
    mode = (mode or "login").strip().lower()
    if mode not in {"login", "link", "delete_account"}:
        mode = "login"
    next_path = normalize_next_path(next, default="/dashboard" if mode == "login" else "/settings")
    user = await authenticated_user(request, required=False)
    if mode in {"link", "delete_account"} and user is None:
        return login_redirect("/settings")
    await state.repository.delete_expired_oauth_state_nonces()
    code_verifier = generate_code_verifier()
    nonce = await state.repository.create_oauth_state_nonce(
        OAuthStateNonce(
            jti=str(uuid4()),
            mode=mode,
            user_id=user.id if mode in {"link", "delete_account"} and user is not None else None,
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
            user_id=user.id if mode in {"link", "delete_account"} and user is not None else None,
        )
    )
    return RedirectResponse(
        url=provider.authorization_url(
            redirect_uri=_callback_url(request, provider_name),
            state=signed_state,
            code_challenge=build_code_challenge(code_verifier),
            code_challenge_method="S256",
        ),
        status_code=303,
    )


@router.get("/auth/google/start")
async def start_google_oauth(request: Request, next: str | None = None, mode: str = "login") -> RedirectResponse:
    return await _start_oauth(request, "google", next=next, mode=mode)


@router.get("/auth/{provider}/start")
async def start_oauth(request: Request, provider: str, next: str | None = None, mode: str = "login") -> RedirectResponse:
    return await _start_oauth(request, provider.strip().lower(), next=next, mode=mode)


async def _oauth_callback(
    request: Request,
    provider_name: str,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    app_state = get_state(request)
    provider = app_state.oauth_providers.get(provider_name)
    if provider is None:
        raise HTTPException(status_code=404)

    try:
        oauth_state = app_state.oauth_state.loads(state or "")
    except ValueError:
        await _audit_oauth_denied(request, reason="invalid_state")
        return _query_redirect("/login", oauth_error=_oauth_redirect_message(provider_name, "sign-in could not be verified."))
    if not oauth_state.jti:
        await _audit_oauth_denied(request, reason="invalid_state")
        return _query_redirect("/login", oauth_error=_oauth_redirect_message(provider_name, "sign-in could not be verified."))
    nonce = await app_state.repository.consume_oauth_state_nonce(oauth_state.jti)
    if nonce is None:
        await _audit_oauth_denied(request, reason="invalid_state_nonce")
        return _query_redirect("/login", oauth_error=_oauth_redirect_message(provider_name, "sign-in could not be verified."))

    next_path = normalize_next_path(
        oauth_state.next_path,
        default="/dashboard" if oauth_state.mode == "login" else "/settings",
    )
    current_user = await authenticated_user(request, required=False)
    if oauth_state.mode in {"link", "delete_account"}:
        if current_user is None or current_user.id != oauth_state.user_id:
            await _audit_oauth_denied(
                request,
                reason="link_session_mismatch" if oauth_state.mode == "link" else "delete_account_session_mismatch",
            )
            message = (
                f"Please sign in again before connecting {_provider_label(provider_name)}."
                if oauth_state.mode == "link"
                else f"Please sign in again before confirming account deletion with {_provider_label(provider_name)}."
            )
            key = "oauth_status" if oauth_state.mode == "link" else "delete_reauth_status"
            tone = {"oauth_tone": "error"} if oauth_state.mode == "link" else {"delete_reauth_tone": "error"}
            return _query_redirect("/settings", **{key: message}, **tone)
    if oauth_state.mode == "login":
        current_user = None

    if error:
        await _audit_oauth_denied(request, reason="provider_error")
        target = "/settings" if oauth_state.mode in {"link", "delete_account"} else "/login"
        key = "oauth_status" if oauth_state.mode == "link" else "delete_reauth_status" if oauth_state.mode == "delete_account" else "oauth_error"
        tone = {"oauth_tone": "error"} if oauth_state.mode == "link" else {"delete_reauth_tone": "error"} if oauth_state.mode == "delete_account" else {}
        return _query_redirect(
            target,
            next=next_path if target == "/login" else None,
            **{key: _oauth_redirect_message(provider_name, "sign-in was cancelled or denied.")},
            **tone,
        )

    if not code:
        await _audit_oauth_denied(request, reason="missing_code")
        target = "/settings" if oauth_state.mode in {"link", "delete_account"} else "/login"
        key = "oauth_status" if oauth_state.mode == "link" else "delete_reauth_status" if oauth_state.mode == "delete_account" else "oauth_error"
        tone = {"oauth_tone": "error"} if oauth_state.mode == "link" else {"delete_reauth_tone": "error"} if oauth_state.mode == "delete_account" else {}
        return _query_redirect(
            target,
            next=next_path if target == "/login" else None,
            **{key: _oauth_redirect_message(provider_name, "sign-in did not return a code.")},
            **tone,
        )

    try:
        identity = await provider.exchange_code(
            code=code,
            redirect_uri=_callback_url(request, provider_name),
            code_verifier=nonce.code_verifier,
        )
        if oauth_state.mode == "delete_account":
            assert current_user is not None
            reauth_token = await app_state.uploads.issue_account_delete_reauth_token(
                current_user,
                provider=identity.provider,
                provider_uid=identity.provider_uid,
            )
            await app_state.telemetry.record_oauth_succeeded(
                request,
                user=current_user,
                provider=provider_name,
                provider_uid=identity.provider_uid,
                outcome="delete_account_reauth",
            )
            return _query_redirect(
                "/settings",
                delete_reauth_status=f"{_provider_label(provider_name)} re-authentication confirmed. You can now delete your account.",
                delete_reauth_tone="success",
                delete_reauth_token=reauth_token,
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
            object_id=provider_name,
        )
        target = "/settings" if oauth_state.mode in {"link", "delete_account"} else "/login"
        key = "oauth_status" if oauth_state.mode == "link" else "delete_reauth_status" if oauth_state.mode == "delete_account" else "oauth_error"
        tone = {"oauth_tone": "error"} if oauth_state.mode == "link" else {"delete_reauth_tone": "error"} if oauth_state.mode == "delete_account" else {}
        return _query_redirect(target, next=next_path if target == "/login" else None, **{key: str(exc.detail)}, **tone)

    await app_state.telemetry.record_oauth_succeeded(
        request,
        user=user,
        provider=provider_name,
        provider_uid=identity.provider_uid,
        outcome=outcome,
    )

    if oauth_state.mode == "link":
        return _query_redirect("/settings", oauth_status=f"{_provider_label(provider_name)} account connected.", oauth_tone="success")

    try:
        token, expires_at = await app_state.session_backend.create_session(user, remember_me=True)
    except SessionBackendUnavailable:
        await app_state.telemetry.record_oauth_denied(
            request,
            reason="session_unavailable",
            actor=user,
            object_id=provider_name,
        )
        return _query_redirect("/login", oauth_error=_oauth_redirect_message(provider_name, "sign-in is temporarily unavailable."))
    response = _query_redirect(next_path)
    apply_session_cookie(response, app_state.settings, token, expires_at=expires_at)
    return response


@router.get("/auth/google/callback")
async def google_oauth_callback(
    request: Request,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    return await _oauth_callback(request, "google", state=state, code=code, error=error)


@router.get("/auth/{provider}/callback")
async def oauth_callback(
    request: Request,
    provider: str,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    return await _oauth_callback(request, provider.strip().lower(), state=state, code=code, error=error)
