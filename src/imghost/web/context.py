from __future__ import annotations

import logging
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..app_state import AppState
from ..config import Settings
from ..models import User, utcnow
from ..public_origin import _normalize_origin, _trusted_origin_set, public_base_url

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@dataclass
class ResolvedPrincipal:
    user: User
    raw_api_key: str | None = None


@dataclass
class PageRuntimeFlags:
    allow_registration: bool
    anon_upload_enabled: bool
    anon_expiry_hours: int
    max_upload_bytes: int


def get_state(request: Request) -> AppState:
    return request.app.state.imghost


def correlation_id(request: Request) -> str:
    return request.headers.get("X-Correlation-ID") or str(uuid4())


def _request_uses_bearer_auth(request: Request) -> bool:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    return scheme.lower() == "bearer" and bool(token)


def _referer_origin(referer: str | None) -> str | None:
    candidate = (referer or "").strip()
    if not candidate:
        return None
    parsed = urlsplit(candidate)
    if not parsed.scheme or not parsed.netloc:
        return None
    return _normalize_origin(f"{parsed.scheme}://{parsed.netloc}")


def _has_trusted_csrf_source(request: Request, settings: Settings) -> bool:
    trusted = _trusted_origin_set(settings)
    origin = _normalize_origin(request.headers.get("Origin", ""))
    if origin is not None:
        return origin in trusted
    referer_origin = _referer_origin(request.headers.get("Referer"))
    if referer_origin is not None:
        return referer_origin in trusted
    return False


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
        api_key = await state.repository.get_api_key_by_hash(sha256(token.encode("utf-8")).hexdigest())
        if api_key is None:
            raise HTTPException(status_code=401, detail="Invalid API key.")
        user = await state.repository.get_user(api_key.user_id)
        if user is None or user.suspended:
            raise HTTPException(status_code=403, detail="User is not allowed to authenticate.")
        api_key.last_used_at = utcnow()
        await state.repository.update_api_key(api_key)
        return ResolvedPrincipal(user=user, raw_api_key=token)

    session_token = request.cookies.get(state.settings.session_cookie_name)
    if session_token:
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
    user = await authenticated_user(request, required=True)
    if user is None or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


def nav_items(user: User | None, *, allow_registration: bool) -> list[dict[str, str]]:
    links: list[tuple[str, str]] = []
    if user is None:
        links.append(("/login", "Login"))
        if allow_registration:
            links.append(("/register", "Register"))
    else:
        links.extend(
            [
                ("/dashboard", "Dashboard"),
                ("/albums", "Albums"),
                ("/settings", "Settings"),
            ]
        )
        if user.is_admin:
            links.append(("/admin", "Admin"))
    return [{"href": href, "label": label} for href, label in links]


async def runtime_flags(request: Request) -> PageRuntimeFlags:
    state = get_state(request)
    return PageRuntimeFlags(
        allow_registration=bool(await state.runtime_config.get_value("allow_registration")),
        anon_upload_enabled=bool(await state.runtime_config.get_value("anon_upload_enabled")),
        anon_expiry_hours=int(await state.runtime_config.get_value("anon_expiry_hours")),
        max_upload_bytes=state.settings.max_upload_bytes,
    )


async def build_page_context(request: Request, *, user: User | None = None) -> dict[str, Any]:
    flags = await runtime_flags(request)
    return {
        "current_user": user,
        "nav_items": nav_items(user, allow_registration=flags.allow_registration),
        "public_base_url": public_base_url(request, get_state(request).settings),
        "runtime_flags": flags,
    }


def login_redirect(next_path: str | None = None) -> RedirectResponse:
    location = "/login"
    if next_path:
        location = f"{location}?{urlencode({'next': next_path})}"
    return RedirectResponse(url=location, status_code=303)


def normalize_next_path(next_path: str | None, *, default: str = "/dashboard") -> str:
    candidate = (next_path or "").strip()
    if not candidate:
        return default
    if not candidate.startswith("/"):
        return default
    if candidate.startswith("//"):
        return default
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return default
    return candidate


async def require_page_user(request: Request) -> User | RedirectResponse:
    user = await authenticated_user(request, required=False)
    if user is None:
        return login_redirect(str(request.url.path))
    return user


async def require_page_admin(request: Request) -> User | RedirectResponse:
    user = await authenticated_user(request, required=False)
    if user is None:
        return login_redirect(str(request.url.path))
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user


async def render_template_page(
    request: Request,
    template_name: str,
    title: str,
    *,
    user: User | None = None,
    extra_context: dict[str, Any] | None = None,
    script_paths: list[str] | None = None,
) -> HTMLResponse:
    context = await build_page_context(request, user=user)
    if extra_context:
        context.update(extra_context)
    context["page_title"] = title
    context["script_paths"] = script_paths or []
    return templates.TemplateResponse(request=request, name=template_name, context=context)


async def clear_stale_session_cookie(request: Request, call_next):
    request.state.clear_session_cookie = False
    response = await call_next(request)
    if getattr(request.state, "clear_session_cookie", False):
        clear_session_cookie(response, get_state(request).settings)
        logging.getLogger(__name__).info("session_cookie_cleared", extra={"path": request.url.path})
    return response


async def enforce_session_csrf(request: Request, call_next):
    if request.method not in {"POST", "PATCH", "DELETE"}:
        return await call_next(request)
    if request.url.path in {"/api/v1/auth/login", "/api/v1/auth/register"}:
        return await call_next(request)
    state = getattr(request.app.state, "imghost", None)
    if state is None:
        return await call_next(request)
    if _request_uses_bearer_auth(request):
        return await call_next(request)
    if not request.cookies.get(state.settings.session_cookie_name):
        return await call_next(request)
    if not _has_trusted_csrf_source(request, state.settings):
        return JSONResponse({"detail": "CSRF protection blocked the request."}, status_code=403)
    return await call_next(request)
