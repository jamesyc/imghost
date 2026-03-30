from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from hashlib import sha256
from time import monotonic, time

from fastapi import HTTPException

from .redis_support import RedisHandle, RedisUnavailable
from .runtime_config import PostgresRuntimeConfig
from .telemetry import Telemetry

RATE_LIMIT_DETAIL = "Too many authentication attempts. Try again later."
MINUTE_SECONDS = 60.0


def hash_auth_identifier(value: str) -> str:
    return sha256(value.strip().lower().encode("utf-8")).hexdigest()


class AuthRateLimiter:
    async def enforce_login_attempt(self, *, ip_key: str, login_key: str) -> None:
        raise NotImplementedError

    async def record_login_failure(self, *, ip_key: str, login_key: str) -> None:
        raise NotImplementedError

    async def record_login_success(self, *, login_key: str) -> None:
        raise NotImplementedError

    async def enforce_registration_attempt(self, *, ip_key: str) -> None:
        raise NotImplementedError

    async def enforce_api_key_attempt(self, *, ip_key: str) -> None:
        raise NotImplementedError

    async def record_api_key_failure(self, *, ip_key: str) -> None:
        raise NotImplementedError

    async def record_api_key_success(self, *, ip_key: str) -> None:
        raise NotImplementedError

    async def enforce_admin_attempt(self, *, ip_key: str) -> None:
        raise NotImplementedError

    async def record_admin_denial(self, *, ip_key: str) -> None:
        raise NotImplementedError


@dataclass
class AuthWindowCounter:
    events: deque[float] = field(default_factory=deque)

    def prune(self, *, now: float, window_seconds: float) -> None:
        cutoff = now - window_seconds
        while self.events and self.events[0] <= cutoff:
            self.events.popleft()

    def count(self, *, now: float, window_seconds: float) -> int:
        self.prune(now=now, window_seconds=window_seconds)
        return len(self.events)

    def add(self, *, now: float) -> None:
        self.events.append(now)


class InMemoryAuthRateLimiter(AuthRateLimiter):
    def __init__(self, runtime_config: PostgresRuntimeConfig) -> None:
        self.runtime_config = runtime_config
        self._login_ip_attempts: dict[str, AuthWindowCounter] = {}
        self._registration_ip_attempts: dict[str, AuthWindowCounter] = {}
        self._login_failures: dict[str, AuthWindowCounter] = {}
        self._api_key_failures: dict[str, AuthWindowCounter] = {}
        self._admin_denials: dict[str, AuthWindowCounter] = {}
        self._locks: dict[str, float] = {}

    async def enforce_login_attempt(self, *, ip_key: str, login_key: str) -> None:
        now = monotonic()
        self._check_lock(f"login:account:{login_key}", now=now)
        rpm_limit = int(await self.runtime_config.get_value("auth_rate_limit_login_ip_rpm"))
        counter = self._login_ip_attempts.setdefault(ip_key, AuthWindowCounter())
        self._enforce_attempt_counter(counter, now=now, window_seconds=MINUTE_SECONDS, limit=rpm_limit)
        counter.add(now=now)

    async def record_login_failure(self, *, ip_key: str, login_key: str) -> None:
        del ip_key
        now = monotonic()
        threshold = int(await self.runtime_config.get_value("auth_rate_limit_login_account_failures"))
        window_seconds = int(await self.runtime_config.get_value("auth_rate_limit_login_account_window_seconds"))
        lock_seconds = int(await self.runtime_config.get_value("auth_rate_limit_login_lock_seconds"))
        counter = self._login_failures.setdefault(login_key, AuthWindowCounter())
        counter.add(now=now)
        if threshold > 0 and counter.count(now=now, window_seconds=window_seconds) >= threshold:
            self._locks[f"login:account:{login_key}"] = now + lock_seconds

    async def record_login_success(self, *, login_key: str) -> None:
        self._login_failures.pop(login_key, None)
        self._locks.pop(f"login:account:{login_key}", None)

    async def enforce_registration_attempt(self, *, ip_key: str) -> None:
        now = monotonic()
        rpm_limit = int(await self.runtime_config.get_value("auth_rate_limit_registration_ip_rpm"))
        counter = self._registration_ip_attempts.setdefault(ip_key, AuthWindowCounter())
        self._enforce_attempt_counter(counter, now=now, window_seconds=MINUTE_SECONDS, limit=rpm_limit)
        counter.add(now=now)

    async def enforce_api_key_attempt(self, *, ip_key: str) -> None:
        self._check_lock(f"api_key:ip:{ip_key}", now=monotonic())

    async def record_api_key_failure(self, *, ip_key: str) -> None:
        now = monotonic()
        threshold = int(await self.runtime_config.get_value("auth_rate_limit_api_key_ip_failures"))
        window_seconds = int(await self.runtime_config.get_value("auth_rate_limit_api_key_ip_window_seconds"))
        lock_seconds = int(await self.runtime_config.get_value("auth_rate_limit_api_key_lock_seconds"))
        counter = self._api_key_failures.setdefault(ip_key, AuthWindowCounter())
        counter.add(now=now)
        if threshold > 0 and counter.count(now=now, window_seconds=window_seconds) >= threshold:
            self._locks[f"api_key:ip:{ip_key}"] = now + lock_seconds

    async def record_api_key_success(self, *, ip_key: str) -> None:
        self._api_key_failures.pop(ip_key, None)
        self._locks.pop(f"api_key:ip:{ip_key}", None)

    async def enforce_admin_attempt(self, *, ip_key: str) -> None:
        self._check_lock(f"admin:ip:{ip_key}", now=monotonic())

    async def record_admin_denial(self, *, ip_key: str) -> None:
        now = monotonic()
        threshold = int(await self.runtime_config.get_value("auth_rate_limit_admin_ip_failures"))
        window_seconds = int(await self.runtime_config.get_value("auth_rate_limit_admin_ip_window_seconds"))
        lock_seconds = int(await self.runtime_config.get_value("auth_rate_limit_admin_lock_seconds"))
        counter = self._admin_denials.setdefault(ip_key, AuthWindowCounter())
        counter.add(now=now)
        if threshold > 0 and counter.count(now=now, window_seconds=window_seconds) >= threshold:
            self._locks[f"admin:ip:{ip_key}"] = now + lock_seconds

    def _check_lock(self, key: str, *, now: float) -> None:
        expiry = self._locks.get(key)
        if expiry is None:
            return
        if expiry <= now:
            self._locks.pop(key, None)
            return
        raise HTTPException(status_code=429, detail=RATE_LIMIT_DETAIL)

    def _enforce_attempt_counter(
        self,
        counter: AuthWindowCounter,
        *,
        now: float,
        window_seconds: float,
        limit: int,
    ) -> None:
        if limit <= 0:
            return
        if counter.count(now=now, window_seconds=window_seconds) >= limit:
            raise HTTPException(status_code=429, detail=RATE_LIMIT_DETAIL)


class RedisAuthRateLimiter(AuthRateLimiter):
    def __init__(
        self,
        runtime_config: PostgresRuntimeConfig,
        redis: RedisHandle,
        fallback: InMemoryAuthRateLimiter,
        telemetry: Telemetry,
    ) -> None:
        self.runtime_config = runtime_config
        self.redis = redis
        self.fallback = fallback
        self.telemetry = telemetry

    async def enforce_login_attempt(self, *, ip_key: str, login_key: str) -> None:
        await self._with_redis_fallback(
            "login attempt check",
            lambda: self.fallback.enforce_login_attempt(ip_key=ip_key, login_key=login_key),
            lambda client: self._enforce_login_attempt_with_redis(client, ip_key=ip_key, login_key=login_key),
        )

    async def record_login_failure(self, *, ip_key: str, login_key: str) -> None:
        await self._with_redis_fallback(
            "login failure record",
            lambda: self.fallback.record_login_failure(ip_key=ip_key, login_key=login_key),
            lambda client: self._record_login_failure_with_redis(client, login_key=login_key),
        )

    async def record_login_success(self, *, login_key: str) -> None:
        await self.fallback.record_login_success(login_key=login_key)
        await self._best_effort_clear(
            lambda client: self._record_login_success_with_redis(client, login_key=login_key),
        )

    async def enforce_registration_attempt(self, *, ip_key: str) -> None:
        await self._with_redis_fallback(
            "registration attempt check",
            lambda: self.fallback.enforce_registration_attempt(ip_key=ip_key),
            lambda client: self._enforce_registration_attempt_with_redis(client, ip_key=ip_key),
        )

    async def enforce_api_key_attempt(self, *, ip_key: str) -> None:
        await self._with_redis_fallback(
            "api key attempt check",
            lambda: self.fallback.enforce_api_key_attempt(ip_key=ip_key),
            lambda client: self._enforce_api_key_attempt_with_redis(client, ip_key=ip_key),
        )

    async def record_api_key_failure(self, *, ip_key: str) -> None:
        await self._with_redis_fallback(
            "api key failure record",
            lambda: self.fallback.record_api_key_failure(ip_key=ip_key),
            lambda client: self._record_api_key_failure_with_redis(client, ip_key=ip_key),
        )

    async def record_api_key_success(self, *, ip_key: str) -> None:
        await self.fallback.record_api_key_success(ip_key=ip_key)
        await self._best_effort_clear(
            lambda client: self._record_api_key_success_with_redis(client, ip_key=ip_key),
        )

    async def enforce_admin_attempt(self, *, ip_key: str) -> None:
        await self._with_redis_fallback(
            "admin attempt check",
            lambda: self.fallback.enforce_admin_attempt(ip_key=ip_key),
            lambda client: self._enforce_admin_attempt_with_redis(client, ip_key=ip_key),
        )

    async def record_admin_denial(self, *, ip_key: str) -> None:
        await self._with_redis_fallback(
            "admin denial record",
            lambda: self.fallback.record_admin_denial(ip_key=ip_key),
            lambda client: self._record_admin_denial_with_redis(client, ip_key=ip_key),
        )

    async def _with_redis_fallback(self, operation: str, fallback_call, redis_call) -> None:
        try:
            await self.redis.execute(operation, redis_call)
        except RedisUnavailable:
            self.telemetry.mark_subsystem_degraded(
                "auth_rate_limits",
                operation=operation,
                reason="redis_unavailable",
            )
            await fallback_call()
            return
        self.telemetry.mark_subsystem_recovered("auth_rate_limits", operation=operation)

    async def _best_effort_clear(self, redis_call) -> None:
        try:
            client = await self.redis.get_client()
        except RedisUnavailable:
            return
        try:
            await redis_call(client)
        except Exception:
            return

    async def _enforce_login_attempt_with_redis(self, client: object, *, ip_key: str, login_key: str) -> None:
        await self._raise_if_locked(client, self.redis.prefixed(f"arl:lock:login:account:{login_key}"))
        limit = int(await self.runtime_config.get_value("auth_rate_limit_login_ip_rpm"))
        await self._enforce_rpm_counter(client, f"arl:req:login:ip:{ip_key}", limit)

    async def _record_login_failure_with_redis(self, client: object, *, login_key: str) -> None:
        threshold = int(await self.runtime_config.get_value("auth_rate_limit_login_account_failures"))
        window_seconds = int(await self.runtime_config.get_value("auth_rate_limit_login_account_window_seconds"))
        lock_seconds = int(await self.runtime_config.get_value("auth_rate_limit_login_lock_seconds"))
        await self._record_failure_counter(
            client,
            counter_key=self.redis.prefixed(f"arl:fail:login:account:{login_key}"),
            lock_key=self.redis.prefixed(f"arl:lock:login:account:{login_key}"),
            threshold=threshold,
            window_seconds=window_seconds,
            lock_seconds=lock_seconds,
        )

    async def _record_login_success_with_redis(self, client: object, *, login_key: str) -> None:
        await client.delete(self.redis.prefixed(f"arl:fail:login:account:{login_key}"))
        await client.delete(self.redis.prefixed(f"arl:lock:login:account:{login_key}"))

    async def _enforce_registration_attempt_with_redis(self, client: object, *, ip_key: str) -> None:
        limit = int(await self.runtime_config.get_value("auth_rate_limit_registration_ip_rpm"))
        await self._enforce_rpm_counter(client, f"arl:req:register:ip:{ip_key}", limit)

    async def _enforce_api_key_attempt_with_redis(self, client: object, *, ip_key: str) -> None:
        await self._raise_if_locked(client, self.redis.prefixed(f"arl:lock:api_key:ip:{ip_key}"))

    async def _record_api_key_failure_with_redis(self, client: object, *, ip_key: str) -> None:
        threshold = int(await self.runtime_config.get_value("auth_rate_limit_api_key_ip_failures"))
        window_seconds = int(await self.runtime_config.get_value("auth_rate_limit_api_key_ip_window_seconds"))
        lock_seconds = int(await self.runtime_config.get_value("auth_rate_limit_api_key_lock_seconds"))
        await self._record_failure_counter(
            client,
            counter_key=self.redis.prefixed(f"arl:fail:api_key:ip:{ip_key}"),
            lock_key=self.redis.prefixed(f"arl:lock:api_key:ip:{ip_key}"),
            threshold=threshold,
            window_seconds=window_seconds,
            lock_seconds=lock_seconds,
        )

    async def _record_api_key_success_with_redis(self, client: object, *, ip_key: str) -> None:
        await client.delete(self.redis.prefixed(f"arl:fail:api_key:ip:{ip_key}"))
        await client.delete(self.redis.prefixed(f"arl:lock:api_key:ip:{ip_key}"))

    async def _enforce_admin_attempt_with_redis(self, client: object, *, ip_key: str) -> None:
        await self._raise_if_locked(client, self.redis.prefixed(f"arl:lock:admin:ip:{ip_key}"))

    async def _record_admin_denial_with_redis(self, client: object, *, ip_key: str) -> None:
        threshold = int(await self.runtime_config.get_value("auth_rate_limit_admin_ip_failures"))
        window_seconds = int(await self.runtime_config.get_value("auth_rate_limit_admin_ip_window_seconds"))
        lock_seconds = int(await self.runtime_config.get_value("auth_rate_limit_admin_lock_seconds"))
        await self._record_failure_counter(
            client,
            counter_key=self.redis.prefixed(f"arl:fail:admin:ip:{ip_key}"),
            lock_key=self.redis.prefixed(f"arl:lock:admin:ip:{ip_key}"),
            threshold=threshold,
            window_seconds=window_seconds,
            lock_seconds=lock_seconds,
        )

    async def _enforce_rpm_counter(self, client: object, key_prefix: str, limit: int) -> None:
        if limit <= 0:
            return
        bucket = int(time() // MINUTE_SECONDS)
        key = self.redis.prefixed(f"{key_prefix}:{bucket}")
        current = int(await client.get(key) or "0")
        if current >= limit:
            raise HTTPException(status_code=429, detail=RATE_LIMIT_DETAIL)
        updated = await client.incr(key)
        if updated == 1:
            await client.expire(key, int(MINUTE_SECONDS * 2))

    async def _record_failure_counter(
        self,
        client: object,
        *,
        counter_key: str,
        lock_key: str,
        threshold: int,
        window_seconds: int,
        lock_seconds: int,
    ) -> None:
        updated = await client.incr(counter_key)
        if updated == 1:
            await client.expire(counter_key, window_seconds)
        if threshold > 0 and updated >= threshold:
            await client.set(lock_key, "1", ex=lock_seconds)

    async def _raise_if_locked(self, client: object, key: str) -> None:
        if await client.get(key):
            raise HTTPException(status_code=429, detail=RATE_LIMIT_DETAIL)


def build_auth_rate_limiter(
    runtime_config: PostgresRuntimeConfig,
    redis: RedisHandle,
    telemetry: Telemetry,
) -> AuthRateLimiter:
    fallback = InMemoryAuthRateLimiter(runtime_config)
    if redis.enabled:
        return RedisAuthRateLimiter(runtime_config, redis, fallback, telemetry)
    return fallback
