from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
import json
import logging
from hashlib import sha256
from math import ceil
from pathlib import Path
from typing import Any
from uuid import uuid4
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pydantic import BaseModel

from .audit import PostgresAuditLog, register_audit_listeners
from .config import Settings, load_settings
from .db import Database
from .events import AdminLoggedIn, ConfigChanged, EventBus, MediaUploaded
from .ids import ALBUM_ID_LENGTH, MEDIA_ID_LENGTH, is_valid_id
from .processors import ProcessorRegistry, build_processor_registry
from .public_origin import public_base_url
from .rate_limits import build_rate_limiter, hash_anon_identity
from .redis_support import RedisHandle
from .repositories import PostgresRepository
from .models import User, utcnow
from .observability import ObservabilityState
from .runtime_config import PostgresRuntimeConfig
from .sessions import SessionBackend, build_session_backend
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
from .storage import build_storage_backend
from .tasks import AsyncTaskQueue, RedisTaskQueue, SyncTaskQueue, TaskContext, TaskQueue


class AppState:
    def __init__(self, settings: Settings, *, run_task_worker: bool | None = None) -> None:
        self.settings = settings
        self.run_task_worker = settings.task_worker_enabled if run_task_worker is None else run_task_worker
        self.database = Database(settings.database_url)
        self.observability = ObservabilityState()
        self.event_bus = EventBus()
        self.repository = PostgresRepository(self.database)
        self.audit = PostgresAuditLog(self.database)
        self.runtime_config = PostgresRuntimeConfig(self.database)
        self.redis = RedisHandle(settings)
        self.session_backend: SessionBackend = build_session_backend(settings, self.redis, self.observability)
        self.rate_limiter = build_rate_limiter(self.runtime_config, self.redis, self.observability)
        self.storage = build_storage_backend(settings)
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
            self.rate_limiter,
        )
        self.tasks.register("generate_thumbnail", self.uploads.generate_thumbnail)
        self.event_bus.subscribe(MediaUploaded, self._enqueue_thumbnail)
        register_audit_listeners(self.event_bus, self.audit)

    def _build_task_queue(self) -> TaskQueue:
        context = TaskContext(self.repository, self.storage, self.processors)
        if self.settings.task_queue_mode == "sync":
            return SyncTaskQueue(context)
        if self.settings.task_queue_mode == "redis" and self.redis.enabled:
            return RedisTaskQueue(
                self.redis,
                context,
                self.observability,
                worker_count=self.settings.thumbnail_worker_count,
                run_worker=self.run_task_worker,
            )
        return AsyncTaskQueue(context, worker_count=self.settings.thumbnail_worker_count)

    async def start(self) -> None:
        await self.database.connect()
        await self.redis.ensure_startup_ready()
        await self.tasks.start()
        await self.recover_thumbnails(include_failed=False)

    async def stop(self) -> None:
        await self.tasks.stop()
        await self.redis.close()
        await self.database.close()

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
        if enqueued:
            logging.getLogger(__name__).info(
                "thumbnail_recovery_enqueued",
                extra={"count": enqueued, "include_failed": include_failed},
            )
        return enqueued

    async def runtime_status(self) -> dict[str, Any]:
        database_ok = False
        try:
            pool = self.database.require_pool()
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            database_ok = True
        except Exception:
            database_ok = False
        storage_ok = await self.storage.health_check()
        redis_reachable = await self.redis.ping()
        redis_configured = self.redis.enabled
        tasks_configured = redis_configured and self.settings.task_queue_mode == "redis"
        return {
            "database": {"ok": database_ok},
            "storage": {"ok": storage_ok},
            "redis": {
                "configured": redis_configured,
                "reachable": redis_reachable,
                "subsystems": {
                    "sessions": self.observability.subsystem_snapshot(
                        "sessions",
                        configured=redis_configured,
                        default_mode="redis" if redis_configured else "disabled",
                    ),
                    "rate_limits": self.observability.subsystem_snapshot(
                        "rate_limits",
                        configured=redis_configured,
                        default_mode="redis" if redis_configured else "disabled",
                    ),
                    "tasks": self.observability.subsystem_snapshot(
                        "tasks",
                        configured=tasks_configured,
                        default_mode="redis" if tasks_configured else "fallback",
                    ),
                },
            },
            "worker": {
                "enabled_in_this_process": self.run_task_worker,
                "last_started_at": self.observability.last_worker_started_at,
                "last_stopped_at": self.observability.last_worker_stopped_at,
                "last_task_failure_at": self.observability.last_task_failure_at,
                "last_task_failure": self.observability.last_task_failure,
            },
            "tasks": {
                "mode": self.settings.task_queue_mode,
                **(await self.tasks.runtime_status()),
            },
            "trusted_public_origins": list(self.settings.trusted_public_origins),
            "forwarded_headers_policy": "trusted_proxies_only" if self.settings.trusted_proxy_cidrs_enabled else "permissive",
            "trusted_proxy_cidrs_enabled": self.settings.trusted_proxy_cidrs_enabled,
            "trusted_proxy_cidrs": list(self.settings.trusted_proxy_cidrs),
        }


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    app.state.imghost = AppState(settings)
    await app.state.imghost.start()
    yield
    await app.state.imghost.stop()


app = FastAPI(title="imghost V1", lifespan=lifespan)
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.middleware("http")
async def clear_stale_session_cookie(request: Request, call_next):
    request.state.clear_session_cookie = False
    response = await call_next(request)
    if getattr(request.state, "clear_session_cookie", False):
        clear_session_cookie(response, get_state(request).settings)
        logging.getLogger(__name__).info("session_cookie_cleared", extra={"path": request.url.path})
    return response


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


def client_ip(request: Request) -> str:
    for header_name in ("CF-Connecting-IP", "X-Real-IP", "X-Forwarded-For"):
        value = request.headers.get(header_name)
        if not value:
            continue
        if header_name == "X-Forwarded-For":
            return value.split(",", 1)[0].strip()
        return value.strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def upload_rate_limit_key(request: Request, user: User | None) -> str:
    if user is not None:
        return user.id
    return hash_anon_identity(client_ip(request), request.headers.get("User-Agent", ""))


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


def album_delete_url(base_url: str, album: Any, *, include_token: bool = False) -> str | None:
    path = f"{base_url}/api/v1/album/{album.id}/delete"
    if not album.delete_token or not include_token:
        return path
    query = urlencode({"delete_token": album.delete_token})
    return f"{path}?{query}"


def album_manage_url(base_url: str, album: Any, *, include_token: bool = False) -> str:
    path = f"{base_url}/manage/{album.id}"
    if not album.delete_token or not include_token:
        return path
    query = urlencode({"token": album.delete_token})
    return f"{path}?{query}"


def resolve_cover_media(album: Any, media_items: list[Any]) -> Any | None:
    if album.cover_media_id:
        for item in media_items:
            if item.id == album.cover_media_id:
                return item
    return media_items[0] if media_items else None


def album_to_payload(
    base_url: str,
    album: Any,
    media_items: list[Any],
    *,
    include_delete_token: bool = False,
) -> dict[str, Any]:
    cover = resolve_cover_media(album, media_items)
    return {
        "id": album.id,
        "title": album.title,
        "cover_media_id": album.cover_media_id,
        "created_at": album.created_at.isoformat(),
        "updated_at": album.updated_at.isoformat(),
        "expires_at": album.expires_at.isoformat() if album.expires_at else None,
        "delete_url": album_delete_url(base_url, album, include_token=include_delete_token),
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


@dataclass
class PageRuntimeFlags:
    allow_registration: bool
    anon_upload_enabled: bool
    anon_expiry_hours: int

def apply_session_cookie(response: Response, settings: Settings, token: str, *, expires_at: datetime | None) -> None:
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


def compatibility_warning(item: Any) -> str | None:
    if item.codec_hint == "hevc":
        return "This video uses HEVC encoding and may not play in Firefox. Try Chrome or Safari."
    if item.codec_hint == "vp9" and item.format == "webm":
        return "This video may not play in older Safari. Try Chrome or Firefox."
    return None


def humanize_bytes(byte_count: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(byte_count)
    unit = units[0]
    for candidate in units:
        unit = candidate
        if size < 1024.0 or candidate == units[-1]:
            break
        size /= 1024.0
    if unit == "B":
        return f"{int(size)} {unit}"
    return f"{size:.1f} {unit}"


def display_timestamp(value: str) -> str:
    return datetime.fromisoformat(value).strftime("%-m/%-d/%Y, %-I:%M:%S %p")


def nav_items(user: User | None, *, allow_registration: bool) -> list[dict[str, str]]:
    links: list[tuple[str, str]] = []
    if user is None:
        links.append(("/login", "Login"))
        if allow_registration:
            links.append(("/register", "Register"))
        links.append(("/album-tools", "Album Tools"))
    else:
        links.extend(
            [
                ("/dashboard", "Dashboard"),
                ("/albums", "Albums"),
                ("/settings", "Settings"),
                ("/album-tools", "Album Tools"),
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
    )


async def build_page_context(request: Request, *, user: User | None = None) -> dict[str, Any]:
    flags = await runtime_flags(request)
    return {
        "current_user": user,
        "nav_items": nav_items(user, allow_registration=flags.allow_registration),
        "public_base_url": public_base_url(request, get_state(request).settings),
        "runtime_flags": flags,
    }


def flash_html() -> str:
    return templates.get_template("partials/flash.html").render()


def with_flash(body: str) -> str:
    return flash_html() + body


def login_redirect(next_path: str | None = None) -> RedirectResponse:
    location = "/login"
    if next_path:
        location = f"{location}?{urlencode({'next': next_path})}"
    return RedirectResponse(url=location, status_code=303)


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


async def page_shell(
    request: Request,
    title: str,
    body: str,
    *,
    user: User | None = None,
    script: str = "",
    extra_context: dict[str, Any] | None = None,
) -> HTMLResponse:
    context = await build_page_context(request, user=user)
    if extra_context:
        context.update(extra_context)
    return templates.TemplateResponse(
        request=request,
        name="base.html",
        context={
            "page_title": title,
            "body_html": Markup(body),
            "script_html": Markup(script),
            **context,
        },
    )


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


@app.get("/", response_class=HTMLResponse)
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


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    user = await authenticated_user(request, required=False)
    if user is not None:
        return RedirectResponse(url="/dashboard", status_code=303)
    next_path = request.query_params.get("next") or "/dashboard"
    return await render_template_page(
        request,
        "pages/login.html",
        "Login",
        extra_context={"next_path": next_path},
        script_paths=["js/auth.js"],
    )


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request) -> HTMLResponse:
    user = await authenticated_user(request, required=False)
    if user is not None:
        return RedirectResponse(url="/dashboard", status_code=303)
    next_path = request.query_params.get("next") or "/dashboard"
    return await render_template_page(
        request,
        "pages/register.html",
        "Register",
        extra_context={"next_path": next_path},
        script_paths=["js/auth.js"],
    )


@app.get("/dashboard", response_class=HTMLResponse)
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


@app.get("/albums", response_class=HTMLResponse)
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


@app.get("/albums/{album_id}", response_class=HTMLResponse)
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


@app.get("/manage/{album_id}", response_class=HTMLResponse)
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


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> str:
    state = get_state(request)
    user_or_redirect = await require_page_user(request)
    if isinstance(user_or_redirect, RedirectResponse):
        return user_or_redirect
    user = user_or_redirect
    session_user = await state.uploads.get_current_user_summary(user)
    bootstrap = json.dumps({"session_user": session_user})
    body = """
      <section id="settings-unauth" class="card">
        <h1>Settings</h1>
        <p>Sign in on the home page to manage your password, API key, ShareX config, and account settings.</p>
      </section>
      <section id="settings-auth" class="stack hidden">
        <section class="grid">
          <section class="card">
            <h1>Settings</h1>
            <p>Manage your account, API key, and ShareX integration from one place.</p>
            <pre id="settings-account-summary" class="result"></pre>
          </section>
          <section class="card">
            <h2>API Key</h2>
            <p class="hint">Raw API keys are stored hash-only server-side, so revealing a key or downloading ShareX config from a browser session rotates the key and invalidates the previous value.</p>
            <div class="row">
              <button id="reveal-api-key" type="button">Rotate And Reveal API Key</button>
              <button id="download-sharex-settings" type="button" class="secondary">Download ShareX Config</button>
            </div>
            <pre id="settings-api-key-output" class="result hidden"></pre>
          </section>
        </section>
        <section class="grid">
          <section class="card">
            <h2>Password</h2>
            <form id="settings-password-form">
              <input type="password" name="current_password" placeholder="Current password" required>
              <input type="password" name="new_password" placeholder="New password" required>
              <button type="submit">Change Password</button>
            </form>
          </section>
          <section class="card">
            <h2>Danger Zone</h2>
            <form id="settings-delete-account-form">
              <button type="submit" class="danger">Delete My Account</button>
            </form>
          </section>
        </section>
      </section>
    """
    script = f"""
    <script>
      const boot = {bootstrap};
      const state = {{
        user: boot.session_user,
        latestApiKey: null,
      }};
      const flash = document.getElementById("flash");
      const unauth = document.getElementById("settings-unauth");
      const auth = document.getElementById("settings-auth");
      const accountSummary = document.getElementById("settings-account-summary");
      const apiKeyOutput = document.getElementById("settings-api-key-output");

      const showMessage = (message) => {{
        flash.textContent = message || "";
      }};
      const renderState = () => {{
        const isAuthed = !!state.user;
        unauth.classList.toggle("hidden", isAuthed);
        auth.classList.toggle("hidden", !isAuthed);
        if (!isAuthed) {{
          accountSummary.textContent = "";
          return;
        }}
        accountSummary.textContent = JSON.stringify(state.user, null, 2);
      }};
      const requestJson = async (url, options = {{}}) => {{
        const response = await fetch(url, options);
        const data = await response.json().catch(() => ({{}}));
        if (!response.ok) {{
          throw new Error(data.detail || `Request failed (${{response.status}}).`);
        }}
        return data;
      }};
      const triggerDownload = (payload) => {{
        const blob = new Blob([JSON.stringify(payload, null, 2)], {{ type: "application/json" }});
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = "imghost.sxcu";
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        URL.revokeObjectURL(url);
      }};
      const refreshUser = async () => {{
        try {{
          state.user = await requestJson("/api/v1/user/me");
        }} catch {{
          state.user = null;
        }}
        renderState();
      }};
      const rotateAndRevealApiKey = async (message) => {{
        const issued = await requestJson("/api/v1/user/me/api-key", {{ method: "POST" }});
        state.latestApiKey = issued.api_key;
        apiKeyOutput.classList.remove("hidden");
        apiKeyOutput.textContent = JSON.stringify(issued, null, 2);
        await refreshUser();
        showMessage(message);
        return issued;
      }};

      document.getElementById("reveal-api-key")?.addEventListener("click", async () => {{
        try {{
          await rotateAndRevealApiKey("API key rotated and revealed.");
        }} catch (error) {{
          showMessage(error.message);
        }}
      }});

      document.getElementById("download-sharex-settings")?.addEventListener("click", async () => {{
        try {{
          const response = await fetch("/api/v1/user/me/sharex-config");
          const data = await response.json().catch(() => ({{}}));
          if (!response.ok) {{
            throw new Error(data.detail || "ShareX download failed.");
          }}
          triggerDownload(data);
          await refreshUser();
          showMessage("ShareX config downloaded. Browser-session download rotates the API key before embedding it.");
        }} catch (error) {{
          showMessage(error.message);
        }}
      }});

      document.getElementById("settings-password-form")?.addEventListener("submit", async (event) => {{
        event.preventDefault();
        try {{
          await requestJson("/api/v1/user/me/password", {{
            method: "PATCH",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify(Object.fromEntries(new FormData(event.currentTarget).entries())),
          }});
          event.currentTarget.reset();
          showMessage("Password changed.");
        }} catch (error) {{
          showMessage(error.message);
        }}
      }});

      document.getElementById("settings-delete-account-form")?.addEventListener("submit", async (event) => {{
        event.preventDefault();
        if (!window.confirm("Delete your account and all owned content?")) {{
          return;
        }}
        try {{
          await requestJson("/api/v1/user/me", {{ method: "DELETE" }});
          state.user = null;
          state.latestApiKey = null;
          apiKeyOutput.classList.add("hidden");
          apiKeyOutput.textContent = "";
          renderState();
          showMessage("Account deleted.");
        }} catch (error) {{
          showMessage(error.message);
        }}
      }});

      renderState();
      if (state.user && !state.user.has_api_key) {{
        rotateAndRevealApiKey("No API key existed, so one was issued automatically.");
      }}
    </script>
    """
    return await page_shell(request, "Settings", with_flash(body), user=user, script=script)


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request) -> str:
    state = get_state(request)
    user_or_redirect = await require_page_admin(request)
    if isinstance(user_or_redirect, RedirectResponse):
        return user_or_redirect
    user = user_or_redirect
    session_user = await state.uploads.get_current_user_summary(user)
    bootstrap = json.dumps({"session_user": session_user})
    body = """
      <section class="grid">
        <section class="card">
          <h1>Admin Dashboard</h1>
          <p>Basic browser UI over the admin APIs: users, albums, runtime config, stats, and audit log.</p>
        </section>
        <section class="card">
          <h2>API Key Mode</h2>
          <form id="admin-api-key-form">
            <input id="admin-api-key-input" type="text" placeholder="Admin API key">
            <div class="row">
              <button type="submit">Use Admin API Key</button>
              <button id="admin-clear-api-key" type="button" class="secondary">Clear</button>
            </div>
          </form>
        </section>
      </section>
      <section id="admin-locked" class="card">
        <h2>Admin Access Needed</h2>
        <p>Sign in as an admin or provide an admin API key.</p>
      </section>
      <section id="admin-panel" class="stack hidden">
        <section class="grid">
          <section class="card">
            <h2>Create User</h2>
            <form id="admin-create-user-form">
              <input type="text" name="username" placeholder="Username" required>
              <input type="email" name="email" placeholder="Email" required>
              <input type="password" name="password" placeholder="Password (optional)">
              <div class="row">
                <label class="check"><input type="checkbox" name="is_admin"> Admin</label>
                <input type="number" name="quota_bytes" placeholder="Quota bytes">
                <input type="number" name="rate_limit_rpm" placeholder="RPM override">
                <input type="number" name="rate_limit_bph" placeholder="BPH override">
              </div>
              <button type="submit">Create User</button>
            </form>
          </section>
          <section class="card">
            <div class="row">
              <h2>Stats</h2>
              <button id="refresh-admin-stats" type="button">Refresh Stats</button>
            </div>
            <pre id="admin-stats" class="result"></pre>
          </section>
        </section>
        <section class="card">
          <div class="row">
            <h2>Runtime Config</h2>
            <button id="refresh-admin-config" type="button">Refresh Config</button>
          </div>
          <form id="admin-config-form"></form>
          <pre id="admin-config-json" class="result"></pre>
        </section>
        <section class="card">
          <div class="row">
            <h2>Users</h2>
            <button id="refresh-admin-users" type="button">Refresh Users</button>
          </div>
          <div id="admin-users" class="stack"></div>
        </section>
        <section class="card">
          <div class="row">
            <h2>Albums</h2>
            <button id="refresh-admin-albums" type="button">Refresh Albums</button>
          </div>
          <div id="admin-albums" class="stack"></div>
        </section>
        <section class="card">
          <h2>Audit Log</h2>
          <form id="admin-audit-form">
            <div class="row">
              <input type="text" name="event_type" placeholder="event_type">
              <input type="text" name="actor_id" placeholder="actor_id">
              <input type="text" name="user_id" placeholder="user_id">
              <input type="text" name="correlation_id" placeholder="correlation_id">
            </div>
            <div class="row">
              <input type="datetime-local" name="after">
              <input type="datetime-local" name="before">
              <input type="number" name="limit" placeholder="limit" value="100">
              <input type="number" name="offset" placeholder="offset" value="0">
            </div>
            <button type="submit">Query Audit</button>
          </form>
          <pre id="admin-audit" class="result"></pre>
        </section>
      </section>
    """
    script = f"""
    <script>
      const boot = {bootstrap};
      const state = {{
        apiKey: window.localStorage.getItem("imghost_admin_api_key") || "",
        user: boot.session_user,
        config: null,
      }};
      const flash = document.getElementById("flash");
      const lockPanel = document.getElementById("admin-locked");
      const adminPanel = document.getElementById("admin-panel");
      const usersRoot = document.getElementById("admin-users");
      const albumsRoot = document.getElementById("admin-albums");
      const statsRoot = document.getElementById("admin-stats");
      const auditRoot = document.getElementById("admin-audit");
      const configForm = document.getElementById("admin-config-form");
      const configJson = document.getElementById("admin-config-json");
      const apiKeyInput = document.getElementById("admin-api-key-input");
      apiKeyInput.value = state.apiKey;

      const showMessage = (message) => {{ flash.textContent = message || ""; }};
      const escapeHtml = (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
      const authHeaders = (headers = {{}}) => {{
        const resolved = new Headers(headers);
        if (state.apiKey) resolved.set("Authorization", `Bearer ${{state.apiKey}}`);
        return resolved;
      }};
      const requestJson = async (url, options = {{}}) => {{
        const response = await fetch(url, {{ ...options, headers: authHeaders(options.headers || {{}}) }});
        const data = await response.json().catch(() => ({{}}));
        if (!response.ok) throw new Error(data.detail || `Request failed (${{response.status}}).`);
        return data;
      }};
      const parseOptionalNumber = (value) => value === "" ? null : Number(value);
      const parseOptionalDate = (value) => value === "" ? null : new Date(value).toISOString();
      const renderAccess = () => {{
        const isAdmin = !!(state.user && state.user.is_admin);
        lockPanel.classList.toggle("hidden", isAdmin);
        adminPanel.classList.toggle("hidden", !isAdmin);
      }};
      const refreshContext = async () => {{
        try {{
          state.user = await requestJson("/api/v1/user/me");
        }} catch {{
          state.user = null;
        }}
        renderAccess();
        if (state.user && state.user.is_admin) {{
          await Promise.all([refreshUsers(), refreshAlbums(), refreshStats(), refreshConfig(), refreshAudit()]);
        }}
      }};
      const refreshUsers = async () => {{
        const users = await requestJson("/api/v1/admin/users");
        usersRoot.innerHTML = users.map((user) => `
          <section class="user-card" data-user-id="${{user.id}}">
            <h3>${{escapeHtml(user.username)}}${{user.is_admin ? " (admin)" : ""}}</h3>
            <p class="hint">${{escapeHtml(user.email)}} · suspended=${{user.suspended}} · storage=${{user.storage_used_bytes}} · media=${{user.media_count}}</p>
            <form class="admin-user-patch-form">
              <div class="row">
                <label class="check"><input type="checkbox" name="suspended" ${{user.suspended ? "checked" : ""}}> Suspended</label>
                <input type="number" name="quota_bytes" placeholder="Quota bytes" value="${{user.quota_bytes ?? ""}}">
                <input type="number" name="rate_limit_rpm" placeholder="RPM override" value="${{user.rate_limit_rpm ?? ""}}">
                <input type="number" name="rate_limit_bph" placeholder="BPH override" value="${{user.rate_limit_bph ?? ""}}">
              </div>
              <button type="submit">Patch User</button>
            </form>
            <form class="admin-user-reset-form">
              <input type="password" name="new_password" placeholder="New password" required>
              <button type="submit" class="secondary">Reset Password</button>
            </form>
            <button type="button" class="danger admin-user-delete">Delete User</button>
          </section>
        `).join("");
      }};
      const refreshAlbums = async () => {{
        const albums = await requestJson("/api/v1/admin/albums");
        albumsRoot.innerHTML = albums.map((album) => `
          <section class="admin-card" data-album-id="${{album.id}}">
            <h3>${{escapeHtml(album.title || "Untitled album")}}</h3>
            <p class="hint">album=${{album.id}} · owner=${{escapeHtml(album.owner_username || "anonymous")}} · items=${{album.item_count}}</p>
            <form class="admin-album-patch-form">
              <input type="datetime-local" name="expires_at" value="${{album.expires_at ? album.expires_at.slice(0, 16) : ""}}">
              <button type="submit">Set/Clear Expiry</button>
            </form>
            <button type="button" class="danger admin-album-delete">Delete Album</button>
          </section>
        `).join("");
      }};
      const refreshStats = async () => {{
        statsRoot.textContent = JSON.stringify(await requestJson("/api/v1/admin/stats"), null, 2);
      }};
      const renderConfigForm = (config) => {{
        configForm.innerHTML = Object.values(config).map((entry) => {{
          const isBool = typeof entry.value === "boolean";
          return `
            <label>
              <strong>${{escapeHtml(entry.key)}}</strong> <span class="hint">source=${{escapeHtml(entry.source)}}${{entry.locked ? " · locked" : ""}}</span>
              ${{
                isBool
                  ? `<select name="${{entry.key}}" ${{entry.locked ? "disabled" : ""}}>
                       <option value="true" ${{entry.value ? "selected" : ""}}>true</option>
                       <option value="false" ${{!entry.value ? "selected" : ""}}>false</option>
                     </select>`
                  : `<input type="number" name="${{entry.key}}" value="${{entry.value}}" ${{entry.locked ? "disabled" : ""}}>`
              }}
            </label>
          `;
        }}).join("") + '<button type="submit">Save Config</button>';
      }};
      const refreshConfig = async () => {{
        state.config = await requestJson("/api/v1/admin/config");
        renderConfigForm(state.config);
        configJson.textContent = JSON.stringify(state.config, null, 2);
      }};
      const refreshAudit = async () => {{
        const params = new URLSearchParams();
        params.set("limit", "100");
        auditRoot.textContent = JSON.stringify(await requestJson(`/api/v1/admin/audit?${{params.toString()}}`), null, 2);
      }};

      document.getElementById("admin-api-key-form").addEventListener("submit", async (event) => {{
        event.preventDefault();
        state.apiKey = apiKeyInput.value.trim();
        if (state.apiKey) window.localStorage.setItem("imghost_admin_api_key", state.apiKey);
        else window.localStorage.removeItem("imghost_admin_api_key");
        await refreshContext();
        showMessage(state.user && state.user.is_admin ? "Admin authentication active." : "Admin authentication failed.");
      }});
      document.getElementById("admin-clear-api-key").addEventListener("click", async () => {{
        state.apiKey = "";
        apiKeyInput.value = "";
        window.localStorage.removeItem("imghost_admin_api_key");
        await refreshContext();
        showMessage("Admin API key cleared.");
      }});
      document.getElementById("admin-create-user-form").addEventListener("submit", async (event) => {{
        event.preventDefault();
        try {{
          const form = new FormData(event.currentTarget);
          await requestJson("/api/v1/admin/users", {{
            method: "POST",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{
              username: form.get("username"),
              email: form.get("email"),
              password: form.get("password") || null,
              is_admin: form.get("is_admin") === "on",
              quota_bytes: parseOptionalNumber(form.get("quota_bytes")),
              rate_limit_rpm: parseOptionalNumber(form.get("rate_limit_rpm")),
              rate_limit_bph: parseOptionalNumber(form.get("rate_limit_bph")),
            }}),
          }});
          event.currentTarget.reset();
          await refreshUsers();
          showMessage("Admin user created.");
        }} catch (error) {{
          showMessage(error.message);
        }}
      }});
      document.getElementById("refresh-admin-users").addEventListener("click", refreshUsers);
      document.getElementById("refresh-admin-albums").addEventListener("click", refreshAlbums);
      document.getElementById("refresh-admin-stats").addEventListener("click", refreshStats);
      document.getElementById("refresh-admin-config").addEventListener("click", refreshConfig);
      configForm.addEventListener("submit", async (event) => {{
        event.preventDefault();
        try {{
          const form = new FormData(event.currentTarget);
          const payload = {{}};
          for (const [key, value] of form.entries()) {{
            const current = state.config[key];
            payload[key] = typeof current.value === "boolean" ? value === "true" : Number(value);
          }}
          await requestJson("/api/v1/admin/config", {{
            method: "PATCH",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify(payload),
          }});
          await refreshConfig();
          showMessage("Runtime config updated.");
        }} catch (error) {{
          showMessage(error.message);
        }}
      }});
      usersRoot.addEventListener("submit", async (event) => {{
        const card = event.target.closest("[data-user-id]");
        if (!card) return;
        event.preventDefault();
        const userId = card.dataset.userId;
        try {{
          if (event.target.matches(".admin-user-patch-form")) {{
            const form = new FormData(event.target);
            await requestJson(`/api/v1/admin/users/${{userId}}`, {{
              method: "PATCH",
              headers: {{ "Content-Type": "application/json" }},
              body: JSON.stringify({{
                suspended: form.get("suspended") === "on",
                quota_bytes: parseOptionalNumber(form.get("quota_bytes")),
                rate_limit_rpm: parseOptionalNumber(form.get("rate_limit_rpm")),
                rate_limit_bph: parseOptionalNumber(form.get("rate_limit_bph")),
              }}),
            }});
            showMessage("User updated.");
          }} else if (event.target.matches(".admin-user-reset-form")) {{
            const form = new FormData(event.target);
            await requestJson(`/api/v1/admin/users/${{userId}}/reset-password`, {{
              method: "POST",
              headers: {{ "Content-Type": "application/json" }},
              body: JSON.stringify({{ new_password: form.get("new_password") }}),
            }});
            showMessage("Password reset.");
          }}
          await refreshUsers();
        }} catch (error) {{
          showMessage(error.message);
        }}
      }});
      usersRoot.addEventListener("click", async (event) => {{
        const card = event.target.closest("[data-user-id]");
        if (!card || !event.target.matches(".admin-user-delete")) return;
        const userId = card.dataset.userId;
        if (!window.confirm(`Delete user ${{userId}}?`)) return;
        try {{
          await requestJson(`/api/v1/admin/users/${{userId}}`, {{ method: "DELETE" }});
          await refreshUsers();
          showMessage("User deleted.");
        }} catch (error) {{
          showMessage(error.message);
        }}
      }});
      albumsRoot.addEventListener("submit", async (event) => {{
        const card = event.target.closest("[data-album-id]");
        if (!card) return;
        event.preventDefault();
        const albumId = card.dataset.albumId;
        try {{
          const form = new FormData(event.target);
          await requestJson(`/api/v1/admin/albums/${{albumId}}`, {{
            method: "PATCH",
            headers: {{ "Content-Type": "application/json" }},
            body: JSON.stringify({{ expires_at: parseOptionalDate(form.get("expires_at")) }}),
          }});
          await refreshAlbums();
          showMessage("Album admin metadata updated.");
        }} catch (error) {{
          showMessage(error.message);
        }}
      }});
      albumsRoot.addEventListener("click", async (event) => {{
        const card = event.target.closest("[data-album-id]");
        if (!card || !event.target.matches(".admin-album-delete")) return;
        const albumId = card.dataset.albumId;
        if (!window.confirm(`Delete album ${{albumId}}?`)) return;
        try {{
          await requestJson(`/api/v1/admin/albums/${{albumId}}`, {{ method: "DELETE" }});
          await refreshAlbums();
          showMessage("Album deleted.");
        }} catch (error) {{
          showMessage(error.message);
        }}
      }});
      document.getElementById("admin-audit-form").addEventListener("submit", async (event) => {{
        event.preventDefault();
        try {{
          const form = new FormData(event.currentTarget);
          const params = new URLSearchParams();
          for (const [key, value] of form.entries()) {{
            if (value !== "") params.set(key, value);
          }}
          auditRoot.textContent = JSON.stringify(await requestJson(`/api/v1/admin/audit?${{params.toString()}}`), null, 2);
          showMessage("Audit log refreshed.");
        }} catch (error) {{
          showMessage(error.message);
        }}
      }});

      refreshContext();
    </script>
    """
    return await page_shell(request, "Admin", with_flash(body), user=user, script=script)


@app.get("/album-tools", response_class=HTMLResponse)
async def album_tools_page(request: Request) -> str:
    user = await authenticated_user(request, required=False)
    body = """
      <section class="grid">
        <section class="card">
          <h1>Album Tools</h1>
          <p>Manual tester for anonymous and public album operations. Load any album by ID, optionally provide a delete token, then edit metadata, reorder items, delete media, download the zip, or delete the album.</p>
        </section>
        <section class="card">
          <h2>Load Album</h2>
          <form id="album-tools-load-form">
            <input type="text" name="album_id" placeholder="Album ID" required>
            <input type="text" name="delete_token" placeholder="Delete token (optional, required for anonymous mutations)">
            <button type="submit">Load Album</button>
          </form>
        </section>
      </section>
      <section class="card">
        <div id="album-tools-result" class="stack"></div>
      </section>
    """
    script = """
    <script>
      const flash = document.getElementById("flash");
      const root = document.getElementById("album-tools-result");
      let currentAccess = { albumId: null, deleteToken: "" };
      const showMessage = (message) => { flash.textContent = message || ""; };
      const escapeHtml = (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
      const requestJson = async (url, options = {}) => {
        const response = await fetch(url, options);
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || `Request failed (${response.status}).`);
        return data;
      };
      const withToken = (path) => {
        if (!currentAccess.deleteToken) return path;
        const glue = path.includes("?") ? "&" : "?";
        return `${path}${glue}delete_token=${encodeURIComponent(currentAccess.deleteToken)}`;
      };
      const renderAlbum = (album) => {
        const orderValue = album.items.map((item) => `${item.id}:${item.position}`).join("\\n");
        root.innerHTML = `
          <section class="album-card" data-album-id="${album.id}">
            <h2>${escapeHtml(album.title || "Untitled album")}</h2>
            <p class="hint">album=${escapeHtml(album.id)} · <a class="inline-link" href="/a/${album.id}" target="_blank">public page</a> · <a class="inline-link" href="/api/v1/album/${album.id}/zip" target="_blank">zip</a></p>
            <form class="album-edit-form">
              <input type="text" name="title" placeholder="Album title" value="${escapeHtml(album.title || "")}">
              <input type="text" name="cover_media_id" placeholder="Cover media ID (blank to clear)" value="${escapeHtml(album.cover_media_id || "")}">
              <button type="submit">Save Album Metadata</button>
            </form>
            <form class="album-order-form">
              <textarea name="order">${escapeHtml(orderValue)}</textarea>
              <button type="submit" class="secondary">Reorder Album</button>
            </form>
            <div class="item-list">
              ${album.items.map((item) => `
                <div class="item">
                  <p><strong>${escapeHtml(item.filename)}</strong> · ${escapeHtml(item.id)}</p>
                  <p class="hint"><a class="inline-link" href="${item.media_url}" target="_blank">media</a> · <a class="inline-link" href="${item.thumb_url}" target="_blank">thumb</a></p>
                  <button type="button" class="danger media-delete" data-media-id="${item.id}">Delete Media</button>
                </div>
              `).join("")}
            </div>
            <button type="button" class="danger album-delete">Delete Album</button>
          </section>
        `;
      };
      const loadAlbum = async () => {
        const album = await requestJson(`/api/v1/album/${currentAccess.albumId}`);
        renderAlbum(album);
      };
      document.getElementById("album-tools-load-form").addEventListener("submit", async (event) => {
        event.preventDefault();
        const form = new FormData(event.currentTarget);
        currentAccess = {
          albumId: String(form.get("album_id")).trim(),
          deleteToken: String(form.get("delete_token") || "").trim(),
        };
        try {
          await loadAlbum();
          showMessage("Album loaded.");
        } catch (error) {
          root.innerHTML = "";
          showMessage(error.message);
        }
      });
      root.addEventListener("submit", async (event) => {
        const albumId = currentAccess.albumId;
        if (!albumId) return;
        event.preventDefault();
        try {
          if (event.target.matches(".album-edit-form")) {
            const form = new FormData(event.target);
            await requestJson(withToken(`/api/v1/album/${albumId}`), {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                title: form.get("title") || null,
                cover_media_id: form.get("cover_media_id") || null,
              }),
            });
            showMessage("Album metadata updated.");
          } else if (event.target.matches(".album-order-form")) {
            const raw = new FormData(event.target).get("order");
            const payload = String(raw || "").split("\\n").map((line) => line.trim()).filter(Boolean).map((line) => {
              const [media_id, position] = line.split(":");
              return { media_id: media_id.trim(), position: Number(position.trim()) };
            });
            await requestJson(withToken(`/api/v1/album/${albumId}/order`), {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(payload),
            });
            showMessage("Album order updated.");
          }
          await loadAlbum();
        } catch (error) {
          showMessage(error.message);
        }
      });
      root.addEventListener("click", async (event) => {
        const albumId = currentAccess.albumId;
        if (!albumId) return;
        try {
          if (event.target.matches(".album-delete")) {
            if (!window.confirm(`Delete album ${albumId}?`)) return;
            await requestJson(withToken(`/api/v1/album/${albumId}`), { method: "DELETE" });
            root.innerHTML = "";
            showMessage("Album deleted.");
          } else if (event.target.matches(".media-delete")) {
            const mediaId = event.target.dataset.mediaId;
            if (!window.confirm(`Delete media ${mediaId}?`)) return;
            await requestJson(withToken(`/api/v1/media/${mediaId}`), { method: "DELETE" });
            await loadAlbum();
            showMessage("Media deleted.");
          }
        } catch (error) {
          showMessage(error.message);
        }
      });
    </script>
    """
    return await page_shell(request, "Album Tools", with_flash(body), user=user, script=script)


@app.post("/api/v1/upload")
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
    user = await authenticated_user(request, required=False)
    if user is None and not await state.runtime_config.get_value("anon_upload_enabled"):
        raise HTTPException(status_code=403, detail="Anonymous uploads are disabled.")
    actor = CurrentActor(user=user, source="api" if user else "web")
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
        "delete_url": album_delete_url(base_url, primary.album, include_token=True),
        "manage_url": album_manage_url(base_url, primary.album, include_token=True) if primary.album.delete_token else None,
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


@app.post("/api/v1/auth/login")
async def login(request: Request, payload: LoginRequest) -> JSONResponse:
    state = get_state(request)
    cid = correlation_id(request)
    user = await state.uploads.authenticate_local_user(
        LocalLoginInput(login=payload.login, password=payload.password)
    )
    if user.is_admin:
        await state.event_bus.emit(
            AdminLoggedIn(
                admin_id=user.id,
                source="web",
                correlation_id=cid,
            )
        )
    token, expires_at = await state.session_backend.create_session(user, remember_me=payload.remember_me)
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
    token, expires_at = await state.session_backend.create_session(created, remember_me=payload.remember_me)
    summary = await state.uploads.get_current_user_summary(created)
    response = JSONResponse({"authenticated": True, "user": summary}, headers={"X-Correlation-ID": cid})
    apply_session_cookie(response, state.settings, token, expires_at=expires_at)
    return response


@app.post("/api/v1/auth/logout")
async def logout(request: Request) -> JSONResponse:
    state = get_state(request)
    await state.session_backend.clear_session(request.cookies.get(state.settings.session_cookie_name))
    response = JSONResponse({"authenticated": False}, headers={"X-Correlation-ID": correlation_id(request)})
    clear_session_cookie(response, state.settings)
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
    return JSONResponse(album_to_payload(public_base_url(request, state.settings), album, items))


@app.get("/a/{album_id}", response_class=HTMLResponse)
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


@app.get("/u/{username}", response_class=HTMLResponse)
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


@app.get("/api/v1/user/me/albums")
async def get_current_user_albums(request: Request) -> JSONResponse:
    state = get_state(request)
    base_url = public_base_url(request, state.settings)
    user = await authenticated_user(request, required=True)
    albums = await state.repository.list_user_albums(user.id)
    albums.sort(key=lambda album: album.updated_at, reverse=True)
    payload = []
    for album in albums:
        items = await state.repository.list_album_media(album.id)
        payload.append(album_to_payload(base_url, album, items))
    return JSONResponse(payload, headers={"X-Correlation-ID": correlation_id(request)})


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


@app.get("/api/v1/admin/users/{user_id}")
async def admin_get_user(request: Request, user_id: str) -> JSONResponse:
    state = get_state(request)
    await require_admin_user(request)
    payload = await state.uploads.get_user_with_usage_for_admin(user_id)
    return JSONResponse(payload, headers={"X-Correlation-ID": correlation_id(request)})


@app.get("/api/v1/admin/users/{user_id}/stats")
async def admin_get_user_stats(request: Request, user_id: str) -> JSONResponse:
    state = get_state(request)
    await require_admin_user(request)
    payload = await state.uploads.get_user_storage_stats_for_admin(user_id)
    return JSONResponse(payload, headers={"X-Correlation-ID": correlation_id(request)})


@app.get("/api/v1/admin/users/{user_id}/albums")
async def admin_list_user_albums(request: Request, user_id: str) -> JSONResponse:
    state = get_state(request)
    await require_admin_user(request)
    payload = await state.uploads.list_albums_for_user_admin_view(user_id)
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


@app.patch("/api/v1/admin/users/{user_id}")
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


@app.post("/api/v1/admin/users/{user_id}/reset-password")
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
    return JSONResponse(
        album_to_payload(public_base_url(request, state.settings), updated, items),
        headers={"X-Correlation-ID": cid},
    )


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


@app.get("/api/v1/admin/runtime-status")
async def admin_runtime_status(request: Request) -> JSONResponse:
    state = get_state(request)
    await require_admin_user(request)
    payload = await state.runtime_status()
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


@app.delete("/api/v1/media/{media_id}")
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


@app.get("/health/live")
async def health_live() -> PlainTextResponse:
    return PlainTextResponse("ok")


@app.get("/health/ready")
async def health_ready(request: Request) -> JSONResponse:
    state = get_state(request)
    payload = await state.runtime_status()
    ready = payload["database"]["ok"] and payload["storage"]["ok"]
    if state.settings.redis_mode == "required" and payload["redis"]["configured"] and not payload["redis"]["reachable"]:
        ready = False
    payload["ok"] = bool(ready)
    return JSONResponse(payload, status_code=200 if ready else 503)
