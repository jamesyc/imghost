from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta
import hmac
import json
import logging
from hashlib import sha256
from uuid import uuid4

from .config import Settings
from .models import User, utcnow
from .telemetry import Telemetry
from .redis_support import RedisHandle, RedisUnavailable

logger = logging.getLogger(__name__)


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(f"{data}{padding}")


@dataclass(frozen=True)
class SessionPayload:
    session_id: str
    user_id: str
    created_at: str
    expires_at: str | None
    store: str

    def to_dict(self) -> dict[str, str | None]:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "store": self.store,
        }


class SessionBackend:
    async def create_session(self, user: User, *, remember_me: bool) -> tuple[str, datetime | None]:
        raise NotImplementedError

    async def resolve_user(self, token: str) -> str | None:
        raise NotImplementedError

    async def clear_session(self, token: str | None) -> None:
        raise NotImplementedError


class SessionBackendUnavailable(RuntimeError):
    pass


class CookieSessionBackend(SessionBackend):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def create_session(self, user: User, *, remember_me: bool) -> tuple[str, datetime | None]:
        return _create_signed_token(self.settings, user, remember_me=remember_me, store="cookie")

    async def resolve_user(self, token: str) -> str | None:
        payload = _decode_signed_token(self.settings, token)
        return payload.user_id if payload else None

    async def clear_session(self, token: str | None) -> None:
        return None


class RedisBackedSessionBackend(SessionBackend):
    def __init__(self, settings: Settings, redis: RedisHandle, telemetry: Telemetry) -> None:
        self.settings = settings
        self.redis = redis
        self.telemetry = telemetry

    async def create_session(self, user: User, *, remember_me: bool) -> tuple[str, datetime | None]:
        token, expires_at = _create_signed_token(self.settings, user, remember_me=remember_me, store="cookie")
        payload = _decode_signed_token(self.settings, token)
        if payload is None:
            raise RuntimeError("Failed to create session token.")
        ttl_seconds = _session_ttl_seconds(self.settings, expires_at)
        try:
            await self.redis.execute(
                "create session",
                lambda client: client.set(
                    self.redis.prefixed(f"session:{payload.session_id}"),
                    json.dumps({"user_id": user.id}),
                    ex=ttl_seconds,
                ),
            )
        except RedisUnavailable:
            self.telemetry.mark_subsystem_degraded(
                "sessions",
                operation="create session",
                reason="redis_unavailable",
            )
            if self.settings.session_redis_fail_closed:
                logger.warning("session_backend_unavailable", extra={"reason": "redis_unavailable", "action": "create"})
                raise SessionBackendUnavailable("Redis-backed sessions are currently unavailable.")
            logger.warning("session_backend_fallback", extra={"reason": "redis_unavailable", "action": "create"})
            return token, expires_at
        self.telemetry.mark_subsystem_recovered("sessions", operation="create session")
        return _sign_payload(self.settings, payload, store="redis"), expires_at

    async def resolve_user(self, token: str) -> str | None:
        payload = _decode_signed_token(self.settings, token)
        if payload is None:
            return None
        if payload.store != "redis":
            return payload.user_id
        try:
            raw = await self.redis.execute(
                "resolve session",
                lambda client: client.get(self.redis.prefixed(f"session:{payload.session_id}")),
            )
        except RedisUnavailable:
            self.telemetry.mark_subsystem_degraded(
                "sessions",
                operation="resolve session",
                reason="redis_unavailable",
            )
            if self.settings.session_redis_fail_closed:
                logger.warning("session_backend_unavailable", extra={"reason": "redis_unavailable", "action": "resolve"})
                raise SessionBackendUnavailable("Redis-backed sessions are currently unavailable.")
            logger.warning("session_backend_fallback", extra={"reason": "redis_unavailable", "action": "resolve"})
            return payload.user_id
        self.telemetry.mark_subsystem_recovered("sessions", operation="resolve session")
        if raw is None:
            return None
        try:
            stored = json.loads(raw)
        except json.JSONDecodeError:
            return None
        user_id = stored.get("user_id")
        return user_id if isinstance(user_id, str) and user_id == payload.user_id else None

    async def clear_session(self, token: str | None) -> None:
        if not token:
            return
        payload = _decode_signed_token(self.settings, token)
        if payload is None or payload.store != "redis":
            return
        try:
            await self.redis.execute(
                "delete session",
                lambda client: client.delete(self.redis.prefixed(f"session:{payload.session_id}")),
            )
        except RedisUnavailable:
            self.telemetry.mark_subsystem_degraded(
                "sessions",
                operation="delete session",
                reason="redis_unavailable",
            )
            return
        self.telemetry.mark_subsystem_recovered("sessions", operation="delete session")


def build_session_backend(settings: Settings, redis: RedisHandle, telemetry: Telemetry) -> SessionBackend:
    if redis.enabled:
        return RedisBackedSessionBackend(settings, redis, telemetry)
    return CookieSessionBackend(settings)


def _session_ttl_seconds(settings: Settings, expires_at: datetime | None) -> int:
    if expires_at is not None:
        return max(1, int((expires_at - utcnow()).total_seconds()))
    return max(1, settings.session_remember_days * 24 * 60 * 60)


def _create_signed_token(settings: Settings, user: User, *, remember_me: bool, store: str) -> tuple[str, datetime | None]:
    created_at = utcnow().replace(microsecond=0)
    expires_at = None
    if remember_me:
        expires_at = created_at + timedelta(days=settings.session_remember_days)
    payload = SessionPayload(
        session_id=str(uuid4()),
        user_id=user.id,
        created_at=created_at.isoformat(),
        expires_at=expires_at.isoformat() if expires_at else None,
        store=store,
    )
    return _sign_payload(settings, payload, store=store), expires_at


def _sign_payload(settings: Settings, payload: SessionPayload, *, store: str) -> str:
    payload_bytes = json.dumps(
        {
            "session_id": payload.session_id,
            "user_id": payload.user_id,
            "created_at": payload.created_at,
            "expires_at": payload.expires_at,
            "store": store,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.new(settings.secret_key.encode("utf-8"), payload_bytes, sha256).hexdigest()
    return f"{_b64encode(payload_bytes)}.{signature}"


def _decode_signed_token(settings: Settings, token: str) -> SessionPayload | None:
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
    if not isinstance(payload, dict):
        return None
    expires_at_raw = payload.get("expires_at")
    if expires_at_raw:
        try:
            expires_at = datetime.fromisoformat(expires_at_raw)
        except ValueError:
            return None
        if expires_at <= utcnow():
            return None
    session_id = payload.get("session_id")
    user_id = payload.get("user_id")
    created_at = payload.get("created_at")
    store = payload.get("store") or "cookie"
    if not all(isinstance(value, str) and value for value in (session_id, user_id, created_at, store)):
        return None
    return SessionPayload(
        session_id=session_id,
        user_id=user_id,
        created_at=created_at,
        expires_at=expires_at_raw if isinstance(expires_at_raw, str) else None,
        store=store,
    )
