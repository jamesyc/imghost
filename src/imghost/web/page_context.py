from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..models import User
from ..public_origin import public_base_url
from .request_context import get_state

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@dataclass
class PageRuntimeFlags:
    allow_registration: bool
    anon_upload_enabled: bool
    anon_expiry_hours: int
    max_upload_bytes: int


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
