from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from time import monotonic, time

from .redis_support import RedisHandle, RedisUnavailable
from .task_catalog import PRUNE_EXPIRED_ALBUMS_TASK
from .tasks import TaskQueue

_SCHEDULER_LEASE_RELEASE = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
  return redis.call("DEL", KEYS[1])
end
return 0
"""


@dataclass(slots=True)
class SchedulerService:
    tasks: TaskQueue
    poll_seconds: int
    cleanup_interval_seconds: int
    redis: RedisHandle | None = None
    lease_seconds: int = 900
    _task: asyncio.Task[None] | None = None
    _next_cleanup_at: float | None = None
    last_tick_at: float | None = None
    last_enqueue_at: float | None = None
    last_enqueue_error: str | None = None
    last_lease_acquired_at: float | None = None
    last_lease_error: str | None = None
    _lease_token: str = field(default_factory=lambda: secrets.token_hex(16))

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="imghost-scheduler")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def tick(self, *, now_monotonic: float | None = None) -> bool:
        now_mono = monotonic() if now_monotonic is None else now_monotonic
        self.last_tick_at = time()
        if self._next_cleanup_at is not None and now_mono < self._next_cleanup_at:
            return False
        lease_acquired = await self._acquire_cleanup_lease()
        if not lease_acquired:
            self._next_cleanup_at = now_mono + self.poll_seconds
            return False
        try:
            await self.tasks.enqueue(PRUNE_EXPIRED_ALBUMS_TASK.name, dry_run=False)
        except Exception as exc:
            self.last_enqueue_error = str(exc)
            await self._release_cleanup_lease()
            self._next_cleanup_at = now_mono + self.poll_seconds
            return False
        self.last_enqueue_at = time()
        self.last_enqueue_error = None
        self._next_cleanup_at = now_mono + self.cleanup_interval_seconds
        return True

    def runtime_status(self) -> dict[str, object]:
        return {
            "poll_seconds": self.poll_seconds,
            "lease_enabled": self._lease_enabled,
            "lease_seconds": self._effective_lease_seconds,
            "last_lease_acquired_at": self.last_lease_acquired_at,
            "last_lease_error": self.last_lease_error,
            "last_tick_at": self.last_tick_at,
            "last_enqueue_at": self.last_enqueue_at,
            "last_enqueue_error": self.last_enqueue_error,
            "jobs": {
                PRUNE_EXPIRED_ALBUMS_TASK.name: {
                    "interval_seconds": self.cleanup_interval_seconds,
                    "queue": PRUNE_EXPIRED_ALBUMS_TASK.default_queue,
                }
            },
        }

    async def _run(self) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(self.poll_seconds)

    @property
    def _lease_enabled(self) -> bool:
        return self.redis is not None and self.redis.enabled

    @property
    def _effective_lease_seconds(self) -> int:
        return max(self.lease_seconds, self.cleanup_interval_seconds)

    def _cleanup_lease_key(self) -> str:
        if self.redis is None:
            return "scheduler:lease:cleanup"
        return self.redis.prefixed(f"scheduler:lease:{PRUNE_EXPIRED_ALBUMS_TASK.name}")

    async def _acquire_cleanup_lease(self) -> bool:
        if not self._lease_enabled:
            self.last_lease_error = None
            return True
        try:
            acquired = await self.redis.execute(
                "acquire scheduler cleanup lease",
                lambda client: client.set(
                    self._cleanup_lease_key(),
                    self._lease_token,
                    ex=self._effective_lease_seconds,
                    nx=True,
                ),
            )
        except RedisUnavailable as exc:
            self.last_lease_error = str(exc)
            return False
        if not acquired:
            self.last_lease_error = None
            return False
        self.last_lease_acquired_at = time()
        self.last_lease_error = None
        return True

    async def _release_cleanup_lease(self) -> None:
        if not self._lease_enabled:
            return
        try:
            await self.redis.execute(
                "release scheduler cleanup lease",
                lambda client: client.eval(
                    _SCHEDULER_LEASE_RELEASE,
                    1,
                    self._cleanup_lease_key(),
                    self._lease_token,
                ),
            )
        except RedisUnavailable as exc:
            self.last_lease_error = str(exc)
