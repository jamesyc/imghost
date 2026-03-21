from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..ids import ALBUM_ID_LENGTH, is_valid_id
from ..payloads import album_to_payload, compatibility_warning
from ..public_origin import public_base_url
from .context import (
    authenticated_user,
    get_state,
    normalize_next_path,
    render_template_page,
    require_page_admin,
    require_page_user,
    runtime_flags,
)
from .utils import display_timestamp, humanize_bytes, humanize_expiry, is_expired

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> str:
    user = await authenticated_user(request, required=False)
    flags = await runtime_flags(request)
    return await render_template_page(
        request,
        "pages/home.html",
        "imghost",
        user=user,
        extra_context={
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
        extra_context={"next_path": next_path},
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
        extra_context={"next_path": next_path},
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
            "workspace_bootstrap": {
                "album_id": album_id,
                "access_mode": "owner",
                "workspace_label": "Owner view",
                "post_delete_url": "/albums",
                "delete_token": None,
            }
        },
        script_paths=["js/upload-box.js", "js/album-detail.js"],
    )


@router.get("/manage/{album_id}", response_class=HTMLResponse)
async def manage_album_page(request: Request, album_id: str, token: str | None = None) -> HTMLResponse:
    if not is_valid_id(album_id, ALBUM_ID_LENGTH):
        raise HTTPException(status_code=404)
    if not token:
        raise HTTPException(status_code=403, detail="Missing manage token.")
    state = get_state(request)
    viewer = await authenticated_user(request, required=False)
    album = await state.repository.get_album(album_id)
    if album is None or is_expired(album.expires_at) or album.delete_token is None:
        raise HTTPException(status_code=404)
    if token != album.delete_token:
        raise HTTPException(status_code=403, detail="Invalid manage token.")
    return await render_template_page(
        request,
        "pages/album-detail.html",
        "Manage Album",
        user=viewer,
        extra_context={
            "workspace_bootstrap": {
                "album_id": album_id,
                "access_mode": "token",
                "workspace_label": "Manage view",
                "post_delete_url": "/",
                "delete_token": token,
            }
        },
        script_paths=["js/upload-box.js", "js/album-detail.js"],
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
        extra_context={"session_user": session_user},
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
    return await render_template_page(
        request,
        "pages/admin-users-new.html",
        "Admin New User",
        user=user,
        script_paths=["js/admin-common.js", "js/admin-users-new.js"],
    )


@router.get("/admin/albums", response_class=HTMLResponse)
async def admin_albums_page(request: Request) -> HTMLResponse:
    user_or_redirect = await require_page_admin(request)
    if isinstance(user_or_redirect, RedirectResponse):
        return user_or_redirect
    user = user_or_redirect
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
    album_payload = album_to_payload(public_base_url(request, state.settings), album, items)
    album_payload["total_size_display"] = humanize_bytes(int(album_payload["total_size"]))
    album_payload["updated_at_display"] = display_timestamp(album_payload["updated_at"])
    for item in album_payload["items"]:
        item["file_size_display"] = humanize_bytes(int(item["file_size"]))
    return await render_template_page(
        request,
        "pages/public-album.html",
        album.title or "Untitled album",
        user=user,
        extra_context={
            "album_payload": album_payload,
            "expiry_hint": humanize_expiry(album.expires_at),
            "compat_warnings": [warning for warning in dict.fromkeys(compatibility_warning(item) for item in items) if warning],
            "is_owner_viewer": user is not None and album.user_id == user.id,
        },
        script_paths=["js/public-album.js"],
    )


@router.get("/u/{username}", response_class=HTMLResponse)
async def user_album_list_page(request: Request, username: str) -> HTMLResponse:
    state = get_state(request)
    viewer = await authenticated_user(request, required=False)
    user, albums = await state.uploads.list_public_albums_for_username(username)
    for album in albums:
        album["total_size_display"] = humanize_bytes(int(album["total_size"]))
        album["created_at_display"] = display_timestamp(str(album["created_at"]))
    return await render_template_page(
        request,
        "pages/public-user-albums.html",
        f"{user.username} albums",
        user=viewer,
        extra_context={
            "public_user": user,
            "public_albums": albums,
        },
    )
