from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from .display_helpers import is_expired
from .media_helpers import thumb_media_type, validate_media_id_or_404
from .request_context import get_state
from ..storage import InvalidByteRange, normalize_byte_range

router = APIRouter()


async def stream_media(request: Request, raw_id: str, thumb: bool) -> StreamingResponse:
    media_id = validate_media_id_or_404(raw_id)
    state = get_state(request)
    media = await state.repository.get_media(media_id)
    if media is None:
        raise HTTPException(status_code=404)
    album = await state.repository.get_album(media.album_id)
    if album is None or is_expired(album.expires_at):
        raise HTTPException(status_code=404)
    if thumb and media.thumb_status in {"pending", "processing"}:
        return StreamingResponse(iter(()), status_code=202)
    if thumb and media.thumb_status == "failed":
        raise HTTPException(status_code=404)
    total_size = media.file_size if (not thumb or media.thumb_is_orig or not media.thumb_size) else media.thumb_size
    try:
        byte_range = normalize_byte_range(request.headers.get("Range"), total_size)
    except InvalidByteRange:
        raise HTTPException(
            status_code=416,
            headers={"Content-Range": f"bytes */{total_size}"},
        ) from None
    key = media.storage_key if (not thumb or media.thumb_is_orig or not media.thumb_key) else media.thumb_key
    stream = await state.storage.get_stream(key, byte_range.header_value if byte_range else None)
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=31536000, immutable",
        "ETag": f'"{media.id}"',
    }
    if stream.content_range:
        headers["Content-Range"] = stream.content_range
    return StreamingResponse(
        stream.body,
        status_code=stream.status_code,
        media_type=thumb_media_type(media) if thumb else media.mime_type,
        headers=headers,
    )


@router.get("/i/{raw_id}")
async def raw_media(request: Request, raw_id: str) -> StreamingResponse:
    return await stream_media(request, raw_id, thumb=False)


@router.get("/t/{raw_id}")
async def thumbnail_media(request: Request, raw_id: str) -> StreamingResponse:
    return await stream_media(request, raw_id, thumb=True)
