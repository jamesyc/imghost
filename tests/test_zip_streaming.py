from threading import Event

import pytest

from imghost.zip_streaming import AsyncIterableBridge


def test_bridge_closes_producer_when_consumer_abandons_full_queue() -> None:
    filled = Event()
    closed = Event()

    async def chunks():
        try:
            for index in range(100):
                if index == 2:
                    filled.set()
                yield str(index).encode()
        finally:
            closed.set()

    async def factory():
        return chunks()

    consumer = iter(AsyncIterableBridge(factory, max_queue_size=1))
    assert next(consumer) == b"0"
    # Let the producer fill the queue and block on its next write.
    assert filled.wait(1)
    consumer.close()
    assert closed.wait(1)


def test_bridge_cancels_producer_waiting_for_more_data() -> None:
    import asyncio

    waiting = Event()
    closed = Event()

    async def chunks():
        try:
            yield b"first"
            waiting.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    async def factory():
        return chunks()

    consumer = iter(AsyncIterableBridge(factory))
    assert next(consumer) == b"first"
    assert waiting.wait(1)
    consumer.close()
    assert closed.wait(1)


@pytest.mark.parametrize("backpressured", [False, True])
def test_bridge_finishes_awaited_cleanup_after_consumer_cancellation(backpressured) -> None:
    import asyncio

    waiting = Event()
    cleanup_started = Event()
    closed = Event()

    async def chunks():
        try:
            yield b"first"
            if backpressured:
                yield b"queued"
                waiting.set()
                yield b"blocked"
            else:
                waiting.set()
                await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await asyncio.sleep(0.05)
            closed.set()

    async def factory():
        return chunks()

    consumer = iter(AsyncIterableBridge(factory, max_queue_size=1))
    assert next(consumer) == b"first"
    assert waiting.wait(1)
    consumer.close()
    assert cleanup_started.wait(1)
    assert closed.wait(1)


def test_bridge_propagates_factory_failure_without_hanging() -> None:
    async def factory():
        raise ValueError("cannot open stream")

    with pytest.raises(ValueError, match="cannot open stream"):
        list(AsyncIterableBridge(factory))


def test_bridge_delivers_chunks_and_closes_exhausted_iterator() -> None:
    closed = Event()

    async def chunks():
        try:
            yield b"first"
            yield b"second"
        finally:
            closed.set()

    async def factory():
        return chunks()

    assert list(AsyncIterableBridge(factory, max_queue_size=1)) == [b"first", b"second"]
    assert closed.is_set()
