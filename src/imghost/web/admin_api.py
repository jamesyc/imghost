from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ..events import ConfigChanged
from ..ids import ALBUM_ID_LENGTH, is_valid_id
from ..payloads import album_to_payload
from ..public_origin import public_base_url
from ..service import AdminAlbumUpdateInput, UNSET, UserCreateInput, UserUpdateInput
from .context import correlation_id, get_state, require_admin_user

router = APIRouter()


class AdminUserCreateRequest(BaseModel):
    username: str
    email: str
    password: str | None = None
    is_admin: bool = False
    quota_bytes: int | None = None
    rate_limit_rpm: int | None = None
    rate_limit_bph: int | None = None


class AdminUserPatchRequest(BaseModel):
    suspended: bool | None = None
    quota_bytes: int | None = None
    rate_limit_rpm: int | None = None
    rate_limit_bph: int | None = None
    password: str | None = None


class AdminUserPasswordResetRequest(BaseModel):
    new_password: str


class AdminAlbumPatchRequest(BaseModel):
    expires_at: datetime | None = None


class AdminConfigPatchRequest(BaseModel):
    allow_registration: bool | None = None
    anon_upload_enabled: bool | None = None
    anon_expiry_hours: int | None = None
    rate_limit_anon_rpm: int | None = None
    rate_limit_anon_bph: int | None = None
    rate_limit_global_anon_rpm: int | None = None
    rate_limit_global_anon_bph: int | None = None
    rate_limit_user_rpm: int | None = None
    rate_limit_user_bph: int | None = None


@router.get("/api/v1/admin/users")
async def admin_list_users(
    request: Request,
    q: str | None = None,
    is_admin: bool | None = None,
    suspended: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JSONResponse:
    state = get_state(request)
    await require_admin_user(request)
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be non-negative.")
    payload = await state.uploads.list_users_with_usage_page(
        q=(q or "").strip() or None,
        is_admin=is_admin,
        suspended=suspended,
        limit=limit,
        offset=offset,
    )
    return JSONResponse(payload, headers={"X-Correlation-ID": correlation_id(request)})


@router.get("/api/v1/admin/users/{user_id}")
async def admin_get_user(request: Request, user_id: str) -> JSONResponse:
    state = get_state(request)
    await require_admin_user(request)
    payload = await state.uploads.get_user_with_usage_for_admin(user_id)
    return JSONResponse(payload, headers={"X-Correlation-ID": correlation_id(request)})


@router.get("/api/v1/admin/users/{user_id}/stats")
async def admin_get_user_stats(request: Request, user_id: str) -> JSONResponse:
    state = get_state(request)
    await require_admin_user(request)
    payload = await state.uploads.get_user_storage_stats_for_admin(user_id)
    return JSONResponse(payload, headers={"X-Correlation-ID": correlation_id(request)})


@router.get("/api/v1/admin/users/{user_id}/albums")
async def admin_list_user_albums(request: Request, user_id: str, limit: int = 10, offset: int = 0) -> JSONResponse:
    state = get_state(request)
    await require_admin_user(request)
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be non-negative.")
    payload = await state.uploads.list_albums_for_user_admin_page(
        user_id,
        base_url=public_base_url(request, state.settings),
        limit=limit,
        offset=offset,
    )
    return JSONResponse(payload, headers={"X-Correlation-ID": correlation_id(request)})


@router.get("/api/v1/admin/albums")
async def admin_list_albums(
    request: Request,
    q: str | None = None,
    owner: str | None = None,
    anonymous: bool | None = None,
    limit: int = 10,
    offset: int = 0,
) -> JSONResponse:
    state = get_state(request)
    await require_admin_user(request)
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be non-negative.")
    payload = await state.uploads.list_albums_for_admin_page(
        q=(q or "").strip() or None,
        owner=(owner or "").strip() or None,
        anonymous=anonymous,
        limit=limit,
        offset=offset,
    )
    return JSONResponse(payload, headers={"X-Correlation-ID": correlation_id(request)})


@router.get("/api/v1/admin/audit")
async def admin_list_audit(
    request: Request,
    event_type: str | None = None,
    actor_id: str | None = None,
    user_id: str | None = None,
    correlation_id_filter: str | None = Query(default=None, alias="correlation_id"),
    after: datetime | None = None,
    before: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
) -> JSONResponse:
    state = get_state(request)
    await require_admin_user(request)
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 500.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be non-negative.")
    events = await state.audit.query_audit_log(
        event_type=event_type,
        actor_id=actor_id,
        user_id=user_id,
        correlation_id=correlation_id_filter,
        after=after,
        before=before,
        limit=limit,
        offset=offset,
    )
    return JSONResponse([event.to_dict() for event in events], headers={"X-Correlation-ID": correlation_id(request)})


@router.get("/api/v1/admin/config")
async def admin_get_config(request: Request) -> JSONResponse:
    state = get_state(request)
    await require_admin_user(request)
    payload = await state.runtime_config.list_effective()
    return JSONResponse({key: value.to_dict() for key, value in payload.items()}, headers={"X-Correlation-ID": correlation_id(request)})


@router.patch("/api/v1/admin/config")
async def admin_patch_config(request: Request, payload: AdminConfigPatchRequest) -> JSONResponse:
    state = get_state(request)
    cid = correlation_id(request)
    admin = await require_admin_user(request)
    updates = payload.model_dump(exclude_unset=True)
    changes = await state.runtime_config.update_values(updates)
    for change in changes:
        await state.event_bus.emit(
            ConfigChanged(
                key=change["key"],
                actor_id=admin.id,
                old_value=change["old_value"],
                new_value=change["new_value"],
                source="api",
                correlation_id=cid,
            )
        )
    resolved = await state.runtime_config.list_effective()
    return JSONResponse({key: value.to_dict() for key, value in resolved.items()}, headers={"X-Correlation-ID": cid})


@router.post("/api/v1/admin/users")
async def admin_create_user(request: Request, payload: AdminUserCreateRequest) -> JSONResponse:
    state = get_state(request)
    cid = correlation_id(request)
    admin = await require_admin_user(request)
    created = await state.uploads.create_user(
        payload=UserCreateInput(
            username=payload.username,
            email=payload.email,
            password=payload.password,
            is_admin=payload.is_admin,
            quota_bytes=payload.quota_bytes,
            rate_limit_rpm=payload.rate_limit_rpm,
            rate_limit_bph=payload.rate_limit_bph,
        ),
        method="admin",
        correlation_id=cid,
        actor_id=admin.id,
        source="api",
    )
    return JSONResponse(
        {
            "id": created.id,
            "username": created.username,
            "email": created.email,
            "is_admin": created.is_admin,
            "suspended": created.suspended,
            "quota_bytes": created.quota_bytes if created.quota_bytes is not None else state.settings.default_user_quota_bytes,
            "rate_limit_rpm": created.rate_limit_rpm,
            "rate_limit_bph": created.rate_limit_bph,
        },
        status_code=201,
        headers={"X-Correlation-ID": cid},
    )


@router.patch("/api/v1/admin/users/{user_id}")
async def admin_patch_user(request: Request, user_id: str, payload: AdminUserPatchRequest) -> JSONResponse:
    state = get_state(request)
    cid = correlation_id(request)
    admin = await require_admin_user(request)
    if "password" in payload.model_fields_set:
        raise HTTPException(
            status_code=400,
            detail="Use the dedicated admin password reset endpoint for password changes.",
        )
    updated = await state.uploads.update_user(
        user_id,
        payload=UserUpdateInput(
            suspended=payload.suspended if "suspended" in payload.model_fields_set else None,
            quota_bytes=payload.quota_bytes if "quota_bytes" in payload.model_fields_set else UNSET,
            rate_limit_rpm=payload.rate_limit_rpm if "rate_limit_rpm" in payload.model_fields_set else UNSET,
            rate_limit_bph=payload.rate_limit_bph if "rate_limit_bph" in payload.model_fields_set else UNSET,
        ),
        correlation_id=cid,
        actor_id=admin.id,
    )
    return JSONResponse(
        {
            "id": updated.id,
            "username": updated.username,
            "email": updated.email,
            "is_admin": updated.is_admin,
            "suspended": updated.suspended,
            "quota_bytes": updated.quota_bytes if updated.quota_bytes is not None else state.settings.default_user_quota_bytes,
            "rate_limit_rpm": updated.rate_limit_rpm,
            "rate_limit_bph": updated.rate_limit_bph,
        },
        headers={"X-Correlation-ID": cid},
    )


@router.post("/api/v1/admin/users/{user_id}/reset-password")
async def admin_reset_user_password(
    request: Request, user_id: str, payload: AdminUserPasswordResetRequest
) -> JSONResponse:
    state = get_state(request)
    cid = correlation_id(request)
    admin = await require_admin_user(request)
    updated = await state.uploads.reset_user_password(
        user_id,
        payload.new_password,
        cid,
        actor_id=admin.id,
    )
    return JSONResponse(
        {
            "reset": True,
            "user_id": updated.id,
        },
        headers={"X-Correlation-ID": cid},
    )


@router.delete("/api/v1/admin/users/{user_id}")
async def admin_delete_user(request: Request, user_id: str) -> JSONResponse:
    state = get_state(request)
    cid = correlation_id(request)
    admin = await require_admin_user(request)
    deleted = await state.uploads.delete_user_by_id(user_id, cid, deleted_by="admin", actor_id=admin.id)
    return JSONResponse(
        {
            "deleted": True,
            "user_id": user_id,
            "album_count": deleted["album_count"],
            "media_count": deleted["media_count"],
        },
        headers={"X-Correlation-ID": cid},
    )


@router.patch("/api/v1/admin/albums/{album_id}")
async def admin_patch_album(request: Request, album_id: str, payload: AdminAlbumPatchRequest) -> JSONResponse:
    if not is_valid_id(album_id, ALBUM_ID_LENGTH):
        raise HTTPException(status_code=404)
    state = get_state(request)
    cid = correlation_id(request)
    admin = await require_admin_user(request)
    updated = await state.uploads.admin_update_album(
        album_id,
        AdminAlbumUpdateInput(
            expires_at=payload.expires_at if "expires_at" in payload.model_fields_set else UNSET,
        ),
        cid,
        actor_id=admin.id,
    )
    items = await state.repository.list_album_media(album_id)
    return JSONResponse(
        album_to_payload(public_base_url(request, state.settings), updated, items),
        headers={"X-Correlation-ID": cid},
    )


@router.delete("/api/v1/admin/albums/{album_id}")
async def admin_delete_album(request: Request, album_id: str) -> JSONResponse:
    if not is_valid_id(album_id, ALBUM_ID_LENGTH):
        raise HTTPException(status_code=404)
    state = get_state(request)
    cid = correlation_id(request)
    admin = await require_admin_user(request)
    album, items = await state.uploads.delete_album(album_id, None, cid, actor_user=admin)
    return JSONResponse(
        {
            "deleted": True,
            "album_id": album.id,
            "item_count": len(items),
        },
        headers={"X-Correlation-ID": cid},
    )


@router.get("/api/v1/admin/stats")
async def admin_stats(request: Request) -> JSONResponse:
    state = get_state(request)
    await require_admin_user(request)
    payload = await state.uploads.global_storage_stats()
    return JSONResponse(payload, headers={"X-Correlation-ID": correlation_id(request)})


@router.get("/api/v1/admin/runtime-status")
async def admin_runtime_status(request: Request) -> JSONResponse:
    state = get_state(request)
    await require_admin_user(request)
    payload = await state.runtime_status()
    return JSONResponse(payload, headers={"X-Correlation-ID": correlation_id(request)})
