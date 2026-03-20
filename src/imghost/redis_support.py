from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any, TypeVar

from .config import Settings

logger = logging.getLogger(__name__)
T = TypeVar("T")

try:
    from redis import asyncio as redis_async
    from redis.exceptions import RedisError
except ImportError:  # pragma: no cover - exercised in tests via monkeypatch
    redis_async = None
    RedisError = None


class RedisUnavailable(RuntimeError):
    pass


def _is_redis_exception(exc: BaseException) -> bool:
    if RedisError is not None and isinstance(exc, RedisError):
        return True
    return isinstance(exc, (OSError, TimeoutError))


class RedisHandle:
    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: Callable[[str], Any] | None = None,
        cooldown_seconds: float = 5.0,
    ) -> None:
        self.url = settings.redis_url
        self.mode = settings.redis_mode
        self.prefix = settings.redis_prefix.strip(":")
        self._client_factory = client_factory
        self._cooldown_seconds = cooldown_seconds
        self._blocked_until = 0.0
        self._client: Any | None = None
        self._degraded = False

        if self.mode not in {"auto", "required", "disabled"}:
            raise ValueError("REDIS_MODE must be one of: auto, required, disabled.")

    @property
    def enabled(self) -> bool:
        return self.mode != "disabled" and bool(self.url)

    @property
    def available(self) -> bool:
        return self.enabled and monotonic() >= self._blocked_until

    def prefixed(self, key: str) -> str:
        return f"{self.prefix}:{key}" if self.prefix else key

    async def get_client(self) -> Any:
        if not self.enabled:
            raise RedisUnavailable("Redis is disabled.")
        if not self.available:
            raise RedisUnavailable("Redis is temporarily unavailable.")
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory(self.url or "")
            elif redis_async is not None:
                self._client = redis_async.from_url(self.url or "", decode_responses=True)
            else:
                raise RedisUnavailable("redis dependency is not installed.")
        return self._client

    async def execute(self, operation: str, callback: Callable[[Any], Awaitable[T]]) -> T:
        try:
            client = await self.get_client()
        except RedisUnavailable:
            raise

        try:
            result = await callback(client)
        except Exception as exc:
            if not _is_redis_exception(exc):
                raise
            self._mark_failure(operation, exc)
            raise RedisUnavailable(f"Redis unavailable during {operation}.") from exc

        self._mark_success(operation)
        return result

    async def close(self) -> None:
        if self._client is None:
            return
        close = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result
        self._client = None

    async def ensure_startup_ready(self) -> None:
        if not self.enabled:
            return
        try:
            await self.execute("startup ping", lambda client: client.ping())
        except RedisUnavailable:
            if self.mode == "required":
                raise RuntimeError("Redis is required but unavailable during startup.")
            logger.warning("redis_startup_degraded")

    def _mark_failure(self, operation: str, exc: BaseException) -> None:
        self._blocked_until = monotonic() + self._cooldown_seconds
        if not self._degraded:
            logger.warning("redis_degraded", extra={"operation": operation, "error": str(exc)})
            self._degraded = True

    def _mark_success(self, operation: str) -> None:
        self._blocked_until = 0.0
        if self._degraded:
            logger.info("redis_recovered", extra={"operation": operation})
            self._degraded = False

