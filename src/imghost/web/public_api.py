from __future__ import annotations

from pydantic import BaseModel
from urllib.parse import urlencode

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from ..ids import ALBUM_ID_LENGTH, MEDIA_ID_LENGTH, is_valid_id
from ..payloads import album_to_payload, media_url, thumb_format, thumb_url
from ..public_origin import public_base_url
from ..sharex_delete import ShareXDeletePayload, ShareXDeleteTokenManager
from ..service import CurrentActor, UNSET
from .auth_context import authenticated_principal, authenticated_user
from .display_helpers import album_manage_url, is_expired
from .request_context import correlation_id, get_state
from .request_helpers import upload_rate_limit_key

router = APIRouter()


class AlbumPatchRequest(BaseModel):
    title: str | None = None
    cover_media_id: str | None = None


class AlbumOrderItem(BaseModel):
    media_id: str
    position: int


def build_sharex_delete_url(base_url: str, secret_key: str, album_id: str, user_id: str) -> str:
    token = ShareXDeleteTokenManager(secret_key).dumps(ShareXDeletePayload(album_id=album_id, user_id=user_id))
    return f"{base_url}/api/v1/album/{album_id}/delete?{urlencode({'token': token})}"


@router.post("/api/v1/upload")
async def upload(
    request: Request,
    file: list[UploadFile] = File(...),
    album_id: str | None = Form(default=None),
    title: str | None = Form(default=None),
    delete_token: str | None = Form(default=None),
) -> JSONResponse:
    state = get_state(request)
    base_url = public_base_url(request, state.settings)
    cid = correlation_id(request)
    principal = await authenticated_principal(request, required=False)
    user = principal.user if principal is not None else None
    if user is None and not await state.runtime_config.get_value("anon_upload_enabled"):
        raise HTTPException(status_code=403, detail="Anonymous uploads are disabled.")
    is_api_key_upload = principal is not None and principal.raw_api_key is not None
    actor = CurrentActor(user=user, source="api" if is_api_key_upload else "web")
    results = []
    active_album_id = album_id
    active_delete_token = delete_token
    for item in file:
        result = await state.uploads.upload(
            item,
            active_album_id,
            title,
            cid,
            actor=actor,
            delete_token=active_delete_token,
            rate_limit_key=upload_rate_limit_key(request, user),
        )
        active_album_id = result.album.id
        if result.album.delete_token:
            active_delete_token = result.album.delete_token
        results.append(result)

    primary = results[0]
    payload = {
        "album_id": primary.album.id,
        "album_url": f"{base_url}/a/{primary.album.id}",
        "media_id": primary.media.id,
        "media_url": media_url(base_url, primary.media.id, primary.media.format),
        "thumb_url": thumb_url(base_url, primary.media.id, primary.media.format),
        "manage_url": album_manage_url(base_url, primary.album, include_token=True) if primary.album.delete_token else None,
        "delete_url": (
            build_sharex_delete_url(base_url, state.settings.secret_key, primary.album.id, user.id)
            if is_api_key_upload and user is not None and primary.album.user_id == user.id
            else None
        ),
        "expires_at": primary.album.expires_at.isoformat() if primary.album.expires_at else None,
        "items": [
            {
                "media_id": result.media.id,
                "media_url": media_url(base_url, result.media.id, result.media.format),
                "thumb_url": thumb_url(base_url, result.media.id, thumb_format(result.media)),
                "thumb_status": result.media.thumb_status,
            }
            for result in results
        ],
    }
    headers = {"X-Correlation-ID": cid}
    return JSONResponse(payload, headers=headers)


@router.get("/api/v1/album/{album_id}/delete")
async def delete_album_via_sharex(request: Request, album_id: str, token: str) -> JSONResponse:
    if not is_valid_id(album_id, ALBUM_ID_LENGTH):
        raise HTTPException(status_code=404)
    state = get_state(request)
    cid = correlation_id(request)
    try:
        payload = ShareXDeleteTokenManager(state.settings.secret_key).loads(token)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Invalid ShareX deletion URL.") from exc
    if payload.album_id != album_id:
        raise HTTPException(status_code=403, detail="Invalid ShareX deletion URL.")
    album = await state.repository.get_album(album_id)
    if album is None or album.user_id != payload.user_id:
        raise HTTPException(status_code=403, detail="Invalid ShareX deletion URL.")
    actor_user = await state.repository.get_user(payload.user_id)
    if actor_user is None or actor_user.suspended:
        raise HTTPException(status_code=403, detail="Invalid ShareX deletion URL.")
    deleted_album, items = await state.uploads.delete_album(album_id, None, cid, actor_user=actor_user)
    return JSONResponse(
        {
            "deleted": True,
            "album_id": deleted_album.id,
            "item_count": len(items),
        },
        headers={"X-Correlation-ID": cid},
    )


@router.get("/api/v1/album/{album_id}")
async def get_album(request: Request, album_id: str) -> JSONResponse:
    if not is_valid_id(album_id, ALBUM_ID_LENGTH):
        raise HTTPException(status_code=404)
    state = get_state(request)
    album = await state.repository.get_album(album_id)
    if album is None or is_expired(album.expires_at):
        raise HTTPException(status_code=404)
    items = await state.repository.list_album_media(album_id)
    return JSONResponse(album_to_payload(public_base_url(request, state.settings), album, items))


@router.get("/api/v1/album/{album_id}/zip")
async def download_album_zip(request: Request, album_id: str) -> StreamingResponse:
    if not is_valid_id(album_id, ALBUM_ID_LENGTH):
        raise HTTPException(status_code=404)
    state = get_state(request)
    album = await state.repository.get_album(album_id)
    if album is None or is_expired(album.expires_at):
        raise HTTPException(status_code=404)
    archive = await state.uploads.stream_album_zip(album_id)
    filename = f"{album.id}.zip"
    return StreamingResponse(
        archive,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/api/v1/album/{album_id}")
async def delete_album(request: Request, album_id: str, delete_token: str | None = None) -> JSONResponse:
    if not is_valid_id(album_id, ALBUM_ID_LENGTH):
        raise HTTPException(status_code=404)
    state = get_state(request)
    cid = correlation_id(request)
    user = await authenticated_user(request, required=False)
    album, items = await state.uploads.delete_album(album_id, delete_token, cid, actor_user=user)
    return JSONResponse(
        {
            "deleted": True,
            "album_id": album.id,
            "item_count": len(items),
        },
        headers={"X-Correlation-ID": cid},
    )


@router.patch("/api/v1/album/{album_id}")
async def patch_album(
    request: Request,
    album_id: str,
    payload: AlbumPatchRequest,
    delete_token: str | None = None,
) -> JSONResponse:
    if not is_valid_id(album_id, ALBUM_ID_LENGTH):
        raise HTTPException(status_code=404)
    state = get_state(request)
    cid = correlation_id(request)
    user = await authenticated_user(request, required=False)
    album, items = await state.uploads.update_album(
        album_id,
        delete_token,
        cid,
        actor_user=user,
        title=payload.title if "title" in payload.model_fields_set else UNSET,
        cover_media_id=payload.cover_media_id if "cover_media_id" in payload.model_fields_set else UNSET,
    )
    return JSONResponse(
        album_to_payload(public_base_url(request, state.settings), album, items),
        headers={"X-Correlation-ID": cid},
    )


@router.patch("/api/v1/album/{album_id}/order")
async def patch_album_order(
    request: Request,
    album_id: str,
    items: list[AlbumOrderItem],
    delete_token: str | None = None,
) -> JSONResponse:
    if not is_valid_id(album_id, ALBUM_ID_LENGTH):
        raise HTTPException(status_code=404)
    state = get_state(request)
    cid = correlation_id(request)
    user = await authenticated_user(request, required=False)
    album, media_items = await state.uploads.reorder_album(
        album_id,
        delete_token,
        [(item.media_id, item.position) for item in items],
        cid,
        actor_user=user,
    )
    return JSONResponse(
        album_to_payload(public_base_url(request, state.settings), album, media_items),
        headers={"X-Correlation-ID": cid},
    )


@router.delete("/api/v1/media/{media_id}")
async def delete_media(request: Request, media_id: str, delete_token: str | None = None) -> JSONResponse:
    if not is_valid_id(media_id, MEDIA_ID_LENGTH):
        raise HTTPException(status_code=404)
    state = get_state(request)
    cid = correlation_id(request)
    user = await authenticated_user(request, required=False)
    result = await state.uploads.delete_media(media_id, delete_token, cid, actor_user=user)
    return JSONResponse(
        {
            "deleted": True,
            "media_id": result.deleted_media.id,
            "album_id": result.deleted_media.album_id,
            "album_deleted": result.album_deleted,
            "remaining_item_count": len(result.remaining_items),
        },
        headers={"X-Correlation-ID": cid},
    )
