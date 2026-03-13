from __future__ import annotations

from contextlib import asynccontextmanager
import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hmac
import json
from hashlib import sha256
from math import ceil
from typing import Any
from uuid import uuid4
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel

from .audit import JsonAuditLog, register_audit_listeners
from .config import Settings, load_settings
from .events import ConfigChanged, EventBus, MediaUploaded
from .ids import ALBUM_ID_LENGTH, MEDIA_ID_LENGTH, is_valid_id
from .processors import ProcessorRegistry, build_processor_registry
from .repositories import JsonRepository
from .models import User, utcnow
from .runtime_config import JsonRuntimeConfig
from .service import (
    AdminAlbumUpdateInput,
    CurrentActor,
    LocalLoginInput,
    PasswordChangeInput,
    UNSET,
    UploadService,
    UserCreateInput,
    UserUpdateInput,
)
from .storage import LocalFilesystemBackend
from .tasks import AsyncTaskQueue, SyncTaskQueue, TaskContext, TaskQueue


class AppState:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.event_bus = EventBus()
        self.repository = JsonRepository(settings.data_dir / "state.json")
        self.audit = JsonAuditLog(settings.data_dir / "audit.json")
        self.runtime_config = JsonRuntimeConfig(settings.data_dir / "config.json")
        self.storage = LocalFilesystemBackend(settings.data_dir)
        self.processors = build_processor_registry(
            settings.max_pixel_megapixels * 1_000_000,
            settings.video_thumb_frames,
        )
        self.tasks = self._build_task_queue()
        self.uploads = UploadService(
            settings,
            self.repository,
            self.storage,
            self.event_bus,
            self.processors,
            self.runtime_config,
        )
        self.tasks.register("generate_thumbnail", self.uploads.generate_thumbnail)
        self.event_bus.subscribe(MediaUploaded, self._enqueue_thumbnail)
        register_audit_listeners(self.event_bus, self.audit)

    def _build_task_queue(self) -> TaskQueue:
        context = TaskContext(self.repository, self.storage, self.processors)
        if self.settings.task_queue_mode == "sync":
            return SyncTaskQueue(context)
        return AsyncTaskQueue(context, worker_count=self.settings.thumbnail_worker_count)

    async def start(self) -> None:
        await self.tasks.start()
        await self.recover_thumbnails(include_failed=False)

    async def stop(self) -> None:
        await self.tasks.stop()

    async def _enqueue_thumbnail(self, event: MediaUploaded) -> None:
        await self.tasks.enqueue(
            "generate_thumbnail",
            queue="thumbnails",
            media_id=event.media_id,
            correlation_id=event.correlation_id,
        )

    async def recover_thumbnails(self, *, include_failed: bool) -> int:
        recoverable = await self.repository.find_pending_thumbnails()
        if include_failed:
            recoverable.extend(await self.repository.find_failed_thumbnails())
        seen: set[str] = set()
        enqueued = 0
        for media in recoverable:
            if media.id in seen:
                continue
            seen.add(media.id)
            if include_failed and media.thumb_status == "failed":
                media.thumb_status = "pending"
                await self.repository.update_media(media)
            await self.tasks.enqueue(
                "generate_thumbnail",
                queue="thumbnails",
                media_id=media.id,
                correlation_id=f"recovery-{media.id}",
            )
            enqueued += 1
        return enqueued


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    app.state.imghost = AppState(settings)
    await app.state.imghost.start()
    yield
    await app.state.imghost.stop()


app = FastAPI(title="imghost V1", lifespan=lifespan)


class AlbumPatchRequest(BaseModel):
    title: str | None = None
    cover_media_id: str | None = None


class AlbumOrderItem(BaseModel):
    media_id: str
    position: int


class AdminUserCreateRequest(BaseModel):
    username: str
    email: str
    password: str | None = None
    is_admin: bool = False
    quota_bytes: int | None = None


class AdminUserPatchRequest(BaseModel):
    suspended: bool | None = None
    quota_bytes: int | None = None
    password: str | None = None


class UserPasswordPatchRequest(BaseModel):
    current_password: str
    new_password: str


class LoginRequest(BaseModel):
    login: str
    password: str
    remember_me: bool = True


class RegistrationRequest(BaseModel):
    username: str
    email: str
    password: str
    remember_me: bool = True


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


def thumb_format(item: Any) -> str:
    if item.thumb_is_orig or not item.thumb_key:
        return item.format
    suffix = item.thumb_key.rsplit(".", 1)[-1].lower()
    return suffix


def thumb_media_type(item: Any) -> str:
    fmt = thumb_format(item)
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "png": "image/png",
        "gif": "image/gif",
        "bmp": "image/bmp",
    }.get(fmt, item.mime_type)


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
    path = f"{base_url}/api/v1/album/{album.id}/delete"
    if not album.delete_token:
        return path
    query = urlencode({"delete_token": album.delete_token})
    return f"{path}?{query}"


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
        "cover_media_id": album.cover_media_id,
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
                "thumb_url": thumb_url(base_url, item.id, thumb_format(item)),
                "position": item.position,
                "file_size": item.file_size,
                "thumb_status": item.thumb_status,
                "codec_hint": item.codec_hint,
                "compat_warning": compatibility_warning(item),
            }
            for item in media_items
        ],
    }


@dataclass
class ResolvedPrincipal:
    user: User
    raw_api_key: str | None = None


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(f"{data}{padding}")


def create_session_token(settings: Settings, user: User, *, remember_me: bool) -> tuple[str, datetime | None]:
    created_at = utcnow().replace(microsecond=0)
    expires_at = None
    if remember_me:
        expires_at = created_at + timedelta(days=settings.session_remember_days)
    payload = {
        "user_id": user.id,
        "created_at": created_at.isoformat(),
        "expires_at": expires_at.isoformat() if expires_at else None,
    }
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(settings.secret_key.encode("utf-8"), payload_bytes, sha256).hexdigest()
    return f"{_b64encode(payload_bytes)}.{signature}", expires_at


def resolve_session_user(settings: Settings, token: str) -> str | None:
    payload_b64, dot, signature = token.partition(".")
    if not dot or not payload_b64 or not signature:
        return None
    try:
        payload_bytes = _b64decode(payload_b64)
    except Exception:
        return None
    expected = hmac.new(settings.secret_key.encode("utf-8"), payload_bytes, sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except json.JSONDecodeError:
        return None
    expires_at_raw = payload.get("expires_at")
    if expires_at_raw:
        try:
            expires_at = datetime.fromisoformat(expires_at_raw)
        except ValueError:
            return None
        if expires_at <= utcnow():
            return None
    user_id = payload.get("user_id")
    return user_id if isinstance(user_id, str) and user_id else None


def apply_session_cookie(response: Response, settings: Settings, token: str, *, expires_at: datetime | None) -> None:
    max_age = None
    if expires_at is not None:
        max_age = max(1, int((expires_at - utcnow()).total_seconds()))
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=max_age,
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(key=settings.session_cookie_name, httponly=True, samesite="lax", secure=False)


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
        user_id = resolve_session_user(state.settings, session_token)
        if user_id:
            user = await state.repository.get_user(user_id)
            if user is None:
                raise HTTPException(status_code=401, detail="Invalid session.")
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


def compatibility_warning(item: Any) -> str | None:
    if item.codec_hint == "hevc":
        return "This video uses HEVC encoding and may not play in Firefox. Try Chrome or Safari."
    if item.codec_hint == "vp9" and item.format == "webm":
        return "This video may not play in older Safari. Try Chrome or Firefox."
    return None


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
    user = await authenticated_user(request, required=False)
    if user is None and not await state.runtime_config.get_value("anon_upload_enabled"):
        raise HTTPException(status_code=403, detail="Anonymous uploads are disabled.")
    actor = CurrentActor(user=user, source="api" if user else "web")
    if user is not None:
        if len(file) != 1:
            raise HTTPException(status_code=400, detail="API key uploads must contain exactly one file.")
        if album_id is not None:
            raise HTTPException(status_code=400, detail="API key uploads always create a new album.")
    results = []
    active_album_id = album_id
    for item in file:
        result = await state.uploads.upload(item, active_album_id, title, cid, actor=actor)
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
                "thumb_url": thumb_url(state.settings.base_url, result.media.id, thumb_format(result.media)),
                "thumb_status": result.media.thumb_status,
            }
            for result in results
        ],
    }
    headers = {"X-Correlation-ID": cid}
    return JSONResponse(payload, headers=headers)


@app.post("/api/v1/auth/login")
async def login(request: Request, payload: LoginRequest) -> JSONResponse:
    state = get_state(request)
    cid = correlation_id(request)
    user = await state.uploads.authenticate_local_user(
        LocalLoginInput(login=payload.login, password=payload.password)
    )
    token, expires_at = create_session_token(state.settings, user, remember_me=payload.remember_me)
    summary = await state.uploads.get_current_user_summary(user)
    response = JSONResponse({"authenticated": True, "user": summary}, headers={"X-Correlation-ID": cid})
    apply_session_cookie(response, state.settings, token, expires_at=expires_at)
    return response


@app.post("/api/v1/auth/register")
async def register(request: Request, payload: RegistrationRequest) -> JSONResponse:
    state = get_state(request)
    cid = correlation_id(request)
    if not await state.runtime_config.get_value("allow_registration"):
        raise HTTPException(status_code=403, detail="Registration is disabled.")
    created = await state.uploads.create_user(
        UserCreateInput(
            username=payload.username,
            email=payload.email,
            password=payload.password,
            is_admin=False,
            quota_bytes=None,
        ),
        method="registration",
        correlation_id=cid,
        source="web",
    )
    token, expires_at = create_session_token(state.settings, created, remember_me=payload.remember_me)
    summary = await state.uploads.get_current_user_summary(created)
    response = JSONResponse({"authenticated": True, "user": summary}, headers={"X-Correlation-ID": cid})
    apply_session_cookie(response, state.settings, token, expires_at=expires_at)
    return response


@app.post("/api/v1/auth/logout")
async def logout(request: Request) -> JSONResponse:
    response = JSONResponse({"authenticated": False}, headers={"X-Correlation-ID": correlation_id(request)})
    clear_session_cookie(response, get_state(request).settings)
    return response


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
    compat_warnings = [warning for warning in dict.fromkeys(compatibility_warning(item) for item in items) if warning]
    cards = []
    for item in items:
        preview_url = thumb_url(state.settings.base_url, item.id, item.format)
        preview_url = thumb_url(state.settings.base_url, item.id, thumb_format(item))
        if item.media_type == "video":
            poster_attr = f' poster="{preview_url}"' if item.thumb_status == "done" else ""
            media_tag = f'<video controls preload="metadata" src="{media_url(state.settings.base_url, item.id, item.format)}"{poster_attr}></video>'
        else:
            if item.thumb_status == "done":
                media_tag = f'<img src="{preview_url}" alt="{item.filename_orig}">'
            elif item.thumb_status == "failed":
                media_tag = '<div class="placeholder">Thumbnail failed</div>'
            else:
                media_tag = f'<img data-thumb-src="{preview_url}" data-media-id="{item.id}" data-thumb-status="{item.thumb_status}" alt="{item.filename_orig}">'
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
        {''.join(f'<p class="banner">{warning}</p>' for warning in compat_warnings)}
        <p class="actions"><a href="/api/v1/album/{album.id}/zip">Download as ZIP</a></p>
      </section>
      <section class="grid">
        {''.join(cards)}
      </section>
    </main>
    <script>
      const pending = document.querySelectorAll('img[data-thumb-status="pending"], img[data-thumb-status="processing"]');
      for (const img of pending) {{
        const poll = async () => {{
          try {{
            const response = await fetch(img.dataset.thumbSrc, {{ method: 'GET', cache: 'no-store' }});
            if (response.status === 200) {{
              img.removeAttribute('data-thumb-status');
              img.src = img.dataset.thumbSrc;
              return;
            }}
            if (response.status === 202) {{
              setTimeout(poll, 1000);
              return;
            }}
            img.outerHTML = '<div class="placeholder">Thumbnail failed</div>';
          }} catch {{
            setTimeout(poll, 1500);
          }}
        }};
        poll();
      }}
    </script>
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
    if thumb and media.thumb_status in {"pending", "processing"}:
        return StreamingResponse(iter(()), status_code=202)
    if thumb and media.thumb_status == "failed":
        raise HTTPException(status_code=404)
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
        media_type=thumb_media_type(media) if thumb else media.mime_type,
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


@app.get("/api/v1/album/{album_id}/delete")
async def delete_album_via_get(request: Request, album_id: str, delete_token: str | None = None) -> JSONResponse:
    return await delete_album(request, album_id, delete_token)


@app.get("/api/v1/user/me")
async def get_current_user(request: Request) -> JSONResponse:
    state = get_state(request)
    user = await authenticated_user(request, required=True)
    summary = await state.uploads.get_current_user_summary(user)
    return JSONResponse(summary, headers={"X-Correlation-ID": correlation_id(request)})


@app.post("/api/v1/user/me/api-key")
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


@app.patch("/api/v1/user/me/password")
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


@app.get("/api/v1/user/me/sharex-config")
async def download_sharex_config(request: Request) -> Response:
    principal = await authenticated_principal(request, required=True)
    if principal.raw_api_key is None:
        raise HTTPException(status_code=400, detail="ShareX config download requires API key authentication.")
    state = get_state(request)
    payload = {
        "Version": "14.1.0",
        "Name": "imghost",
        "DestinationType": "ImageUploader, FileUploader",
        "RequestMethod": "POST",
        "RequestURL": f"{state.settings.base_url}/api/v1/upload",
        "Headers": {
            "Authorization": f"Bearer {principal.raw_api_key}",
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


@app.delete("/api/v1/user/me")
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


@app.get("/api/v1/admin/users")
async def admin_list_users(request: Request) -> JSONResponse:
    state = get_state(request)
    await require_admin_user(request)
    payload = await state.uploads.list_users_with_usage()
    return JSONResponse(payload, headers={"X-Correlation-ID": correlation_id(request)})


@app.get("/api/v1/admin/albums")
async def admin_list_albums(request: Request) -> JSONResponse:
    state = get_state(request)
    await require_admin_user(request)
    payload = await state.uploads.list_albums_for_admin()
    return JSONResponse(payload, headers={"X-Correlation-ID": correlation_id(request)})


@app.get("/api/v1/admin/audit")
async def admin_list_audit(
    request: Request,
    event_type: str | None = None,
    actor_id: str | None = None,
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
        correlation_id=correlation_id_filter,
        after=after,
        before=before,
        limit=limit,
        offset=offset,
    )
    return JSONResponse([event.to_dict() for event in events], headers={"X-Correlation-ID": correlation_id(request)})


@app.get("/api/v1/admin/config")
async def admin_get_config(request: Request) -> JSONResponse:
    state = get_state(request)
    await require_admin_user(request)
    payload = await state.runtime_config.list_effective()
    return JSONResponse({key: value.to_dict() for key, value in payload.items()}, headers={"X-Correlation-ID": correlation_id(request)})


@app.patch("/api/v1/admin/config")
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


@app.post("/api/v1/admin/users")
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
        },
        status_code=201,
        headers={"X-Correlation-ID": cid},
    )


@app.patch("/api/v1/admin/users/{user_id}")
async def admin_patch_user(request: Request, user_id: str, payload: AdminUserPatchRequest) -> JSONResponse:
    state = get_state(request)
    cid = correlation_id(request)
    admin = await require_admin_user(request)
    updated = await state.uploads.update_user(
        user_id,
        payload=UserUpdateInput(
            suspended=payload.suspended if "suspended" in payload.model_fields_set else None,
            quota_bytes=payload.quota_bytes if "quota_bytes" in payload.model_fields_set else UNSET,
            password=payload.password if "password" in payload.model_fields_set else None,
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
        },
        headers={"X-Correlation-ID": cid},
    )


@app.delete("/api/v1/admin/users/{user_id}")
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


@app.patch("/api/v1/admin/albums/{album_id}")
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
    return JSONResponse(album_to_payload(state.settings.base_url, updated, items), headers={"X-Correlation-ID": cid})


@app.delete("/api/v1/admin/albums/{album_id}")
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


@app.get("/api/v1/admin/stats")
async def admin_stats(request: Request) -> JSONResponse:
    state = get_state(request)
    await require_admin_user(request)
    payload = await state.uploads.global_storage_stats()
    return JSONResponse(payload, headers={"X-Correlation-ID": correlation_id(request)})


@app.patch("/api/v1/album/{album_id}")
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
    album, items = await state.uploads.update_album(
        album_id,
        delete_token,
        cid,
        title=payload.title if "title" in payload.model_fields_set else UNSET,
        cover_media_id=payload.cover_media_id if "cover_media_id" in payload.model_fields_set else UNSET,
    )
    return JSONResponse(album_to_payload(state.settings.base_url, album, items), headers={"X-Correlation-ID": cid})


@app.patch("/api/v1/album/{album_id}/order")
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
    album, media_items = await state.uploads.reorder_album(
        album_id,
        delete_token,
        [(item.media_id, item.position) for item in items],
        cid,
    )
    return JSONResponse(album_to_payload(state.settings.base_url, album, media_items), headers={"X-Correlation-ID": cid})


@app.delete("/api/v1/media/{media_id}")
async def delete_media(request: Request, media_id: str, delete_token: str | None = None) -> JSONResponse:
    if not is_valid_id(media_id, MEDIA_ID_LENGTH):
        raise HTTPException(status_code=404)
    state = get_state(request)
    cid = correlation_id(request)
    result = await state.uploads.delete_media(media_id, delete_token, cid)
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


@app.get("/health/live")
async def health_live() -> PlainTextResponse:
    return PlainTextResponse("ok")


@app.get("/health/ready")
async def health_ready(request: Request) -> JSONResponse:
    state = get_state(request)
    ready = await state.storage.health_check()
    return JSONResponse({"ok": ready}, status_code=200 if ready else 503)
