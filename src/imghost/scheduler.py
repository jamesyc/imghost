from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic, time

from .task_catalog import PRUNE_EXPIRED_ALBUMS_TASK
from .tasks import TaskQueue


@dataclass(slots=True)
class SchedulerService:
    tasks: TaskQueue
    poll_seconds: int
    cleanup_interval_seconds: int
    _task: asyncio.Task[None] | None = None
    _next_cleanup_at: float | None = None
    last_tick_at: float | None = None
    last_enqueue_at: float | None = None
    last_enqueue_error: str | None = None

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
        try:
            await self.tasks.enqueue(PRUNE_EXPIRED_ALBUMS_TASK.name, dry_run=False)
        except Exception as exc:
            self.last_enqueue_error = str(exc)
            self._next_cleanup_at = now_mono + self.cleanup_interval_seconds
            return False
        self.last_enqueue_at = time()
        self.last_enqueue_error = None
        self._next_cleanup_at = now_mono + self.cleanup_interval_seconds
        return True

    def runtime_status(self) -> dict[str, object]:
        return {
            "poll_seconds": self.poll_seconds,
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
