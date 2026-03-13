from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from hashlib import sha256
from time import monotonic

from fastapi import HTTPException

from .runtime_config import JsonRuntimeConfig

MINUTE_SECONDS = 60.0
HOUR_SECONDS = 3600.0


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


class InMemoryRateLimiter:
    def __init__(self, runtime_config: JsonRuntimeConfig) -> None:
        self.runtime_config = runtime_config
        self._anon_windows: dict[str, WindowCounter] = {}
        self._user_windows: dict[str, WindowCounter] = {}
        self._global_anon = WindowCounter()

    async def enforce_upload_limits(
        self,
        *,
        actor_key: str,
        byte_count: int,
        authenticated: bool,
    ) -> None:
        now = monotonic()
        if authenticated:
            counter = self._user_windows.setdefault(actor_key, WindowCounter())
            rpm_limit = int(await self.runtime_config.get_value("rate_limit_user_rpm"))
            bph_limit = int(await self.runtime_config.get_value("rate_limit_user_bph"))
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


def hash_anon_identity(ip: str, user_agent: str) -> str:
    return sha256(f"{ip}|{user_agent}".encode("utf-8")).hexdigest()
