from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from queue import Full, Queue
from threading import Event, Thread
from typing import TypeVar

T = TypeVar("T")
_END = object()


class _ErrorBox:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


class AsyncIterableBridge:
    def __init__(
        self,
        factory: Callable[[], Awaitable[AsyncIterator[bytes]]],
        *,
        max_queue_size: int = 8,
    ) -> None:
        self.factory = factory
        self.max_queue_size = max_queue_size

    def __iter__(self) -> Iterator[bytes]:
        queue: Queue[object] = Queue(maxsize=self.max_queue_size)
        stopped = Event()
        loop: asyncio.AbstractEventLoop | None = None
        task: asyncio.Task | None = None

        def enqueue(item: object) -> bool:
            while not stopped.is_set():
                try:
                    queue.put(item, timeout=0.05)
                    return True
                except Full:
                    continue
            return False

        async def produce() -> None:
            nonlocal loop, task
            loop = asyncio.get_running_loop()
            task = asyncio.current_task()
            try:
                iterator = await self.factory()
                try:
                    async for chunk in iterator:
                        if not enqueue(chunk):
                            return
                finally:
                    close = getattr(iterator, "aclose", None)
                    if close is not None:
                        # Cancellation may arrive after backpressure ends and cleanup
                        # has already started. Finish closing before the loop exits.
                        closing = asyncio.ensure_future(close())
                        try:
                            await asyncio.shield(closing)
                        except asyncio.CancelledError:
                            await closing
                            raise
            except BaseException as exc:
                enqueue(_ErrorBox(exc))
            else:
                enqueue(_END)

        thread = Thread(target=lambda: asyncio.run(produce()), daemon=True)
        thread.start()
        try:
            while True:
                item = queue.get()
                if item is _END:
                    return
                if isinstance(item, _ErrorBox):
                    raise item.exc
                yield item  # type: ignore[misc]
        finally:
            stopped.set()
            if loop is not None and task is not None:
                try:
                    loop.call_soon_threadsafe(task.cancel)
                except RuntimeError:
                    pass  # The producer already finished and closed its event loop.
            thread.join(timeout=1.0)
