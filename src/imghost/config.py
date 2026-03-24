from __future__ import annotations

import os
from dataclasses import dataclass
from ipaddress import ip_network
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit


@dataclass(frozen=True)
class Settings:
    base_url: str
    public_origin_enabled: bool
    trusted_public_origins: tuple[str, ...]
    trusted_proxy_cidrs_enabled: bool
    trusted_proxy_cidrs: tuple[str, ...]
    database_url: str
    data_dir: Path
    redis_url: str | None
    redis_password: str | None
    redis_mode: str
    redis_prefix: str
    storage_backend: str
    s3_endpoint_url: str | None
    s3_access_key_id: str | None
    s3_secret_access_key: str | None
    s3_bucket: str | None
    s3_region: str
    secret_key: str
    session_cookie_name: str
    session_cookie_secure: bool
    session_redis_fail_closed: bool
    session_remember_days: int
    max_upload_bytes: int
    anon_expiry_hours: int
    max_pixel_megapixels: int
    default_user_quota_bytes: int
    server_quota_bytes: int
    video_thumb_frames: int
    task_queue_mode: str
    task_worker_enabled: bool
    thumbnail_worker_count: int
    task_worker_queues: tuple[str, ...] = ("default", "thumbnails")
    scheduler_enabled: bool = False
    scheduler_poll_seconds: int = 30
    scheduler_lease_seconds: int = 900
    cleanup_interval_seconds: int = 900
    promote_username_to_admin: str | None = None
    google_oauth_enabled: bool = False
    google_client_id: str | None = None
    google_client_secret: str | None = None


def _env_bool(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean-like value.")


def _env_csv(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    values = [item.strip() for item in raw.split(",")]
    return tuple(item for item in values if item)


def _dedupe_strings(values: tuple[str, ...]) -> tuple[str, ...]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return tuple(deduped)


def _resolve_redis_url(raw_url: str | None, raw_password: str | None) -> str | None:
    url = (raw_url or "").strip() or None
    password = (raw_password or "").strip() or None
    if url is None:
        return None
    parsed = urlsplit(url)
    if parsed.password is not None or not password:
        return url
    username = parsed.username or ""
    credentials = f"{username}:{quote(password, safe='')}" if username else f":{quote(password, safe='')}"
    host = parsed.hostname or ""
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    netloc = f"{credentials}@{host}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def load_settings() -> Settings:
    data_dir = Path(os.getenv("IMGHOST_DATA_DIR", "data")).resolve()
    base_url = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
    session_cookie_secure = _env_bool("SESSION_COOKIE_SECURE")
    public_origin_enabled = _env_bool("PUBLIC_ORIGIN_ENABLED")
    trusted_proxy_cidrs_enabled = _env_bool("TRUSTED_PROXY_CIDRS_ENABLED")
    session_redis_fail_closed = _env_bool("SESSION_REDIS_FAIL_CLOSED")
    trusted_proxy_cidrs = _env_csv("TRUSTED_PROXY_CIDRS")
    redis_password = (os.getenv("REDIS_PASSWORD") or "").strip() or None
    if session_cookie_secure is None:
        session_cookie_secure = urlsplit(base_url).scheme == "https"
    if public_origin_enabled is None:
        public_origin_enabled = True
    if trusted_proxy_cidrs_enabled is None:
        trusted_proxy_cidrs_enabled = False
    if session_redis_fail_closed is None:
        session_redis_fail_closed = False
    if trusted_proxy_cidrs_enabled and not trusted_proxy_cidrs:
        raise ValueError("TRUSTED_PROXY_CIDRS_ENABLED=true requires TRUSTED_PROXY_CIDRS to be set.")
    for cidr in trusted_proxy_cidrs:
        ip_network(cidr, strict=False)
    google_oauth_enabled = _env_bool("GOOGLE_OAUTH_ENABLED")
    if google_oauth_enabled is None:
        google_oauth_enabled = False
    google_client_id = (os.getenv("GOOGLE_CLIENT_ID") or "").strip() or None
    google_client_secret = (os.getenv("GOOGLE_CLIENT_SECRET") or "").strip() or None
    return Settings(
        base_url=base_url,
        public_origin_enabled=public_origin_enabled,
        trusted_public_origins=_env_csv("TRUSTED_PUBLIC_ORIGINS"),
        trusted_proxy_cidrs_enabled=trusted_proxy_cidrs_enabled,
        trusted_proxy_cidrs=trusted_proxy_cidrs,
        database_url=os.getenv("DATABASE_URL", "postgresql://imghost:imghost@localhost:5432/imghost"),
        data_dir=data_dir,
        redis_url=_resolve_redis_url(os.getenv("REDIS_URL"), redis_password),
        redis_password=redis_password,
        redis_mode=os.getenv("REDIS_MODE", "auto").strip().lower(),
        redis_prefix=os.getenv("REDIS_PREFIX", "imghost").strip() or "imghost",
        storage_backend=os.getenv("STORAGE_BACKEND", "filesystem").strip().lower(),
        s3_endpoint_url=(os.getenv("S3_ENDPOINT_URL") or "").strip() or None,
        s3_access_key_id=(os.getenv("S3_ACCESS_KEY_ID") or "").strip() or None,
        s3_secret_access_key=(os.getenv("S3_SECRET_ACCESS_KEY") or "").strip() or None,
        s3_bucket=(os.getenv("S3_BUCKET") or "").strip() or None,
        s3_region=os.getenv("S3_REGION", "garage").strip() or "garage",
        secret_key=os.getenv("SECRET_KEY", "dev-secret-key"),
        promote_username_to_admin=(os.getenv("PROMOTE_USERNAME_TO_ADMIN") or "").strip() or None,
        session_cookie_name=os.getenv("SESSION_COOKIE_NAME", "imghost_session"),
        session_cookie_secure=session_cookie_secure,
        session_redis_fail_closed=session_redis_fail_closed,
        session_remember_days=max(1, int(os.getenv("SESSION_REMEMBER_DAYS", "30"))),
        max_upload_bytes=int(os.getenv("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024))),
        anon_expiry_hours=int(os.getenv("ANON_EXPIRY_HOURS", "24")),
        max_pixel_megapixels=int(os.getenv("MAX_PIXEL_MEGAPIXELS", "50")),
        default_user_quota_bytes=int(os.getenv("DEFAULT_USER_QUOTA_BYTES", str(2 * 1024 * 1024 * 1024))),
        server_quota_bytes=int(os.getenv("SERVER_QUOTA_BYTES", "0")),
        video_thumb_frames=max(1, int(os.getenv("VIDEO_THUMB_FRAMES", "10"))),
        task_queue_mode=os.getenv("TASK_QUEUE_MODE", "async").strip().lower(),
        task_worker_enabled=_env_bool("TASK_WORKER_ENABLED") if _env_bool("TASK_WORKER_ENABLED") is not None else True,
        task_worker_queues=_dedupe_strings(_env_csv("TASK_WORKER_QUEUES")) or ("default", "thumbnails"),
        thumbnail_worker_count=max(1, int(os.getenv("THUMBNAIL_WORKER_COUNT", "1"))),
        scheduler_enabled=_env_bool("SCHEDULER_ENABLED") or False,
        scheduler_poll_seconds=max(1, int(os.getenv("SCHEDULER_POLL_SECONDS", "30"))),
        scheduler_lease_seconds=max(1, int(os.getenv("SCHEDULER_LEASE_SECONDS", "900"))),
        cleanup_interval_seconds=max(1, int(os.getenv("CLEANUP_INTERVAL_SECONDS", "900"))),
        google_oauth_enabled=google_oauth_enabled,
        google_client_id=google_client_id,
        google_client_secret=google_client_secret,
    )
