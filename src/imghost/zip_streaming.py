from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from queue import Queue
from threading import Thread
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

        async def produce() -> None:
            iterator = await self.factory()
            try:
                async for chunk in iterator:
                    queue.put(chunk)
            except BaseException as exc:
                queue.put(_ErrorBox(exc))
            else:
                queue.put(_END)

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
            thread.join(timeout=1.0)
