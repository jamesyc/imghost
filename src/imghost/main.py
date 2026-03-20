from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
import json
import logging
from hashlib import sha256
from math import ceil
from typing import Any
from uuid import uuid4
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
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
        }


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    app.state.imghost = AppState(settings)
    await app.state.imghost.start()
    yield
    await app.state.imghost.stop()


app = FastAPI(title="imghost V1", lifespan=lifespan)


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


def nav_links(user: User | None) -> str:
    links = [
        ("/", "Home"),
        ("/dashboard", "Dashboard"),
        ("/settings", "Settings"),
        ("/album-tools", "Album Tools"),
    ]
    if user is not None and user.is_admin:
        links.append(("/admin", "Admin"))
    return "".join(f'<a class="nav-link" href="{href}">{escape(label)}</a>' for href, label in links)


def page_shell(title: str, body: str, *, user: User | None = None, script: str = "") -> str:
    nav = nav_links(user)
    return f"""
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{escape(title)}</title>
    <style>
      :root {{ color-scheme: light; --bg: #f5efe4; --fg: #14213d; --card: #fffaf2; --accent: #d97706; --line: #eadcc2; }}
      * {{ box-sizing: border-box; }}
      body {{ margin: 0; font-family: Georgia, serif; background: radial-gradient(circle at top, #fff8eb, var(--bg)); color: var(--fg); }}
      main {{ max-width: 1100px; margin: 0 auto; padding: 28px 20px 64px; }}
      nav {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 18px; }}
      .nav-link {{ text-decoration: none; color: var(--fg); background: #fff3dc; border: 1px solid var(--line); border-radius: 999px; padding: 10px 14px; }}
      .card {{ background: var(--card); border: 1px solid var(--line); border-radius: 20px; padding: 24px; box-shadow: 0 12px 30px rgba(20,33,61,.08); }}
      .stack {{ display: grid; gap: 16px; }}
      .grid {{ display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }}
      h1 {{ font-size: 2.6rem; margin: 0 0 12px; }}
      h2, h3 {{ margin: 0 0 12px; }}
      p {{ line-height: 1.5; }}
      form {{ display: grid; gap: 12px; margin-top: 16px; }}
      input, button, textarea, select {{ font: inherit; }}
      input[type="text"], input[type="email"], input[type="password"], input[type="datetime-local"], input[type="number"], input[type="file"], textarea, select {{
        width: 100%; padding: 12px; background: white; border: 1px solid #d4c5a8; border-radius: 12px;
      }}
      textarea {{ min-height: 120px; resize: vertical; }}
      button {{ padding: 12px 16px; border: 0; border-radius: 999px; background: var(--accent); color: white; cursor: pointer; }}
      button.secondary {{ background: #375a7f; }}
      button.danger {{ background: #b42318; }}
      button.ghost {{ background: #8b6b3f; }}
      .row {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }}
      .row > * {{ flex: 1 1 180px; }}
      .hint {{ color: #6b7280; font-size: .95rem; }}
      .flash {{ min-height: 1.4rem; color: #7c5414; }}
      .hidden {{ display: none !important; }}
      .check {{ display: flex; gap: 8px; align-items: center; font-size: .95rem; color: #6b7280; }}
      .result, pre {{ background: #fff; border: 1px solid var(--line); border-radius: 14px; padding: 14px; overflow: auto; }}
      .album-card, .user-card, .admin-card {{ border: 1px solid var(--line); border-radius: 16px; padding: 16px; background: #fffdf8; }}
      .item-list {{ display: grid; gap: 10px; margin-top: 12px; }}
      .item {{ border-top: 1px solid var(--line); padding-top: 10px; }}
      .muted {{ color: #786b57; }}
      a.inline-link {{ color: #9a4d00; }}
    </style>
  </head>
  <body>
    <main>
      <nav>{nav}</nav>
      {body}
    </main>
    {script}
  </body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> str:
    state = get_state(request)
    base_url = public_base_url(request, state.settings)
    user = await authenticated_user(request, required=False)
    allow_registration = bool(await state.runtime_config.get_value("allow_registration"))
    anon_upload_enabled = bool(await state.runtime_config.get_value("anon_upload_enabled"))
    anon_expiry_hours = int(await state.runtime_config.get_value("anon_expiry_hours"))
    upload_enabled = user is not None or anon_upload_enabled
    auth_panel = ""
    if user is None:
        register_block = (
            """
        <section class="card auth-card">
          <h2>Create Account</h2>
          <form id="register-form" class="auth-form" action="/api/v1/auth/register" method="post">
            <input type="text" name="username" placeholder="Username" required>
            <input type="email" name="email" placeholder="Email" required>
            <input type="password" name="password" placeholder="Password" required>
            <label class="check"><input type="checkbox" name="remember_me" checked> Remember me</label>
            <button type="submit">Register</button>
          </form>
        </section>
            """
            if allow_registration
            else """
        <section class="card auth-card">
          <h2>Create Account</h2>
          <p class="hint">Registration is currently disabled.</p>
        </section>
            """
        )
        auth_panel = f"""
      <section class="grid">
        <section class="card auth-card">
          <h2>Sign In</h2>
          <form id="login-form" class="auth-form" action="/api/v1/auth/login" method="post">
            <input type="text" name="login" placeholder="Username or email" required>
            <input type="password" name="password" placeholder="Password" required>
            <label class="check"><input type="checkbox" name="remember_me" checked> Remember me</label>
            <button type="submit">Sign In</button>
          </form>
        </section>
        {register_block}
      </section>
        """
    else:
        auth_panel = f"""
      <section class="card auth-card">
        <h2>Signed In</h2>
        <p>Logged in as <strong>{user.username}</strong>.</p>
        <p class="hint">Continue in the dashboard for album management, account actions, and ShareX export.</p>
        <form id="logout-form" class="auth-form" action="/api/v1/auth/logout" method="post">
          <button type="submit">Log Out</button>
        </form>
      </section>
        """
    upload_block = (
        f"""
      <section class="card">
        <h1>imghost</h1>
        <p>{'Upload with your session-backed account. You can optionally target an existing owned album by ID.' if user else 'Paste or pick one or more files to create an anonymous album with clean media URLs.'}</p>
        <form id="upload-form" action="/api/v1/upload" method="post" enctype="multipart/form-data">
          <input type="text" name="title" placeholder="Album title (optional)">
          {('<input type="text" name="album_id" placeholder="Existing owned album ID (optional)">' if user else '')}
          <input type="file" name="file" required multiple>
          <button type="submit">Upload</button>
        </form>
        <p class="hint">Base URL: {base_url}</p>
        {f'<p class="hint">Anonymous uploads currently expire after {anon_expiry_hours} hour(s).</p>' if user is None else '<p class="hint">Authenticated uploads do not expire by default.</p>'}
        <p class="hint">For account tools, owned albums, admin APIs, and manual token-based album actions, use the pages in the nav above.</p>
        <pre id="upload-result" class="result hidden"></pre>
      </section>
        """
        if upload_enabled
        else f"""
      <section class="card">
        <h1>imghost</h1>
        <p>Anonymous uploads are currently disabled. Sign in to upload.</p>
        <p class="hint">Base URL: {base_url}</p>
        <p class="hint">If you already have an API key, the dashboard page can still use it for authenticated testing.</p>
      </section>
        """
    )
    body = f"""
      <p id="flash" class="flash"></p>
      <section class="stack">
        {auth_panel}
        {upload_block}
      </section>
    """
    script = """
    <script>
      const flash = document.getElementById("flash");
      const uploadResult = document.getElementById("upload-result");
      const showMessage = (message) => {
        if (flash) {
          flash.textContent = message;
        }
      };
      const submitJson = async (form, url) => {
        const formData = new FormData(form);
        const payload = Object.fromEntries(formData.entries());
        if ("remember_me" in payload) {
          payload.remember_me = formData.get("remember_me") === "on";
        }
        const response = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) {
          throw new Error(data.detail || "Request failed.");
        }
      };
      const loginForm = document.getElementById("login-form");
      if (loginForm) {
        loginForm.addEventListener("submit", async (event) => {
          event.preventDefault();
          try {
            await submitJson(loginForm, "/api/v1/auth/login");
            window.location.reload();
          } catch (error) {
            showMessage(error.message);
          }
        });
      }
      const registerForm = document.getElementById("register-form");
      if (registerForm) {
        registerForm.addEventListener("submit", async (event) => {
          event.preventDefault();
          try {
            await submitJson(registerForm, "/api/v1/auth/register");
            window.location.reload();
          } catch (error) {
            showMessage(error.message);
          }
        });
      }
      const logoutForm = document.getElementById("logout-form");
      if (logoutForm) {
        logoutForm.addEventListener("submit", async (event) => {
          event.preventDefault();
          try {
            await fetch("/api/v1/auth/logout", { method: "POST" });
            window.location.reload();
          } catch {
            showMessage("Logout failed.");
          }
        });
      }
      const uploadForm = document.getElementById("upload-form");
      if (uploadForm) {
        uploadForm.addEventListener("submit", async (event) => {
          event.preventDefault();
          try {
            const response = await fetch("/api/v1/upload", {
              method: "POST",
              body: new FormData(uploadForm),
            });
            const data = await response.json().catch(() => ({}));
            if (!response.ok) {
              throw new Error(data.detail || "Upload failed.");
            }
            if (uploadResult) {
              uploadResult.classList.remove("hidden");
              uploadResult.textContent = JSON.stringify(data, null, 2);
            }
            showMessage("Upload succeeded.");
          } catch (error) {
            showMessage(error.message);
          }
        });
      }
    </script>
    """
    return page_shell("imghost", body, user=user, script=script)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request) -> str:
    state = get_state(request)
    base_url = public_base_url(request, state.settings)
    user = await authenticated_user(request, required=False)
    session_user = await state.uploads.get_current_user_summary(user) if user else None
    bootstrap = json.dumps({"session_user": session_user, "base_url": base_url})
    body = """
      <p id="flash" class="flash"></p>
      <section class="grid">
        <section class="card">
          <h1>User Dashboard</h1>
          <p>Use this page to test authenticated uploads and owned album management. Session auth or a pasted API key both work.</p>
        </section>
        <section class="card">
          <h2>API Key Mode</h2>
          <form id="api-key-form">
            <input id="api-key-input" type="text" name="api_key" placeholder="Paste API key to drive the dashboard without a browser session">
            <div class="row">
              <button type="submit">Use API Key</button>
              <button id="clear-api-key" type="button" class="secondary">Clear API Key</button>
            </div>
          </form>
          <p class="hint">This is useful for testing on plain HTTP when secure session cookies are unavailable.</p>
        </section>
      </section>
      <section id="dashboard-unauth" class="card">
        <h2>Authentication Needed</h2>
        <p>Sign in on the home page or paste an API key here to unlock the dashboard.</p>
      </section>
      <section id="dashboard-auth" class="stack hidden">
        <section class="card">
          <h2>User Settings</h2>
          <p>API key management, ShareX export, password changes, and account deletion now live on the dedicated settings page.</p>
          <p><a class="inline-link" href="/settings">Open settings</a></p>
        </section>
        <section class="card">
          <h2>Authenticated Upload</h2>
          <form id="dashboard-upload-form" enctype="multipart/form-data">
            <input type="text" name="title" placeholder="Album title (optional)">
            <input type="text" name="album_id" placeholder="Existing owned album ID (optional)">
            <input type="file" name="file" required multiple>
            <button type="submit">Upload</button>
          </form>
          <pre id="dashboard-upload-result" class="result hidden"></pre>
        </section>
        <section class="card">
          <div class="row">
            <h2>Owned Albums</h2>
            <button id="refresh-albums" type="button">Refresh Albums</button>
          </div>
          <p id="owned-albums-link" class="hint"></p>
          <div id="owned-albums" class="stack"></div>
        </section>
      </section>
    """
    script = f"""
    <script>
      const boot = {bootstrap};
      const state = {{
        apiKey: window.localStorage.getItem("imghost_api_key") || "",
        latestApiKey: null,
        user: boot.session_user,
      }};
      const flash = document.getElementById("flash");
      const apiKeyInput = document.getElementById("api-key-input");
      const unauth = document.getElementById("dashboard-unauth");
      const auth = document.getElementById("dashboard-auth");
      const albumsRoot = document.getElementById("owned-albums");
      const albumsLink = document.getElementById("owned-albums-link");
      const uploadResult = document.getElementById("dashboard-upload-result");
      apiKeyInput.value = state.apiKey;

      const showMessage = (message) => {{
        flash.textContent = message || "";
      }};
      const escapeHtml = (value) => String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
      const authHeaders = (headers = {{}}) => {{
        const resolved = new Headers(headers);
        const apiKey = state.apiKey || state.latestApiKey;
        if (apiKey) {{
          resolved.set("Authorization", `Bearer ${{apiKey}}`);
        }}
        return resolved;
      }};
      const requestJson = async (url, options = {{}}) => {{
        const response = await fetch(url, {{ ...options, headers: authHeaders(options.headers || {{}}) }});
        const data = await response.json().catch(() => ({{}}));
        if (!response.ok) {{
          throw new Error(data.detail || `Request failed (${{response.status}}).`);
        }}
        return data;
      }};
      const formToObject = (form) => Object.fromEntries(new FormData(form).entries());
      const parseOptionalNumber = (value) => value === "" ? null : Number(value);
      const renderState = () => {{
        const isAuthed = !!state.user;
        unauth.classList.toggle("hidden", isAuthed);
        auth.classList.toggle("hidden", !isAuthed);
        if (!isAuthed) {{
          albumsRoot.innerHTML = "";
          albumsLink.textContent = "";
          return;
        }}
        albumsLink.innerHTML = `Public album list: <a class="inline-link" href="/u/${{encodeURIComponent(state.user.username)}}" target="_blank">/u/${{escapeHtml(state.user.username)}}</a>`;
      }};
      const refreshUser = async () => {{
        try {{
          state.user = await requestJson("/api/v1/user/me");
        }} catch {{
          state.user = null;
        }}
        renderState();
        if (state.user) {{
          await refreshAlbums();
        }}
      }};
      const renderAlbum = (album) => {{
        const orderValue = album.items.map((item) => `${{item.id}}:${{item.position}}`).join("\\n");
        const items = album.items.map((item) => `
          <div class="item">
            <p><strong>${{escapeHtml(item.filename)}}</strong> · ${{escapeHtml(item.id)}} · ${{escapeHtml(item.media_type)}} · thumb=${{escapeHtml(item.thumb_status)}}</p>
            <p class="hint"><a class="inline-link" href="${{item.media_url}}" target="_blank">media</a> · <a class="inline-link" href="${{item.thumb_url}}" target="_blank">thumb</a>${{item.compat_warning ? ` · ${{escapeHtml(item.compat_warning)}}` : ""}}</p>
            <button type="button" class="danger media-delete" data-media-id="${{item.id}}">Delete Media</button>
          </div>
        `).join("");
        return `
          <section class="album-card" data-album-id="${{album.id}}">
            <h3>${{escapeHtml(album.title || "Untitled album")}}</h3>
            <p class="hint">Album ${{escapeHtml(album.id)}} · ${{album.item_count}} item(s) · <a class="inline-link" href="/a/${{album.id}}" target="_blank">public page</a> · <a class="inline-link" href="/api/v1/album/${{album.id}}/zip" target="_blank">zip</a></p>
            <form class="album-edit-form">
              <input type="text" name="title" placeholder="Album title" value="${{escapeHtml(album.title || "")}}">
              <input type="text" name="cover_media_id" placeholder="Cover media ID (blank to clear)" value="${{escapeHtml(album.cover_media_id || "")}}">
              <button type="submit">Save Album Metadata</button>
            </form>
            <form class="album-append-form" enctype="multipart/form-data">
              <input type="file" name="file" required multiple>
              <button type="submit" class="secondary">Append Files To Album</button>
            </form>
            <form class="album-order-form">
              <textarea name="order" placeholder="media_id:position per line">${{escapeHtml(orderValue)}}</textarea>
              <button type="submit" class="ghost">Reorder</button>
            </form>
            <div class="item-list">${{items || '<p class="muted">No items.</p>'}}</div>
            <button type="button" class="danger album-delete">Delete Album</button>
          </section>
        `;
      }};
      const refreshAlbums = async () => {{
        if (!state.user) {{
          albumsRoot.innerHTML = "";
          return;
        }}
        const albums = await requestJson("/api/v1/user/me/albums");
        albumsRoot.innerHTML = albums.length ? albums.map(renderAlbum).join("") : '<p class="muted">No owned albums yet.</p>';
      }};

      document.getElementById("api-key-form").addEventListener("submit", async (event) => {{
        event.preventDefault();
        state.apiKey = apiKeyInput.value.trim();
        state.latestApiKey = null;
        if (state.apiKey) {{
          window.localStorage.setItem("imghost_api_key", state.apiKey);
        }} else {{
          window.localStorage.removeItem("imghost_api_key");
        }}
        await refreshUser();
        showMessage(state.user ? "API key accepted." : "Unable to authenticate with that API key.");
      }});
      document.getElementById("clear-api-key").addEventListener("click", async () => {{
        state.apiKey = "";
        state.latestApiKey = null;
        apiKeyInput.value = "";
        window.localStorage.removeItem("imghost_api_key");
        await refreshUser();
        showMessage("API key cleared.");
      }});
      document.getElementById("dashboard-upload-form").addEventListener("submit", async (event) => {{
        event.preventDefault();
        try {{
          const response = await fetch("/api/v1/upload", {{
            method: "POST",
            headers: authHeaders(),
            body: new FormData(event.currentTarget),
          }});
          const data = await response.json().catch(() => ({{}}));
          if (!response.ok) {{
            throw new Error(data.detail || "Upload failed.");
          }}
          uploadResult.classList.remove("hidden");
          uploadResult.textContent = JSON.stringify(data, null, 2);
          event.currentTarget.reset();
          await refreshAlbums();
          showMessage("Upload succeeded.");
        }} catch (error) {{
          showMessage(error.message);
        }}
      }});
      document.getElementById("refresh-albums").addEventListener("click", refreshAlbums);
      albumsRoot.addEventListener("submit", async (event) => {{
        const card = event.target.closest("[data-album-id]");
        if (!card) return;
        const albumId = card.dataset.albumId;
        event.preventDefault();
        try {{
          if (event.target.matches(".album-edit-form")) {{
            const form = new FormData(event.target);
            await requestJson(`/api/v1/album/${{albumId}}`, {{
              method: "PATCH",
              headers: {{ "Content-Type": "application/json" }},
              body: JSON.stringify({{
                title: form.get("title") || null,
                cover_media_id: form.get("cover_media_id") || null,
              }}),
            }});
            showMessage("Album metadata updated.");
          }} else if (event.target.matches(".album-append-form")) {{
            const form = new FormData(event.target);
            form.append("album_id", albumId);
            const response = await fetch("/api/v1/upload", {{
              method: "POST",
              headers: authHeaders(),
              body: form,
            }});
            const data = await response.json().catch(() => ({{}}));
            if (!response.ok) {{
              throw new Error(data.detail || "Append upload failed.");
            }}
            showMessage("Files appended to album.");
          }} else if (event.target.matches(".album-order-form")) {{
            const raw = new FormData(event.target).get("order");
            const payload = String(raw || "").split("\\n").map((line) => line.trim()).filter(Boolean).map((line) => {{
              const [media_id, position] = line.split(":");
              return {{ media_id: media_id.trim(), position: Number(position.trim()) }};
            }});
            await requestJson(`/api/v1/album/${{albumId}}/order`, {{
              method: "PATCH",
              headers: {{ "Content-Type": "application/json" }},
              body: JSON.stringify(payload),
            }});
            showMessage("Album order updated.");
          }}
          await refreshAlbums();
        }} catch (error) {{
          showMessage(error.message);
        }}
      }});
      albumsRoot.addEventListener("click", async (event) => {{
        const card = event.target.closest("[data-album-id]");
        if (!card) return;
        const albumId = card.dataset.albumId;
        try {{
          if (event.target.matches(".album-delete")) {{
            if (!window.confirm(`Delete album ${{albumId}}?`)) return;
            await requestJson(`/api/v1/album/${{albumId}}`, {{ method: "DELETE" }});
            showMessage("Album deleted.");
            await refreshAlbums();
          }} else if (event.target.matches(".media-delete")) {{
            const mediaId = event.target.dataset.mediaId;
            if (!window.confirm(`Delete media ${{mediaId}}?`)) return;
            await requestJson(`/api/v1/media/${{mediaId}}`, {{ method: "DELETE" }});
            showMessage("Media deleted.");
            await refreshAlbums();
          }}
        }} catch (error) {{
          showMessage(error.message);
        }}
      }});

      refreshUser();
    </script>
    """
    return page_shell("Dashboard", body, user=user, script=script)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request) -> str:
    state = get_state(request)
    user = await authenticated_user(request, required=False)
    session_user = await state.uploads.get_current_user_summary(user) if user else None
    bootstrap = json.dumps({"session_user": session_user})
    body = """
      <p id="flash" class="flash"></p>
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
    return page_shell("Settings", body, user=user, script=script)


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request) -> str:
    state = get_state(request)
    user = await authenticated_user(request, required=False)
    session_user = await state.uploads.get_current_user_summary(user) if user else None
    bootstrap = json.dumps({"session_user": session_user})
    body = """
      <p id="flash" class="flash"></p>
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
    return page_shell("Admin", body, user=user, script=script)


@app.get("/album-tools", response_class=HTMLResponse)
async def album_tools_page(request: Request) -> str:
    user = await authenticated_user(request, required=False)
    body = """
      <p id="flash" class="flash"></p>
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
    return page_shell("Album Tools", body, user=user, script=script)


@app.post("/api/v1/upload")
async def upload(
    request: Request,
    file: list[UploadFile] = File(...),
    album_id: str | None = Form(default=None),
    title: str | None = Form(default=None),
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
    for item in file:
        result = await state.uploads.upload(
            item,
            active_album_id,
            title,
            cid,
            actor=actor,
            rate_limit_key=upload_rate_limit_key(request, user),
        )
        active_album_id = result.album.id
        results.append(result)

    primary = results[0]
    payload = {
        "album_id": primary.album.id,
        "album_url": f"{base_url}/a/{primary.album.id}",
        "media_id": primary.media.id,
        "media_url": media_url(base_url, primary.media.id, primary.media.format),
        "thumb_url": thumb_url(base_url, primary.media.id, primary.media.format),
        "delete_url": album_delete_url(base_url, primary.album),
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
async def album_page(request: Request, album_id: str) -> str:
    if not is_valid_id(album_id, ALBUM_ID_LENGTH):
        raise HTTPException(status_code=404)
    state = get_state(request)
    base_url = public_base_url(request, state.settings)
    album = await state.repository.get_album(album_id)
    if album is None or is_expired(album.expires_at):
        raise HTTPException(status_code=404)
    items = await state.repository.list_album_media(album_id)
    expiry_hint = humanize_expiry(album.expires_at)
    compat_warnings = [warning for warning in dict.fromkeys(compatibility_warning(item) for item in items) if warning]
    cards = []
    for item in items:
        preview_url = thumb_url(base_url, item.id, item.format)
        preview_url = thumb_url(base_url, item.id, thumb_format(item))
        if item.media_type == "video":
            poster_attr = f' poster="{preview_url}"' if item.thumb_status == "done" else ""
            media_tag = f'<video controls preload="metadata" src="{media_url(base_url, item.id, item.format)}"{poster_attr}></video>'
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
              <input type="text" readonly value="{media_url(base_url, item.id, item.format)}">
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


@app.get("/u/{username}", response_class=HTMLResponse)
async def user_album_list_page(request: Request, username: str) -> str:
    state = get_state(request)
    base_url = public_base_url(request, state.settings)
    user, albums = await state.uploads.list_public_albums_for_username(username)

    cards = []
    for album in albums:
        if album["cover_media_id"] is not None and album["cover_thumb_format"] is not None:
            if album["cover_thumb_status"] == "done":
                preview = (
                    f'<img src="{thumb_url(base_url, album["cover_media_id"], album["cover_thumb_format"])}" '
                    f'alt="{escape(album["title"] or "Untitled album")}">'
                )
            else:
                preview = '<div class="placeholder">Thumbnail pending</div>'
        else:
            preview = '<div class="placeholder">No media</div>'
        cards.append(
            f"""
            <a class="album-card" href="/a/{album["id"]}">
              {preview}
              <div class="meta">
                <h2>{escape(album["title"] or "Untitled album")}</h2>
                <p>{album["item_count"]} item(s) · {humanize_bytes(int(album["total_size"]))}</p>
                <p class="hint">Created {escape(str(album["created_at"]))}</p>
              </div>
            </a>
            """
        )

    empty_state = (
        '<p class="empty">This user has no public albums yet.</p>'
        if not cards
        else "".join(cards)
    )

    return f"""
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{escape(user.username)} albums</title>
    <style>
      body {{ margin: 0; font-family: Georgia, serif; background: #f4f1ea; color: #18212f; }}
      main {{ max-width: 980px; margin: 0 auto; padding: 32px 20px 64px; }}
      .hero {{ margin-bottom: 24px; }}
      .grid {{ display: grid; gap: 20px; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }}
      .album-card {{ display: block; text-decoration: none; color: inherit; background: #fffdf8; border: 1px solid #e3d6be; border-radius: 18px; overflow: hidden; box-shadow: 0 10px 24px rgba(24,33,47,.06); }}
      .album-card img, .placeholder {{ width: 100%; aspect-ratio: 16 / 10; display: block; background: #ebe6dc; object-fit: cover; }}
      .placeholder {{ display: grid; place-items: center; color: #786b57; font-style: italic; }}
      .meta {{ padding: 14px; }}
      h1, h2 {{ margin: 0 0 8px; }}
      p {{ margin: 0; line-height: 1.5; }}
      .hint {{ color: #786b57; }}
      .empty {{ color: #786b57; font-style: italic; }}
    </style>
  </head>
  <body>
    <main>
      <section class="hero">
        <p class="hint">Public user album list.</p>
        <h1>{escape(user.username)}</h1>
        <p>{len(albums)} public album(s), sorted by most recently modified.</p>
      </section>
      <section class="grid">
        {empty_state}
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
