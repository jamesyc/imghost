from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from hashlib import sha256
from time import monotonic, time

from fastapi import HTTPException

from .models import User
from .redis_support import RedisHandle, RedisUnavailable
from .runtime_config import PostgresRuntimeConfig

MINUTE_SECONDS = 60.0
HOUR_SECONDS = 3600.0


class RateLimiter:
    async def enforce_upload_limits(
        self,
        *,
        actor_key: str,
        byte_count: int,
        user: User | None,
    ) -> None:
        raise NotImplementedError


@dataclass
class WindowCounter:
    events: deque[tuple[float, int]] = field(default_factory=deque)

    def prune(self, *, now: float, window_seconds: float) -> None:
        cutoff = now - window_seconds
        while self.events and self.events[0][0] <= cutoff:
            self.events.popleft()

    def count(self, *, now: float, window_seconds: float) -> int:
        self.prune(now=now, window_seconds=window_seconds)
        return len(self.events)

    def bytes_used(self, *, now: float, window_seconds: float) -> int:
        self.prune(now=now, window_seconds=window_seconds)
        return sum(size for _, size in self.events)

    def add(self, *, now: float, byte_count: int) -> None:
        self.events.append((now, byte_count))


class InMemoryRateLimiter(RateLimiter):
    def __init__(self, runtime_config: PostgresRuntimeConfig) -> None:
        self.runtime_config = runtime_config
        self._anon_windows: dict[str, WindowCounter] = {}
        self._user_windows: dict[str, WindowCounter] = {}
        self._global_anon = WindowCounter()

    async def enforce_upload_limits(
        self,
        *,
        actor_key: str,
        byte_count: int,
        user: User | None,
    ) -> None:
        now = monotonic()
        if user is not None:
            counter = self._user_windows.setdefault(actor_key, WindowCounter())
            rpm_limit = user.rate_limit_rpm if user.rate_limit_rpm is not None else int(await self.runtime_config.get_value("rate_limit_user_rpm"))
            bph_limit = user.rate_limit_bph if user.rate_limit_bph is not None else int(await self.runtime_config.get_value("rate_limit_user_bph"))
            self._enforce_counter(counter, now=now, rpm_limit=rpm_limit, bph_limit=bph_limit, byte_count=byte_count)
            counter.add(now=now, byte_count=byte_count)
            return

        counter = self._anon_windows.setdefault(actor_key, WindowCounter())
        rpm_limit = int(await self.runtime_config.get_value("rate_limit_anon_rpm"))
        bph_limit = int(await self.runtime_config.get_value("rate_limit_anon_bph"))
        global_rpm_limit = int(await self.runtime_config.get_value("rate_limit_global_anon_rpm"))
        global_bph_limit = int(await self.runtime_config.get_value("rate_limit_global_anon_bph"))

        self._enforce_counter(counter, now=now, rpm_limit=rpm_limit, bph_limit=bph_limit, byte_count=byte_count)
        self._enforce_counter(
            self._global_anon,
            now=now,
            rpm_limit=global_rpm_limit,
            bph_limit=global_bph_limit,
            byte_count=byte_count,
        )
        counter.add(now=now, byte_count=byte_count)
        self._global_anon.add(now=now, byte_count=byte_count)

    def _enforce_counter(
        self,
        counter: WindowCounter,
        *,
        now: float,
        rpm_limit: int,
        bph_limit: int,
        byte_count: int,
    ) -> None:
        requests_this_minute = counter.count(now=now, window_seconds=MINUTE_SECONDS)
        if rpm_limit > 0 and requests_this_minute >= rpm_limit:
            raise HTTPException(status_code=429, detail="Upload rate limit exceeded.")

        bytes_this_hour = counter.bytes_used(now=now, window_seconds=HOUR_SECONDS)
        if bph_limit > 0 and bytes_this_hour + byte_count > bph_limit:
            raise HTTPException(status_code=429, detail="Upload bandwidth limit exceeded.")


class RedisRateLimiter(RateLimiter):
    def __init__(self, runtime_config: PostgresRuntimeConfig, redis: RedisHandle, fallback: InMemoryRateLimiter) -> None:
        self.runtime_config = runtime_config
        self.redis = redis
        self.fallback = fallback

    async def enforce_upload_limits(
        self,
        *,
        actor_key: str,
        byte_count: int,
        user: User | None,
    ) -> None:
        try:
            await self.redis.execute(
                "rate limit check",
                lambda client: self._enforce_with_redis(
                    client,
                    actor_key=actor_key,
                    byte_count=byte_count,
                    user=user,
                ),
            )
        except RedisUnavailable:
            await self.fallback.enforce_upload_limits(actor_key=actor_key, byte_count=byte_count, user=user)

    async def _enforce_with_redis(
        self,
        client: object,
        *,
        actor_key: str,
        byte_count: int,
        user: User | None,
    ) -> None:
        now = int(time())
        minute_bucket = now // 60
        if user is not None:
            rpm_limit = user.rate_limit_rpm if user.rate_limit_rpm is not None else int(await self.runtime_config.get_value("rate_limit_user_rpm"))
            bph_limit = user.rate_limit_bph if user.rate_limit_bph is not None else int(await self.runtime_config.get_value("rate_limit_user_bph"))
            await self._enforce_counter(client, f"user:{actor_key}", minute_bucket, rpm_limit, bph_limit, byte_count)
            return

        rpm_limit = int(await self.runtime_config.get_value("rate_limit_anon_rpm"))
        bph_limit = int(await self.runtime_config.get_value("rate_limit_anon_bph"))
        global_rpm_limit = int(await self.runtime_config.get_value("rate_limit_global_anon_rpm"))
        global_bph_limit = int(await self.runtime_config.get_value("rate_limit_global_anon_bph"))
        await self._enforce_counter(client, f"anon:{actor_key}", minute_bucket, rpm_limit, bph_limit, byte_count)
        await self._enforce_counter(client, "anon:global", minute_bucket, global_rpm_limit, global_bph_limit, byte_count)

    async def _enforce_counter(
        self,
        client: object,
        scope: str,
        minute_bucket: int,
        rpm_limit: int,
        bph_limit: int,
        byte_count: int,
    ) -> None:
        req_key = self.redis.prefixed(f"rl:req:{scope}:{minute_bucket}")
        bytes_key = self.redis.prefixed(f"rl:bytes:{scope}")

        if rpm_limit > 0:
            current_requests = await client.get(req_key)
            requests_this_minute = int(current_requests or "0")
            if requests_this_minute >= rpm_limit:
                raise HTTPException(status_code=429, detail="Upload rate limit exceeded.")

        if bph_limit > 0:
            bucket_values = await client.hgetall(bytes_key)
            bytes_this_hour = 0
            stale_fields: list[str] = []
            cutoff = minute_bucket - 59
            for field, raw_value in bucket_values.items():
                bucket = int(field)
                if bucket < cutoff:
                    stale_fields.append(field)
                    continue
                bytes_this_hour += int(raw_value)
            if stale_fields:
                await client.hdel(bytes_key, *stale_fields)
            if bytes_this_hour + byte_count > bph_limit:
                raise HTTPException(status_code=429, detail="Upload bandwidth limit exceeded.")

        await client.incr(req_key)
        await client.expire(req_key, 120)
        await client.hincrby(bytes_key, str(minute_bucket), byte_count)
        await client.expire(bytes_key, int(HOUR_SECONDS) + 120)


def build_rate_limiter(runtime_config: PostgresRuntimeConfig, redis: RedisHandle) -> RateLimiter:
    fallback = InMemoryRateLimiter(runtime_config)
    if redis.enabled:
        return RedisRateLimiter(runtime_config, redis, fallback)
    return fallback


def hash_anon_identity(ip: str, user_agent: str) -> str:
    return sha256(f"{ip}|{user_agent}".encode("utf-8")).hexdigest()
