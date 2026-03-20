from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    base_url: str
    database_url: str
    data_dir: Path
    redis_url: str | None
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


def load_settings() -> Settings:
    data_dir = Path(os.getenv("IMGHOST_DATA_DIR", "data")).resolve()
    base_url = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
    session_cookie_secure = _env_bool("SESSION_COOKIE_SECURE")
    if session_cookie_secure is None:
        session_cookie_secure = urlsplit(base_url).scheme == "https"
    return Settings(
        base_url=base_url,
        database_url=os.getenv("DATABASE_URL", "postgresql://imghost:imghost@localhost:5432/imghost"),
        data_dir=data_dir,
        redis_url=(os.getenv("REDIS_URL") or "").strip() or None,
        redis_mode=os.getenv("REDIS_MODE", "auto").strip().lower(),
        redis_prefix=os.getenv("REDIS_PREFIX", "imghost").strip() or "imghost",
        storage_backend=os.getenv("STORAGE_BACKEND", "filesystem").strip().lower(),
        s3_endpoint_url=(os.getenv("S3_ENDPOINT_URL") or "").strip() or None,
        s3_access_key_id=(os.getenv("S3_ACCESS_KEY_ID") or "").strip() or None,
        s3_secret_access_key=(os.getenv("S3_SECRET_ACCESS_KEY") or "").strip() or None,
        s3_bucket=(os.getenv("S3_BUCKET") or "").strip() or None,
        s3_region=os.getenv("S3_REGION", "garage").strip() or "garage",
        secret_key=os.getenv("SECRET_KEY", "dev-secret-key"),
        session_cookie_name=os.getenv("SESSION_COOKIE_NAME", "imghost_session"),
        session_cookie_secure=session_cookie_secure,
        session_remember_days=max(1, int(os.getenv("SESSION_REMEMBER_DAYS", "30"))),
        max_upload_bytes=int(os.getenv("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024))),
        anon_expiry_hours=int(os.getenv("ANON_EXPIRY_HOURS", "24")),
        max_pixel_megapixels=int(os.getenv("MAX_PIXEL_MEGAPIXELS", "50")),
        default_user_quota_bytes=int(os.getenv("DEFAULT_USER_QUOTA_BYTES", str(2 * 1024 * 1024 * 1024))),
        server_quota_bytes=int(os.getenv("SERVER_QUOTA_BYTES", "0")),
        video_thumb_frames=max(1, int(os.getenv("VIDEO_THUMB_FRAMES", "10"))),
        task_queue_mode=os.getenv("TASK_QUEUE_MODE", "async").strip().lower(),
        task_worker_enabled=_env_bool("TASK_WORKER_ENABLED") if _env_bool("TASK_WORKER_ENABLED") is not None else True,
        thumbnail_worker_count=max(1, int(os.getenv("THUMBNAIL_WORKER_COUNT", "1"))),
    )
