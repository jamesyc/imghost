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
from urllib.parse import urlencode, urlsplit

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
    normalized = "jpg" if fmt == "jpeg" else fmt
    ext = f".{normalized}" if normalized else ""
    return f"{base_url}/i/{media_id}{ext}"


def thumb_url(base_url: str, media_id: str, fmt: str) -> str:
    normalized = "jpg" if fmt == "jpeg" else fmt
    ext = f".{normalized}" if normalized else ""
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
    max_upload_bytes: int

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


def flash_html() -> str:
    return templates.get_template("partials/flash.html").render()


def with_flash(body: str) -> str:
    return flash_html() + body


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
    next_path = normalize_next_path(request.query_params.get("next"))
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
    next_path = normalize_next_path(request.query_params.get("next"))
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


@app.get("/admin", response_class=HTMLResponse)
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


@app.get("/admin/users", response_class=HTMLResponse)
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


@app.get("/admin/users/new", response_class=HTMLResponse)
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


@app.get("/admin/albums", response_class=HTMLResponse)
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


@app.get("/admin/config", response_class=HTMLResponse)
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


@app.get("/admin/ops", response_class=HTMLResponse)
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
async def get_current_user_albums(request: Request, limit: int = 10, offset: int = 0) -> JSONResponse:
    state = get_state(request)
    base_url = public_base_url(request, state.settings)
    user = await authenticated_user(request, required=True)
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 200.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be non-negative.")
    albums, total = await state.repository.list_user_albums_page(user.id, limit=limit, offset=offset)
    payload_items = []
    for album in albums:
        album_items = await state.repository.list_album_media(album.id)
        payload_items.append(album_to_payload(base_url, album, album_items))
    return JSONResponse(
        {
            "items": payload_items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(payload_items) < total,
        },
        headers={"X-Correlation-ID": correlation_id(request)},
    )


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
