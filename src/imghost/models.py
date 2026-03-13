from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class User:
    id: str
    username: str
    email: str
    password_hash: str | None
    is_admin: bool
    suspended: bool
    quota_bytes: int | None
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("created_at", "updated_at"):
            data[key] = data[key].isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "User":
        values = data.copy()
        for key in ("created_at", "updated_at"):
            values[key] = datetime.fromisoformat(values[key])
        values.setdefault("password_hash", None)
        values.setdefault("is_admin", False)
        values.setdefault("suspended", False)
        values.setdefault("quota_bytes", None)
        return cls(**values)


@dataclass
class ApiKey:
    id: str
    user_id: str
    key_hash: str
    created_at: datetime
    last_used_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("created_at", "last_used_at"):
            if data[key] is not None:
                data[key] = data[key].isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApiKey":
        values = data.copy()
        values["created_at"] = datetime.fromisoformat(values["created_at"])
        if values.get("last_used_at") is not None:
            values["last_used_at"] = datetime.fromisoformat(values["last_used_at"])
        return cls(**values)


@dataclass
class Album:
    id: str
    title: str | None
    user_id: str | None
    cover_media_id: str | None
    delete_token: str | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("created_at", "updated_at", "expires_at"):
            if data[key] is not None:
                data[key] = data[key].isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Album":
        values = data.copy()
        values.setdefault("delete_token", None)
        for key in ("created_at", "updated_at", "expires_at"):
            if values.get(key) is not None:
                values[key] = datetime.fromisoformat(values[key])
        return cls(**values)


@dataclass
class Media:
    id: str
    album_id: str
    user_id: str | None
    filename_orig: str
    media_type: str
    format: str
    mime_type: str
    storage_key: str
    thumb_key: str | None
    thumb_is_orig: bool
    thumb_status: str
    file_size: int
    thumb_size: int | None
    width: int | None
    height: int | None
    duration_secs: float | None
    is_animated: bool
    codec_hint: str | None
    position: int
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Media":
        values = data.copy()
        values["created_at"] = datetime.fromisoformat(values["created_at"])
        return cls(**values)
