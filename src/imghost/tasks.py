from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .processors import ProcessorRegistry
from .repositories import PostgresRepository
from .storage import LocalFilesystemBackend

TaskHandler = Callable[..., Awaitable[None]]
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TaskContext:
    repository: PostgresRepository
    storage: LocalFilesystemBackend
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


@dataclass(slots=True)
class QueuedTask:
    task_name: str
    kwargs: dict[str, object]


class AsyncTaskQueue(TaskQueue):
    def __init__(self, context: TaskContext, worker_count: int = 1) -> None:
        self.context = context
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


class SyncTaskQueue(TaskQueue):
    def __init__(self, context: TaskContext) -> None:
        self.context = context
        self._handlers: dict[str, TaskHandler] = {}

    def register(self, task_name: str, handler: TaskHandler) -> None:
        self._handlers[task_name] = handler

    async def enqueue(self, task_name: str, queue: str = "default", **kwargs) -> None:
        handler = self._handlers[task_name]
        await handler(**kwargs)
