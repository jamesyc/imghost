from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    base_url: str
    database_url: str
    data_dir: Path
    secret_key: str
    session_cookie_name: str
    session_remember_days: int
    max_upload_bytes: int
    anon_expiry_hours: int
    max_pixel_megapixels: int
    default_user_quota_bytes: int
    server_quota_bytes: int
    video_thumb_frames: int
    task_queue_mode: str
    thumbnail_worker_count: int


def load_settings() -> Settings:
    data_dir = Path(os.getenv("IMGHOST_DATA_DIR", "data")).resolve()
    return Settings(
        base_url=os.getenv("BASE_URL", "http://localhost:8000").rstrip("/"),
        database_url=os.getenv("DATABASE_URL", "postgresql://imghost:imghost@localhost:5432/imghost"),
        data_dir=data_dir,
        secret_key=os.getenv("SECRET_KEY", "dev-secret-key"),
        session_cookie_name=os.getenv("SESSION_COOKIE_NAME", "imghost_session"),
        session_remember_days=max(1, int(os.getenv("SESSION_REMEMBER_DAYS", "30"))),
        max_upload_bytes=int(os.getenv("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024))),
        anon_expiry_hours=int(os.getenv("ANON_EXPIRY_HOURS", "24")),
        max_pixel_megapixels=int(os.getenv("MAX_PIXEL_MEGAPIXELS", "50")),
        default_user_quota_bytes=int(os.getenv("DEFAULT_USER_QUOTA_BYTES", str(2 * 1024 * 1024 * 1024))),
        server_quota_bytes=int(os.getenv("SERVER_QUOTA_BYTES", "0")),
        video_thumb_frames=max(1, int(os.getenv("VIDEO_THUMB_FRAMES", "10"))),
        task_queue_mode=os.getenv("TASK_QUEUE_MODE", "async").strip().lower(),
        thumbnail_worker_count=max(1, int(os.getenv("THUMBNAIL_WORKER_COUNT", "1"))),
    )
