from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic

from .telemetry import Telemetry
from .processors import ProcessorRegistry
from .redis_support import RedisHandle, RedisUnavailable
from .repositories import PostgresRepository
from .storage import StorageBackend

TaskHandler = Callable[..., Awaitable[None]]
logger = logging.getLogger(__name__)
KNOWN_QUEUES = ("default", "thumbnails")


def normalize_task_queues(queues: tuple[str, ...] | list[str] | None) -> tuple[str, ...] | None:
    if queues is None:
        return None
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_queue in queues:
        queue = str(raw_queue).strip().lower()
        if not queue or queue in seen:
            continue
        seen.add(queue)
        normalized.append(queue)
    return tuple(normalized)


@dataclass(slots=True)
class TaskContext:
    repository: PostgresRepository
    storage: StorageBackend
    processors: ProcessorRegistry


class TaskQueue:
    def register(self, task_name: str, handler: TaskHandler) -> None:
        raise NotImplementedError

    async def enqueue(self, task_name: str, queue: str = "default", **kwargs) -> None:
        raise NotImplementedError

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def join(self) -> None:
        return None

    async def runtime_status(self) -> dict[str, object]:
        return {"queue_backend": "unknown"}


@dataclass(slots=True)
class QueuedTask:
    task_name: str
    kwargs: dict[str, object]


class AsyncTaskQueue(TaskQueue):
    def __init__(self, context: TaskContext, worker_count: int = 1, telemetry: Telemetry | None = None) -> None:
        self.context = context
        self.telemetry = telemetry
        self.worker_count = max(1, worker_count)
        self._handlers: dict[str, TaskHandler] = {}
        self._queue: asyncio.Queue[QueuedTask | None] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []

    def register(self, task_name: str, handler: TaskHandler) -> None:
        self._handlers[task_name] = handler

    async def start(self) -> None:
        if self._workers:
            return
        self._workers = [asyncio.create_task(self._run_worker(index)) for index in range(self.worker_count)]

    async def stop(self) -> None:
        if not self._workers:
            return
        for _ in self._workers:
            await self._queue.put(None)
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []

    async def enqueue(self, task_name: str, queue: str = "default", **kwargs) -> None:
        if task_name not in self._handlers:
            raise KeyError(task_name)
        await self._queue.put(QueuedTask(task_name=task_name, kwargs=kwargs))
        if self.telemetry is not None:
            self.telemetry.record_task_enqueued(queue=queue, task_name=task_name)

    async def _run_worker(self, worker_index: int) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                handler = self._handlers[item.task_name]
                await handler(**item.kwargs)
            except Exception:
                logger.exception("task_worker_failed", extra={"worker_index": worker_index})
            finally:
                self._queue.task_done()

    async def join(self) -> None:
        await self._queue.join()

    async def runtime_status(self) -> dict[str, object]:
        return {
            "queue_backend": "async",
            "worker_count": self.worker_count,
            "active_workers": len(self._workers),
            "queue_depth": self._queue.qsize(),
        }


class SyncTaskQueue(TaskQueue):
    def __init__(self, context: TaskContext, telemetry: Telemetry | None = None) -> None:
        self.context = context
        self.telemetry = telemetry
        self._handlers: dict[str, TaskHandler] = {}

    def register(self, task_name: str, handler: TaskHandler) -> None:
        self._handlers[task_name] = handler

    async def enqueue(self, task_name: str, queue: str = "default", **kwargs) -> None:
        handler = self._handlers[task_name]
        if self.telemetry is not None:
            self.telemetry.record_task_enqueued(queue=queue, task_name=task_name)
        await handler(**kwargs)

    async def runtime_status(self) -> dict[str, object]:
        return {"queue_backend": "sync"}


class RedisTaskQueue(TaskQueue):
    def __init__(
        self,
        redis: RedisHandle,
        context: TaskContext,
        telemetry: Telemetry,
        *,
        worker_count: int = 1,
        run_worker: bool = True,
        worker_queues: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        self.redis = redis
        self.context = context
        self.telemetry = telemetry
        self.worker_count = max(1, worker_count)
        self.run_worker = run_worker
        self.worker_queues = normalize_task_queues(worker_queues)
        self._handlers: dict[str, TaskHandler] = {}
        self._fallback = AsyncTaskQueue(context, worker_count=worker_count, telemetry=telemetry)
        self._workers: list[asyncio.Task[None]] = []
        self._active_jobs = 0
        self._known_queues = set(KNOWN_QUEUES)
        if self.worker_queues is not None:
            self._known_queues.update(self.worker_queues)

    def register(self, task_name: str, handler: TaskHandler) -> None:
        self._handlers[task_name] = handler
        self._fallback.register(task_name, handler)

    async def start(self) -> None:
        await self._fallback.start()
        if not self.run_worker or self._workers:
            return
        if not self._dequeue_queue_names():
            return
        self.telemetry.mark_worker_started()
        self._workers = [asyncio.create_task(self._run_worker(index)) for index in range(self.worker_count)]

    async def stop(self) -> None:
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers = []
            self.telemetry.mark_worker_stopped()
        await self._fallback.stop()

    async def enqueue(self, task_name: str, queue: str = "default", **kwargs) -> None:
        if task_name not in self._handlers:
            raise KeyError(task_name)
        self._known_queues.add(queue)
        message = json.dumps({"task_name": task_name, "kwargs": kwargs}, separators=(",", ":"))
        try:
            await self.redis.execute(
                "enqueue task",
                lambda client: client.rpush(self.redis.prefixed(f"queue:{queue}"), message),
            )
        except RedisUnavailable:
            self.telemetry.mark_subsystem_degraded(
                "tasks",
                operation="enqueue task",
                reason="redis_unavailable",
            )
            await self._fallback.enqueue(task_name, queue=queue, **kwargs)
            return
        self.telemetry.mark_subsystem_recovered("tasks", operation="enqueue task")
        self.telemetry.record_task_enqueued(queue=queue, task_name=task_name)

    async def join(self) -> None:
        await self._fallback.join()
        if not self.run_worker:
            return
        deadline = monotonic() + 5.0
        while monotonic() < deadline:
            if self._active_jobs == 0 and await self._all_queues_empty():
                return
            await asyncio.sleep(0.05)

    async def _all_queues_empty(self) -> bool:
        try:
            lengths = await self.redis.execute(
                "check queue lengths",
                lambda client: self._read_queue_lengths(client, queue_names=self._join_queue_names()),
            )
        except RedisUnavailable:
            return True
        return all(length == 0 for length in lengths)

    async def _read_queue_lengths(self, client: object, *, queue_names: tuple[str, ...]) -> list[int]:
        lengths: list[int] = []
        for queue in queue_names:
            lengths.append(int(await client.llen(self.redis.prefixed(f"queue:{queue}"))))
        return lengths

    async def _run_worker(self, worker_index: int) -> None:
        while True:
            queue_keys = [self.redis.prefixed(f"queue:{queue}") for queue in self._dequeue_queue_names()]
            if not queue_keys:
                await asyncio.sleep(0.25)
                continue
            try:
                item = await self.redis.execute(
                    "dequeue task",
                    lambda client: client.blpop(queue_keys, timeout=1),
                )
            except RedisUnavailable:
                self.telemetry.mark_subsystem_degraded(
                    "tasks",
                    operation="dequeue task",
                    reason="redis_unavailable",
                )
                await asyncio.sleep(0.25)
                continue
            except asyncio.CancelledError:
                raise
            self.telemetry.mark_subsystem_recovered("tasks", operation="dequeue task")
            if item is None:
                continue
            _, raw_payload = item
            payload: dict[str, object] | None = None
            try:
                payload = json.loads(raw_payload)
                task_name = payload["task_name"]
                kwargs = payload["kwargs"]
                handler = self._handlers[task_name]
                self._active_jobs += 1
                await handler(**kwargs)
            except asyncio.CancelledError:
                raise
            except Exception:
                task_name = str(payload.get("task_name")) if isinstance(payload, dict) else "unknown"
                details = {"worker_index": worker_index}
                if isinstance(payload, dict):
                    kwargs = payload.get("kwargs")
                    if isinstance(kwargs, dict):
                        details.update({key: kwargs.get(key) for key in ("media_id", "correlation_id")})
                self.telemetry.record_task_failure(task_name=task_name, details=details)
                logger.exception("redis_task_worker_failed", extra={"worker_index": worker_index})
            finally:
                if self._active_jobs > 0:
                    self._active_jobs -= 1

    async def runtime_status(self) -> dict[str, object]:
        queues = await self._safe_queue_lengths()
        return {
            "queue_backend": "redis",
            "worker_count": self.worker_count,
            "worker_enabled": self.run_worker,
            "worker_queues": list(self._dequeue_queue_names()),
            "active_workers": len(self._workers),
            "active_jobs": self._active_jobs,
            "queue_depth": sum(queues.values()),
            "queues": queues,
        }

    async def _safe_queue_lengths(self) -> dict[str, int]:
        try:
            lengths = await self.redis.execute(
                "check queue lengths",
                lambda client: self._read_named_queue_lengths(client),
            )
        except RedisUnavailable:
            self.telemetry.mark_subsystem_degraded(
                "tasks",
                operation="check queue lengths",
                reason="redis_unavailable",
            )
            return {}
        self.telemetry.mark_subsystem_recovered("tasks", operation="check queue lengths")
        return lengths

    async def _read_named_queue_lengths(self, client: object) -> dict[str, int]:
        lengths: dict[str, int] = {}
        for queue in sorted(self._known_queues):
            lengths[queue] = int(await client.llen(self.redis.prefixed(f"queue:{queue}")))
        return lengths

    def _dequeue_queue_names(self) -> tuple[str, ...]:
        if self.worker_queues is None:
            return tuple(sorted(self._known_queues))
        return self.worker_queues

    def _join_queue_names(self) -> tuple[str, ...]:
        if self.worker_queues is None:
            return tuple(sorted(self._known_queues))
        return tuple(queue for queue in self.worker_queues if queue in self._known_queues)
