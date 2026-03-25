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

TaskHandler = Callable[..., Awaitable[object]]
logger = logging.getLogger(__name__)
KNOWN_QUEUES = ("default", "thumbnails")
TASK_STATE_QUEUED = "queued"
TASK_STATE_STARTED = "started"
TASK_STATE_SKIPPED = "skipped"
TASK_STATE_RETRY_SCHEDULED = "retry_scheduled"
TASK_STATE_SUCCEEDED = "succeeded"
TASK_STATE_FAILED_PERMANENT = "failed_permanent"
TASK_STATE_FAILED_EXHAUSTED = "failed_exhausted"
TASK_STATES = (
    TASK_STATE_QUEUED,
    TASK_STATE_STARTED,
    TASK_STATE_SKIPPED,
    TASK_STATE_RETRY_SCHEDULED,
    TASK_STATE_SUCCEEDED,
    TASK_STATE_FAILED_PERMANENT,
    TASK_STATE_FAILED_EXHAUSTED,
)
_TASK_ATTEMPT_KEY = "_task_attempt"
_TASK_MAX_ATTEMPTS_KEY = "_task_max_attempts"


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


@dataclass(slots=True, frozen=True)
class TaskRunResult:
    state: str
    reason: str | None = None
    retryable: bool = False


class TaskQueue:
    def register(
        self,
        task_name: str,
        handler: TaskHandler,
        *,
        default_queue: str = "default",
        max_attempts: int = 1,
        pass_retry_metadata: bool = False,
    ) -> None:
        raise NotImplementedError

    async def enqueue(self, task_name: str, queue: str | None = None, **kwargs) -> None:
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
    attempt: int
    max_attempts: int


@dataclass(slots=True)
class RegisteredTask:
    handler: TaskHandler
    default_queue: str
    max_attempts: int = 1
    pass_retry_metadata: bool = False


class AsyncTaskQueue(TaskQueue):
    def __init__(self, context: TaskContext, worker_count: int = 1, telemetry: Telemetry | None = None) -> None:
        self.context = context
        self.telemetry = telemetry
        self.worker_count = max(1, worker_count)
        self._tasks: dict[str, RegisteredTask] = {}
        self._queue: asyncio.Queue[QueuedTask | None] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []

    def register(
        self,
        task_name: str,
        handler: TaskHandler,
        *,
        default_queue: str = "default",
        max_attempts: int = 1,
        pass_retry_metadata: bool = False,
    ) -> None:
        self._tasks[task_name] = RegisteredTask(
            handler=handler,
            default_queue=default_queue,
            max_attempts=max(1, max_attempts),
            pass_retry_metadata=pass_retry_metadata,
        )

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

    async def enqueue(self, task_name: str, queue: str | None = None, **kwargs) -> None:
        if task_name not in self._tasks:
            raise KeyError(task_name)
        resolved_queue = self._resolve_queue_name(task_name, queue)
        attempt, max_attempts = self._normalize_attempt_metadata(task_name, kwargs)
        await self._queue.put(QueuedTask(task_name=task_name, kwargs=kwargs, attempt=attempt, max_attempts=max_attempts))
        if self.telemetry is not None:
            self.telemetry.record_task_enqueued(queue=resolved_queue, task_name=task_name)
            self.telemetry.record_task_state(
                task_name=task_name,
                state=TASK_STATE_QUEUED,
                details={"queue": resolved_queue, "attempt": attempt, "max_attempts": max_attempts, **kwargs},
            )

    async def _run_worker(self, worker_index: int) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                await self._execute_task(item, queue_name=self._resolve_queue_name(item.task_name, None))
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

    def _resolve_queue_name(self, task_name: str, queue: str | None) -> str:
        if queue is not None:
            return queue
        return self._tasks[task_name].default_queue

    def _normalize_attempt_metadata(self, task_name: str, kwargs: dict[str, object]) -> tuple[int, int]:
        task = self._tasks[task_name]
        attempt = int(kwargs.pop(_TASK_ATTEMPT_KEY, 1))
        max_attempts = int(kwargs.pop(_TASK_MAX_ATTEMPTS_KEY, task.max_attempts))
        return max(1, attempt), max(1, max_attempts)

    async def _execute_task(self, item: QueuedTask, *, queue_name: str) -> None:
        task = self._tasks[item.task_name]
        handler_kwargs = dict(item.kwargs)
        if task.pass_retry_metadata:
            handler_kwargs["attempt"] = item.attempt
            handler_kwargs["max_attempts"] = item.max_attempts
        if self.telemetry is not None:
            self.telemetry.record_task_state(
                task_name=item.task_name,
                state=TASK_STATE_STARTED,
                details={"queue": queue_name, "attempt": item.attempt, "max_attempts": item.max_attempts, **item.kwargs},
            )
        try:
            result = await task.handler(**handler_kwargs)
        except Exception as exc:
            details = {
                "queue": queue_name,
                "attempt": item.attempt,
                "max_attempts": item.max_attempts,
                "reason": str(exc) or type(exc).__name__,
                **item.kwargs,
            }
            if self.telemetry is not None:
                self.telemetry.record_task_state(
                    task_name=item.task_name,
                    state=TASK_STATE_FAILED_PERMANENT,
                    details=details,
                )
                self.telemetry.record_task_failure(task_name=item.task_name, details=details)
            logger.exception("async_task_worker_failed", extra={"task_name": item.task_name, "queue": queue_name})
            return
        await self._handle_task_result(
            item.task_name,
            queue_name=queue_name,
            kwargs=item.kwargs,
            attempt=item.attempt,
            max_attempts=item.max_attempts,
            result=result,
        )

    async def _handle_task_result(
        self,
        task_name: str,
        *,
        queue_name: str,
        kwargs: dict[str, object],
        attempt: int,
        max_attempts: int,
        result: object,
    ) -> None:
        if not isinstance(result, TaskRunResult):
            if self.telemetry is not None:
                self.telemetry.record_task_state(
                    task_name=task_name,
                    state=TASK_STATE_SUCCEEDED,
                    details={"queue": queue_name, "attempt": attempt, "max_attempts": max_attempts, **kwargs},
                )
            return
        details = {
            "queue": queue_name,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "reason": result.reason,
            **kwargs,
        }
        if result.state == TASK_STATE_RETRY_SCHEDULED and result.retryable and attempt < max_attempts:
            if self.telemetry is not None:
                self.telemetry.record_task_state(task_name=task_name, state=TASK_STATE_RETRY_SCHEDULED, details=details)
            await self.enqueue(
                task_name,
                queue=queue_name,
                _task_attempt=attempt + 1,
                _task_max_attempts=max_attempts,
                **kwargs,
            )
            return
        terminal_state = result.state
        if result.state == TASK_STATE_RETRY_SCHEDULED:
            terminal_state = TASK_STATE_FAILED_EXHAUSTED
        if self.telemetry is not None:
            self.telemetry.record_task_state(task_name=task_name, state=terminal_state, details=details)
            if terminal_state in {TASK_STATE_FAILED_PERMANENT, TASK_STATE_FAILED_EXHAUSTED}:
                self.telemetry.record_task_failure(task_name=task_name, details=details)


class SyncTaskQueue(TaskQueue):
    def __init__(self, context: TaskContext, telemetry: Telemetry | None = None) -> None:
        self.context = context
        self.telemetry = telemetry
        self._tasks: dict[str, RegisteredTask] = {}

    def register(
        self,
        task_name: str,
        handler: TaskHandler,
        *,
        default_queue: str = "default",
        max_attempts: int = 1,
        pass_retry_metadata: bool = False,
    ) -> None:
        self._tasks[task_name] = RegisteredTask(
            handler=handler,
            default_queue=default_queue,
            max_attempts=max(1, max_attempts),
            pass_retry_metadata=pass_retry_metadata,
        )

    async def enqueue(self, task_name: str, queue: str | None = None, **kwargs) -> None:
        task = self._tasks[task_name]
        resolved_queue = queue if queue is not None else task.default_queue
        attempt = int(kwargs.pop(_TASK_ATTEMPT_KEY, 1))
        max_attempts = int(kwargs.pop(_TASK_MAX_ATTEMPTS_KEY, task.max_attempts))
        if self.telemetry is not None:
            self.telemetry.record_task_enqueued(queue=resolved_queue, task_name=task_name)
            self.telemetry.record_task_state(
                task_name=task_name,
                state=TASK_STATE_QUEUED,
                details={"queue": resolved_queue, "attempt": attempt, "max_attempts": max_attempts, **kwargs},
            )
        while True:
            handler_kwargs = dict(kwargs)
            if task.pass_retry_metadata:
                handler_kwargs["attempt"] = attempt
                handler_kwargs["max_attempts"] = max_attempts
            if self.telemetry is not None:
                self.telemetry.record_task_state(
                    task_name=task_name,
                    state=TASK_STATE_STARTED,
                    details={"queue": resolved_queue, "attempt": attempt, "max_attempts": max_attempts, **kwargs},
                )
            try:
                result = await task.handler(**handler_kwargs)
            except Exception as exc:
                details = {
                    "queue": resolved_queue,
                    "attempt": attempt,
                    "max_attempts": max_attempts,
                    "reason": str(exc) or type(exc).__name__,
                    **kwargs,
                }
                if self.telemetry is not None:
                    self.telemetry.record_task_state(task_name=task_name, state=TASK_STATE_FAILED_PERMANENT, details=details)
                    self.telemetry.record_task_failure(task_name=task_name, details=details)
                logger.exception("sync_task_failed", extra={"task_name": task_name, "queue": resolved_queue})
                return
            if not isinstance(result, TaskRunResult):
                if self.telemetry is not None:
                    self.telemetry.record_task_state(
                        task_name=task_name,
                        state=TASK_STATE_SUCCEEDED,
                        details={"queue": resolved_queue, "attempt": attempt, "max_attempts": max_attempts, **kwargs},
                    )
                return
            details = {
                "queue": resolved_queue,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "reason": result.reason,
                **kwargs,
            }
            if result.state == TASK_STATE_RETRY_SCHEDULED and result.retryable and attempt < max_attempts:
                if self.telemetry is not None:
                    self.telemetry.record_task_state(task_name=task_name, state=TASK_STATE_RETRY_SCHEDULED, details=details)
                    self.telemetry.record_task_state(
                        task_name=task_name,
                        state=TASK_STATE_QUEUED,
                        details={"queue": resolved_queue, "attempt": attempt + 1, "max_attempts": max_attempts, **kwargs},
                    )
                attempt += 1
                continue
            terminal_state = result.state if result.state != TASK_STATE_RETRY_SCHEDULED else TASK_STATE_FAILED_EXHAUSTED
            if self.telemetry is not None:
                self.telemetry.record_task_state(task_name=task_name, state=terminal_state, details=details)
                if terminal_state in {TASK_STATE_FAILED_PERMANENT, TASK_STATE_FAILED_EXHAUSTED}:
                    self.telemetry.record_task_failure(task_name=task_name, details=details)
            return

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
        self._tasks: dict[str, RegisteredTask] = {}
        self._fallback = AsyncTaskQueue(context, worker_count=worker_count, telemetry=telemetry)
        self._workers: list[asyncio.Task[None]] = []
        self._active_jobs = 0
        self._known_queues = set(KNOWN_QUEUES)
        if self.worker_queues is not None:
            self._known_queues.update(self.worker_queues)

    def register(
        self,
        task_name: str,
        handler: TaskHandler,
        *,
        default_queue: str = "default",
        max_attempts: int = 1,
        pass_retry_metadata: bool = False,
    ) -> None:
        registered = RegisteredTask(
            handler=handler,
            default_queue=default_queue,
            max_attempts=max(1, max_attempts),
            pass_retry_metadata=pass_retry_metadata,
        )
        self._tasks[task_name] = registered
        self._fallback.register(
            task_name,
            handler,
            default_queue=default_queue,
            max_attempts=max_attempts,
            pass_retry_metadata=pass_retry_metadata,
        )

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

    async def enqueue(self, task_name: str, queue: str | None = None, **kwargs) -> None:
        if task_name not in self._tasks:
            raise KeyError(task_name)
        task = self._tasks[task_name]
        resolved_queue = queue if queue is not None else task.default_queue
        attempt = int(kwargs.pop(_TASK_ATTEMPT_KEY, 1))
        max_attempts = int(kwargs.pop(_TASK_MAX_ATTEMPTS_KEY, task.max_attempts))
        self._known_queues.add(resolved_queue)
        message = json.dumps(
            {"task_name": task_name, "kwargs": kwargs, "attempt": attempt, "max_attempts": max_attempts},
            separators=(",", ":"),
        )
        try:
            await self.redis.execute(
                "enqueue task",
                lambda client: client.rpush(self.redis.prefixed(f"queue:{resolved_queue}"), message),
            )
        except RedisUnavailable:
            self.telemetry.mark_subsystem_degraded(
                "tasks",
                operation="enqueue task",
                reason="redis_unavailable",
            )
            await self._fallback.enqueue(
                task_name,
                queue=resolved_queue,
                _task_attempt=attempt,
                _task_max_attempts=max_attempts,
                **kwargs,
            )
            return
        self.telemetry.mark_subsystem_recovered("tasks", operation="enqueue task")
        self.telemetry.record_task_enqueued(queue=resolved_queue, task_name=task_name)
        self.telemetry.record_task_state(
            task_name=task_name,
            state=TASK_STATE_QUEUED,
            details={"queue": resolved_queue, "attempt": attempt, "max_attempts": max_attempts, **kwargs},
        )

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
            queue_key, raw_payload = item
            payload: dict[str, object] | None = None
            try:
                payload = json.loads(raw_payload)
                task_name = payload["task_name"]
                kwargs = payload["kwargs"]
                attempt = int(payload.get("attempt", 1))
                max_attempts = int(payload.get("max_attempts", self._tasks[task_name].max_attempts))
                handler = self._tasks[task_name].handler
                self._active_jobs += 1
                queue_name = str(queue_key).split("queue:")[-1]
                if self.telemetry is not None:
                    self.telemetry.record_task_state(
                        task_name=task_name,
                        state=TASK_STATE_STARTED,
                        details={"queue": queue_name, "attempt": attempt, "max_attempts": max_attempts, **kwargs},
                    )
                handler_kwargs = dict(kwargs)
                if self._tasks[task_name].pass_retry_metadata:
                    handler_kwargs["attempt"] = attempt
                    handler_kwargs["max_attempts"] = max_attempts
                result = await handler(**handler_kwargs)
                await self._handle_result(
                    task_name=task_name,
                    queue_name=queue_name,
                    kwargs=kwargs,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    result=result,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                task_name = str(payload.get("task_name")) if isinstance(payload, dict) else "unknown"
                details = {"worker_index": worker_index}
                if isinstance(payload, dict):
                    kwargs = payload.get("kwargs")
                    if isinstance(kwargs, dict):
                        details.update({key: kwargs.get(key) for key in ("media_id", "correlation_id")})
                        details["attempt"] = payload.get("attempt", 1)
                        details["max_attempts"] = payload.get("max_attempts", 1)
                self.telemetry.record_task_state(task_name=task_name, state=TASK_STATE_FAILED_PERMANENT, details=details)
                self.telemetry.record_task_failure(task_name=task_name, details=details)
                logger.exception("redis_task_worker_failed", extra={"worker_index": worker_index})
            finally:
                if self._active_jobs > 0:
                    self._active_jobs -= 1

    async def _handle_result(
        self,
        *,
        task_name: str,
        queue_name: str,
        kwargs: dict[str, object],
        attempt: int,
        max_attempts: int,
        result: object,
    ) -> None:
        if not isinstance(result, TaskRunResult):
            self.telemetry.record_task_state(
                task_name=task_name,
                state=TASK_STATE_SUCCEEDED,
                details={"queue": queue_name, "attempt": attempt, "max_attempts": max_attempts, **kwargs},
            )
            return
        details = {"queue": queue_name, "attempt": attempt, "max_attempts": max_attempts, "reason": result.reason, **kwargs}
        if result.state == TASK_STATE_RETRY_SCHEDULED and result.retryable and attempt < max_attempts:
            self.telemetry.record_task_state(task_name=task_name, state=TASK_STATE_RETRY_SCHEDULED, details=details)
            await self.enqueue(
                task_name,
                queue=queue_name,
                _task_attempt=attempt + 1,
                _task_max_attempts=max_attempts,
                **kwargs,
            )
            return
        terminal_state = result.state if result.state != TASK_STATE_RETRY_SCHEDULED else TASK_STATE_FAILED_EXHAUSTED
        self.telemetry.record_task_state(task_name=task_name, state=terminal_state, details=details)
        if terminal_state in {TASK_STATE_FAILED_PERMANENT, TASK_STATE_FAILED_EXHAUSTED}:
            self.telemetry.record_task_failure(task_name=task_name, details=details)

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
