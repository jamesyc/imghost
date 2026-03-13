from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    base_url: str
    data_dir: Path
    max_upload_bytes: int
    anon_expiry_hours: int


def load_settings() -> Settings:
    data_dir = Path(os.getenv("IMGHOST_DATA_DIR", "data")).resolve()
    return Settings(
        base_url=os.getenv("BASE_URL", "http://localhost:8000").rstrip("/"),
        data_dir=data_dir,
        max_upload_bytes=int(os.getenv("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024))),
        anon_expiry_hours=int(os.getenv("ANON_EXPIRY_HOURS", "24")),
    )
