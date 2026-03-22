from __future__ import annotations

from pydantic import BaseModel
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ..service import PasswordChangeInput
from ..public_origin import public_base_url
from .auth_context import authenticated_principal, authenticated_user
from .pagination import validate_pagination
from .request_context import correlation_id, get_state

router = APIRouter()


class UserPasswordPatchRequest(BaseModel):
    current_password: str
    new_password: str


@router.get("/api/v1/user/me")
async def get_current_user(request: Request) -> JSONResponse:
    state = get_state(request)
    user = await authenticated_user(request, required=True)
    summary = await state.uploads.get_current_user_summary(user)
    return JSONResponse(summary, headers={"X-Correlation-ID": correlation_id(request)})


@router.get("/api/v1/user/me/albums")
async def get_current_user_albums(request: Request, limit: int = 10, offset: int = 0) -> JSONResponse:
    state = get_state(request)
    base_url = public_base_url(request, state.settings)
    user = await authenticated_user(request, required=True)
    validate_pagination(limit, offset)
    payload = await state.uploads.get_current_user_albums_page(user, base_url=base_url, limit=limit, offset=offset)
    return JSONResponse(payload, headers={"X-Correlation-ID": correlation_id(request)})


@router.post("/api/v1/user/me/api-key")
async def regenerate_api_key(request: Request) -> JSONResponse:
    state = get_state(request)
    user = await authenticated_user(request, required=True)
    issued = await state.uploads.issue_api_key(user)
    return JSONResponse(
        {
            "api_key": issued.raw_key,
            "created_at": issued.api_key.created_at.isoformat(),
        },
        headers={"X-Correlation-ID": correlation_id(request)},
    )


@router.patch("/api/v1/user/me/password")
async def change_current_user_password(request: Request, payload: UserPasswordPatchRequest) -> JSONResponse:
    state = get_state(request)
    user = await authenticated_user(request, required=True)
    cid = correlation_id(request)
    await state.uploads.change_password(
        user,
        PasswordChangeInput(
            current_password=payload.current_password,
            new_password=payload.new_password,
        ),
    )
    return JSONResponse({"updated": True}, headers={"X-Correlation-ID": cid})


@router.get("/api/v1/user/me/sharex-config")
async def download_sharex_config(request: Request) -> Response:
    principal = await authenticated_principal(request, required=True)
    state = get_state(request)
    raw_api_key = principal.raw_api_key
    if raw_api_key is None:
        issued = await state.uploads.issue_api_key(principal.user)
        raw_api_key = issued.raw_key
    base_url = public_base_url(request, state.settings)
    payload = {
        "Version": "14.1.0",
        "Name": "imghost",
        "DestinationType": "ImageUploader, FileUploader",
        "RequestMethod": "POST",
        "RequestURL": f"{base_url}/api/v1/upload",
        "Headers": {
            "Authorization": f"Bearer {raw_api_key}",
        },
        "Body": "MultipartFormData",
        "FileFormName": "file",
        "URL": "$json:media_url$",
        "ThumbnailURL": "$json:thumb_url$",
        "DeletionURL": "$json:delete_url$",
    }
    return Response(
        content=JSONResponse(payload).body,
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="imghost.sxcu"',
            "X-Correlation-ID": correlation_id(request),
        },
    )


@router.delete("/api/v1/user/me")
async def delete_current_user(request: Request) -> JSONResponse:
    state = get_state(request)
    user = await authenticated_user(request, required=True)
    cid = correlation_id(request)
    deleted = await state.uploads.delete_user_account(user, cid)
    return JSONResponse(
        {
            "deleted": True,
            "user_id": user.id,
            "album_count": deleted["album_count"],
            "media_count": deleted["media_count"],
        },
        headers={"X-Correlation-ID": cid},
    )
