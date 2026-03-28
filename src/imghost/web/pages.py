from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..ids import ALBUM_ID_LENGTH, is_valid_id
from ..public_origin import public_base_url
from .auth_context import (
    authenticated_user,
    require_page_admin,
    require_page_user,
)
from .display_helpers import is_expired
from .page_context import normalize_next_path, render_template_page, runtime_flags
from .page_views import (
    build_open_graph,
    build_public_album_page_context,
    build_public_user_album_list_context,
    build_workspace_bootstrap,
)
from .request_context import get_state

router = APIRouter()

PWA_THEME_COLOR = "#10233f"
PWA_BACKGROUND_COLOR = "#edf4ff"


async def _audit_admin_page_view(request: Request, user, page_name: str, *, object_id: str | None = None) -> None:
    state = get_state(request)
    await state.telemetry.record_admin_page_viewed(request, user=user, page_name=page_name, object_id=object_id)


@router.get("/manifest.webmanifest")
async def manifest(request: Request) -> Response:
    base_url = public_base_url(request, get_state(request).settings)
    payload = {
        "id": "/",
        "name": "ImgHost",
        "short_name": "ImgHost",
        "description": "Upload your photos and videos. Share them in seconds.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": PWA_BACKGROUND_COLOR,
        "theme_color": PWA_THEME_COLOR,
        "icons": [
            {
                "src": f"{base_url}/static/icons/icon-192.png",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": f"{base_url}/static/icons/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable",
            },
        ],
    }
    return Response(
        content=json.dumps(payload),
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/service-worker.js")
async def service_worker() -> Response:
    script = """self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});"""
    return Response(
        content=script,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> str:
    user = await authenticated_user(request, required=False)
    flags = await runtime_flags(request)
    base_url = public_base_url(request, get_state(request).settings)
    return await render_template_page(
        request,
        "pages/home.html",
        "imghost",
        user=user,
        extra_context={
            "open_graph": build_open_graph(
                title="imghost",
                description="Upload your photos and videos. Share them in seconds.",
                url=base_url,
            ),
            "upload_enabled": user is not None or flags.anon_upload_enabled,
        },
        script_paths=["js/upload-box.js", "js/home.js"],
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    user = await authenticated_user(request, required=False)
    if user is not None:
        return RedirectResponse(url="/dashboard", status_code=303)
    next_path = normalize_next_path(request.query_params.get("next"))
    return await render_template_page(
        request,
        "pages/login.html",
        "Login",
        extra_context={
            "next_path": next_path,
            "oauth_error": (request.query_params.get("oauth_error") or "").strip() or None,
        },
        script_paths=["js/auth.js"],
    )


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request) -> HTMLResponse:
    user = await authenticated_user(request, required=False)
    if user is not None:
        return RedirectResponse(url="/dashboard", status_code=303)
    next_path = normalize_next_path(request.query_params.get("next"))
    return await render_template_page(
        request,
        "pages/register.html",
        "Register",
        extra_context={
            "next_path": next_path,
            "oauth_error": (request.query_params.get("oauth_error") or "").strip() or None,
        },
        script_paths=["js/auth.js"],
    )


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request) -> HTMLResponse:
    state = get_state(request)
    user_or_redirect = await require_page_user(request)
    if isinstance(user_or_redirect, RedirectResponse):
        return user_or_redirect
    user = user_or_redirect
    session_user = await state.uploads.get_current_user_summary(user)
    return await render_template_page(
        request,
        "pages/dashboard.html",
        "Dashboard",
        user=user,
        extra_context={"session_user": session_user},
        script_paths=["js/upload-box.js", "js/album-cards.js", "js/dashboard.js"],
    )


@router.get("/albums", response_class=HTMLResponse)
async def albums_page(request: Request) -> HTMLResponse:
    user_or_redirect = await require_page_user(request)
    if isinstance(user_or_redirect, RedirectResponse):
        return user_or_redirect
    user = user_or_redirect
    return await render_template_page(
        request,
        "pages/albums.html",
        "Albums",
        user=user,
        script_paths=["js/album-cards.js", "js/albums.js"],
    )


@router.get("/albums/{album_id}", response_class=HTMLResponse)
async def album_detail_page(request: Request, album_id: str) -> HTMLResponse:
    if not is_valid_id(album_id, ALBUM_ID_LENGTH):
        raise HTTPException(status_code=404)
    state = get_state(request)
    user_or_redirect = await require_page_user(request)
    if isinstance(user_or_redirect, RedirectResponse):
        return user_or_redirect
    user = user_or_redirect
    album = await state.repository.get_album(album_id)
    if album is None or is_expired(album.expires_at) or album.user_id != user.id:
        raise HTTPException(status_code=404)
    return await render_template_page(
        request,
        "pages/album-detail.html",
        "Album",
        user=user,
        extra_context={
            "workspace_bootstrap": build_workspace_bootstrap(
                album_id,
                access_mode="owner",
                post_delete_url="/albums",
                delete_token=None,
            )
        },
        script_paths=["js/upload-box.js", "js/album-detail-core.js", "js/album-detail-render.js", "js/album-detail-actions.js", "js/album-detail.js"],
    )


@router.get("/manage/{album_id}", response_class=HTMLResponse)
async def manage_album_page(request: Request, album_id: str, token: str | None = None) -> HTMLResponse:
    if not is_valid_id(album_id, ALBUM_ID_LENGTH):
        raise HTTPException(status_code=404)
    active_token = token or request.cookies.get(f"imghost_manage_{album_id}")
    if not active_token:
        raise HTTPException(status_code=403, detail="Missing manage token.")
    state = get_state(request)
    viewer = await authenticated_user(request, required=False)
    album = await state.repository.get_album(album_id)
    if album is None or is_expired(album.expires_at) or album.delete_token is None:
        raise HTTPException(status_code=404)
    if active_token != album.delete_token:
        raise HTTPException(status_code=403, detail="Invalid manage token.")
    return await render_template_page(
        request,
        "pages/album-detail.html",
        "Manage Album",
        user=viewer,
        extra_context={
            "workspace_bootstrap": build_workspace_bootstrap(
                album_id,
                access_mode="token",
                post_delete_url="/",
                delete_token=active_token,
            )
        },
        script_paths=["js/upload-box.js", "js/album-detail-core.js", "js/album-detail-render.js", "js/album-detail-actions.js", "js/album-detail.js"],
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> HTMLResponse:
    state = get_state(request)
    user_or_redirect = await require_page_user(request)
    if isinstance(user_or_redirect, RedirectResponse):
        return user_or_redirect
    user = user_or_redirect
    session_user = await state.uploads.get_current_user_summary(user)
    return await render_template_page(
        request,
        "pages/settings.html",
        "Settings",
        user=user,
        extra_context={
            "session_user": session_user,
            "oauth_status": (request.query_params.get("oauth_status") or "").strip() or None,
            "oauth_tone": (request.query_params.get("oauth_tone") or "").strip() or None,
            "delete_reauth_token": (request.query_params.get("delete_reauth_token") or "").strip() or None,
            "delete_reauth_status": (request.query_params.get("delete_reauth_status") or "").strip() or None,
            "delete_reauth_tone": (request.query_params.get("delete_reauth_tone") or "").strip() or None,
        },
        script_paths=["js/settings.js"],
    )


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request) -> HTMLResponse:
    state = get_state(request)
    user_or_redirect = await require_page_admin(request)
    if isinstance(user_or_redirect, RedirectResponse):
        return user_or_redirect
    user = user_or_redirect
    session_user = await state.uploads.get_current_user_summary(user)
    await _audit_admin_page_view(request, user, "admin.overview")
    return await render_template_page(
        request,
        "pages/admin.html",
        "Admin",
        user=user,
        extra_context={"session_user": session_user},
        script_paths=["js/admin-common.js", "js/admin-index.js"],
    )


@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(request: Request) -> HTMLResponse:
    user_or_redirect = await require_page_admin(request)
    if isinstance(user_or_redirect, RedirectResponse):
        return user_or_redirect
    user = user_or_redirect
    await _audit_admin_page_view(request, user, "admin.users")
    return await render_template_page(
        request,
        "pages/admin-users.html",
        "Admin Users",
        user=user,
        script_paths=["js/admin-common.js", "js/admin-users.js"],
    )


@router.get("/admin/users/new", response_class=HTMLResponse)
async def admin_users_new_page(request: Request) -> HTMLResponse:
    user_or_redirect = await require_page_admin(request)
    if isinstance(user_or_redirect, RedirectResponse):
        return user_or_redirect
    user = user_or_redirect
    await _audit_admin_page_view(request, user, "admin.users_new")
    return await render_template_page(
        request,
        "pages/admin-users-new.html",
        "Admin New User",
        user=user,
        script_paths=["js/admin-common.js", "js/admin-users-new.js"],
    )


@router.get("/admin/users/{user_id}", response_class=HTMLResponse)
async def admin_user_detail_page(request: Request, user_id: str) -> HTMLResponse:
    state = get_state(request)
    user_or_redirect = await require_page_admin(request)
    if isinstance(user_or_redirect, RedirectResponse):
        return user_or_redirect
    user = user_or_redirect
    admin_user = await state.uploads.get_user_with_usage_for_admin(user_id)
    await _audit_admin_page_view(request, user, "admin.user_detail", object_id=user_id)
    return await render_template_page(
        request,
        "pages/admin-user-detail.html",
        f"Admin User {admin_user['username']}",
        user=user,
        extra_context={"admin_user_bootstrap": {"user_id": user_id, "username": admin_user["username"]}},
        script_paths=["js/admin-common.js", "js/album-cards.js", "js/admin-user-detail.js"],
    )


@router.get("/admin/albums", response_class=HTMLResponse)
async def admin_albums_page(request: Request) -> HTMLResponse:
    user_or_redirect = await require_page_admin(request)
    if isinstance(user_or_redirect, RedirectResponse):
        return user_or_redirect
    user = user_or_redirect
    await _audit_admin_page_view(request, user, "admin.albums")
    return await render_template_page(
        request,
        "pages/admin-albums.html",
        "Admin Albums",
        user=user,
        script_paths=["js/admin-common.js", "js/admin-albums.js"],
    )


@router.get("/admin/config", response_class=HTMLResponse)
async def admin_config_page(request: Request) -> HTMLResponse:
    user_or_redirect = await require_page_admin(request)
    if isinstance(user_or_redirect, RedirectResponse):
        return user_or_redirect
    user = user_or_redirect
    await _audit_admin_page_view(request, user, "admin.config")
    return await render_template_page(
        request,
        "pages/admin-config.html",
        "Admin Config",
        user=user,
        script_paths=["js/admin-common.js", "js/admin-config.js"],
    )


@router.get("/admin/ops", response_class=HTMLResponse)
async def admin_ops_page(request: Request) -> HTMLResponse:
    user_or_redirect = await require_page_admin(request)
    if isinstance(user_or_redirect, RedirectResponse):
        return user_or_redirect
    user = user_or_redirect
    await _audit_admin_page_view(request, user, "admin.ops")
    return await render_template_page(
        request,
        "pages/admin-ops.html",
        "Admin Ops",
        user=user,
        script_paths=["js/admin-common.js", "js/admin-ops.js"],
    )


@router.get("/a/{album_id}", response_class=HTMLResponse)
async def album_page(request: Request, album_id: str) -> HTMLResponse:
    if not is_valid_id(album_id, ALBUM_ID_LENGTH):
        raise HTTPException(status_code=404)
    state = get_state(request)
    user = await authenticated_user(request, required=False)
    album = await state.repository.get_album(album_id)
    if album is None or is_expired(album.expires_at):
        raise HTTPException(status_code=404)
    items = await state.repository.list_album_media(album_id)
    base_url = public_base_url(request, state.settings)
    return await render_template_page(
        request,
        "pages/public-album.html",
        album.title or "Untitled album",
        user=user,
        extra_context=build_public_album_page_context(
            base_url,
            album,
            items,
            viewer_user_id=user.id if user is not None else None,
        ),
        script_paths=["js/public-album.js"],
    )


@router.get("/u/{username}", response_class=HTMLResponse)
async def user_album_list_page(request: Request, username: str) -> HTMLResponse:
    state = get_state(request)
    viewer = await authenticated_user(request, required=False)
    user, albums = await state.uploads.list_public_albums_for_username(username)
    base_url = public_base_url(request, state.settings)
    return await render_template_page(
        request,
        "pages/public-user-albums.html",
        f"{user.username} albums",
        user=viewer,
        extra_context={
            "public_user": user,
            **build_public_user_album_list_context(base_url, user.username, albums),
        },
        script_paths=["js/album-cards.js", "js/public-user-albums.js"],
    )
