from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from math import ceil
from typing import Any
from uuid import uuid4
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse

from .config import Settings, load_settings
from .events import EventBus
from .ids import ALBUM_ID_LENGTH, MEDIA_ID_LENGTH, is_valid_id
from .repositories import JsonRepository
from .service import UploadService
from .storage import LocalFilesystemBackend


class AppState:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.event_bus = EventBus()
        self.repository = JsonRepository(settings.data_dir / "state.json")
        self.storage = LocalFilesystemBackend(settings.data_dir)
        self.uploads = UploadService(settings, self.repository, self.storage, self.event_bus)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    app.state.imghost = AppState(settings)
    yield


app = FastAPI(title="imghost V1", lifespan=lifespan)


def get_state(request: Request) -> AppState:
    return request.app.state.imghost


def correlation_id(request: Request) -> str:
    return request.headers.get("X-Correlation-ID") or str(uuid4())


def media_url(base_url: str, media_id: str, fmt: str) -> str:
    ext = f".{fmt}" if fmt else ""
    return f"{base_url}/i/{media_id}{ext}"


def thumb_url(base_url: str, media_id: str, fmt: str) -> str:
    ext = f".{fmt}" if fmt else ""
    return f"{base_url}/t/{media_id}{ext}"


def extract_media_id(raw_id: str) -> str:
    return raw_id.rsplit(".", 1)[0].lower()


def is_expired(expires_at: datetime | None) -> bool:
    return expires_at is not None and expires_at <= datetime.now(UTC)


def humanize_expiry(expires_at: datetime | None) -> str | None:
    if expires_at is None:
        return None
    delta = expires_at - datetime.now(UTC)
    total_seconds = max(0, int(delta.total_seconds()))
    if total_seconds < 3600:
        minutes = max(1, ceil(total_seconds / 60))
        return f"This album expires in {minutes} minute(s)."
    if total_seconds < 86400:
        hours = ceil(total_seconds / 3600)
        return f"This album expires in {hours} hour(s)."
    days = ceil(total_seconds / 86400)
    return f"This album expires in {days} day(s)."


def album_delete_url(base_url: str, album: Any) -> str | None:
    if not album.delete_token:
        return None
    query = urlencode({"delete_token": album.delete_token})
    return f"{base_url}/api/v1/album/{album.id}/delete?{query}"


def resolve_cover_media(album: Any, media_items: list[Any]) -> Any | None:
    if album.cover_media_id:
        for item in media_items:
            if item.id == album.cover_media_id:
                return item
    return media_items[0] if media_items else None


def album_to_payload(base_url: str, album: Any, media_items: list[Any]) -> dict[str, Any]:
    cover = resolve_cover_media(album, media_items)
    return {
        "id": album.id,
        "title": album.title,
        "created_at": album.created_at.isoformat(),
        "updated_at": album.updated_at.isoformat(),
        "expires_at": album.expires_at.isoformat() if album.expires_at else None,
        "delete_url": album_delete_url(base_url, album),
        "item_count": len(media_items),
        "total_size": sum(item.file_size for item in media_items),
        "cover_url": media_url(base_url, cover.id, cover.format) if cover else None,
        "items": [
            {
                "id": item.id,
                "filename": item.filename_orig,
                "media_type": item.media_type,
                "mime_type": item.mime_type,
                "media_url": media_url(base_url, item.id, item.format),
                "thumb_url": thumb_url(base_url, item.id, item.format),
                "position": item.position,
                "file_size": item.file_size,
                "thumb_status": item.thumb_status,
            }
            for item in media_items
        ],
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> str:
    state = get_state(request)
    return f"""
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>imghost</title>
    <style>
      :root {{ color-scheme: light; --bg: #f5efe4; --fg: #14213d; --card: #fffaf2; --accent: #d97706; }}
      body {{ margin: 0; font-family: Georgia, serif; background: radial-gradient(circle at top, #fff8eb, var(--bg)); color: var(--fg); }}
      main {{ max-width: 760px; margin: 0 auto; padding: 48px 20px 64px; }}
      .card {{ background: var(--card); border: 1px solid #eadcc2; border-radius: 20px; padding: 24px; box-shadow: 0 12px 30px rgba(20,33,61,.08); }}
      h1 {{ font-size: 3rem; margin: 0 0 12px; }}
      p {{ line-height: 1.5; }}
      form {{ display: grid; gap: 12px; margin-top: 24px; }}
      input, button {{ font: inherit; }}
      input[type="text"], input[type="file"] {{ padding: 12px; background: white; border: 1px solid #d4c5a8; border-radius: 12px; }}
      button {{ padding: 14px 18px; border: 0; border-radius: 999px; background: var(--accent); color: white; cursor: pointer; }}
      .hint {{ color: #6b7280; font-size: .95rem; }}
    </style>
  </head>
  <body>
    <main>
      <section class="card">
        <p class="hint">Step 3 of 5: V1 upload flow is live.</p>
        <h1>imghost</h1>
        <p>Paste or pick one or more files to create an anonymous album with clean media URLs.</p>
        <form action="/api/v1/upload" method="post" enctype="multipart/form-data">
          <input type="text" name="title" placeholder="Album title (optional)">
          <input type="file" name="file" required multiple>
          <button type="submit">Upload</button>
        </form>
        <p class="hint">Base URL: {state.settings.base_url}</p>
      </section>
    </main>
  </body>
</html>
"""


@app.post("/api/v1/upload")
async def upload(
    request: Request,
    file: list[UploadFile] = File(...),
    album_id: str | None = Form(default=None),
    title: str | None = Form(default=None),
) -> JSONResponse:
    state = get_state(request)
    cid = correlation_id(request)
    results = []
    active_album_id = album_id
    for item in file:
        result = await state.uploads.upload(item, active_album_id, title, cid)
        active_album_id = result.album.id
        results.append(result)

    primary = results[0]
    payload = {
        "album_id": primary.album.id,
        "album_url": f"{state.settings.base_url}/a/{primary.album.id}",
        "media_id": primary.media.id,
        "media_url": media_url(state.settings.base_url, primary.media.id, primary.media.format),
        "thumb_url": thumb_url(state.settings.base_url, primary.media.id, primary.media.format),
        "delete_url": album_delete_url(state.settings.base_url, primary.album),
        "expires_at": primary.album.expires_at.isoformat() if primary.album.expires_at else None,
        "items": [
            {
                "media_id": result.media.id,
                "media_url": media_url(state.settings.base_url, result.media.id, result.media.format),
                "thumb_url": thumb_url(state.settings.base_url, result.media.id, result.media.format),
                "thumb_status": result.media.thumb_status,
            }
            for result in results
        ],
    }
    headers = {"X-Correlation-ID": cid}
    return JSONResponse(payload, headers=headers)


@app.get("/api/v1/album/{album_id}")
async def get_album(request: Request, album_id: str) -> JSONResponse:
    if not is_valid_id(album_id, ALBUM_ID_LENGTH):
        raise HTTPException(status_code=404)
    state = get_state(request)
    album = await state.repository.get_album(album_id)
    if album is None or is_expired(album.expires_at):
        raise HTTPException(status_code=404)
    items = await state.repository.list_album_media(album_id)
    return JSONResponse(album_to_payload(state.settings.base_url, album, items))


@app.get("/a/{album_id}", response_class=HTMLResponse)
async def album_page(request: Request, album_id: str) -> str:
    if not is_valid_id(album_id, ALBUM_ID_LENGTH):
        raise HTTPException(status_code=404)
    state = get_state(request)
    album = await state.repository.get_album(album_id)
    if album is None or is_expired(album.expires_at):
        raise HTTPException(status_code=404)
    items = await state.repository.list_album_media(album_id)
    expiry_hint = humanize_expiry(album.expires_at)
    cards = []
    for item in items:
        preview_url = thumb_url(state.settings.base_url, item.id, item.format)
        if item.media_type == "video":
            poster_attr = f' poster="{preview_url}"' if item.thumb_status == "done" else ""
            media_tag = f'<video controls preload="metadata" src="{media_url(state.settings.base_url, item.id, item.format)}"{poster_attr}></video>'
        else:
            if item.thumb_status == "done":
                media_tag = f'<img src="{preview_url}" alt="{item.filename_orig}">'
            else:
                media_tag = '<div class="placeholder">Thumbnail pending</div>'
        cards.append(
            f"""
            <article class="item">
              {media_tag}
              <input type="text" readonly value="{media_url(state.settings.base_url, item.id, item.format)}">
            </article>
            """
        )

    return f"""
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{album.title or album.id}</title>
    <style>
      body {{ margin: 0; font-family: Georgia, serif; background: #f4f1ea; color: #18212f; }}
      main {{ max-width: 980px; margin: 0 auto; padding: 32px 20px 64px; }}
      .hero {{ margin-bottom: 24px; }}
      .grid {{ display: grid; gap: 20px; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }}
      .item {{ background: #fffdf8; border: 1px solid #e3d6be; border-radius: 18px; padding: 12px; }}
      img, video, .placeholder {{ width: 100%; display: block; border-radius: 12px; background: #ebe6dc; }}
      .placeholder {{ min-height: 220px; display: grid; place-items: center; color: #786b57; font-style: italic; }}
      input {{ width: 100%; margin-top: 12px; padding: 10px; border-radius: 10px; border: 1px solid #d5c6ab; }}
      .hint {{ color: #786b57; }}
      .banner {{ background: #fff2d8; border: 1px solid #e6c88f; color: #7c5414; border-radius: 14px; padding: 10px 14px; margin: 12px 0 0; }}
      .actions {{ margin-top: 16px; }}
    </style>
  </head>
  <body>
    <main>
      <section class="hero">
        <p class="hint">V1.1 public album view.</p>
        <h1>{album.title or "Untitled album"}</h1>
        <p>{len(items)} item(s) · Created {album.created_at.isoformat()}</p>
        {f'<p class="banner">{expiry_hint}</p>' if expiry_hint else ''}
        <p class="actions"><a href="/api/v1/album/{album.id}/zip">Download as ZIP</a></p>
      </section>
      <section class="grid">
        {''.join(cards)}
      </section>
    </main>
  </body>
</html>
"""


async def stream_media(request: Request, raw_id: str, thumb: bool) -> StreamingResponse:
    media_id = extract_media_id(raw_id)
    if not is_valid_id(media_id, MEDIA_ID_LENGTH):
        raise HTTPException(status_code=404)
    state = get_state(request)
    media = await state.repository.get_media(media_id)
    if media is None:
        raise HTTPException(status_code=404)
    album = await state.repository.get_album(media.album_id)
    if album is None or is_expired(album.expires_at):
        raise HTTPException(status_code=404)
    if thumb and media.thumb_status != "done":
        return StreamingResponse(iter(()), status_code=202)
    key = media.storage_key if (not thumb or media.thumb_is_orig or not media.thumb_key) else media.thumb_key
    stream = await state.storage.get_stream(key, request.headers.get("Range"))
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
        media_type=media.mime_type,
        headers=headers,
    )


@app.get("/i/{raw_id}")
async def raw_media(request: Request, raw_id: str) -> StreamingResponse:
    return await stream_media(request, raw_id, thumb=False)


@app.get("/t/{raw_id}")
async def thumbnail_media(request: Request, raw_id: str) -> StreamingResponse:
    return await stream_media(request, raw_id, thumb=True)


@app.get("/api/v1/album/{album_id}/zip")
async def download_album_zip(request: Request, album_id: str) -> Response:
    if not is_valid_id(album_id, ALBUM_ID_LENGTH):
        raise HTTPException(status_code=404)
    state = get_state(request)
    album = await state.repository.get_album(album_id)
    if album is None or is_expired(album.expires_at):
        raise HTTPException(status_code=404)
    archive = await state.uploads.build_album_zip(album_id)
    filename = f"{album.id}.zip"
    return Response(
        content=archive,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/api/v1/album/{album_id}")
async def delete_album(request: Request, album_id: str, delete_token: str | None = None) -> JSONResponse:
    if not is_valid_id(album_id, ALBUM_ID_LENGTH):
        raise HTTPException(status_code=404)
    state = get_state(request)
    cid = correlation_id(request)
    album, items = await state.uploads.delete_album(album_id, delete_token, cid)
    return JSONResponse(
        {
            "deleted": True,
            "album_id": album.id,
            "item_count": len(items),
        },
        headers={"X-Correlation-ID": cid},
    )


@app.get("/api/v1/album/{album_id}/delete")
async def delete_album_via_get(request: Request, album_id: str, delete_token: str | None = None) -> JSONResponse:
    return await delete_album(request, album_id, delete_token)


@app.get("/health/live")
async def health_live() -> PlainTextResponse:
    return PlainTextResponse("ok")


@app.get("/health/ready")
async def health_ready(request: Request) -> JSONResponse:
    state = get_state(request)
    ready = await state.storage.health_check()
    return JSONResponse({"ok": ready}, status_code=200 if ready else 503)
