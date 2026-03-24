from __future__ import annotations

import asyncio

from imghost.scheduler import SchedulerService


class _RecordingTasks:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def enqueue(self, task_name: str, queue: str | None = None, **kwargs) -> None:
        if self.fail:
            raise RuntimeError("enqueue failed")
        self.calls.append((task_name, kwargs))


def test_scheduler_tick_enqueues_cleanup_task_when_due() -> None:
    tasks = _RecordingTasks()
    scheduler = SchedulerService(tasks, poll_seconds=30, cleanup_interval_seconds=900)

    enqueued = asyncio.run(scheduler.tick(now_monotonic=100.0))

    assert enqueued is True
    assert tasks.calls == [("prune_expired_albums", {"dry_run": False})]
    assert scheduler.last_tick_at is not None
    assert scheduler.last_enqueue_at is not None
    assert scheduler.last_enqueue_error is None


def test_scheduler_tick_skips_enqueue_until_interval_elapses() -> None:
    tasks = _RecordingTasks()
    scheduler = SchedulerService(tasks, poll_seconds=30, cleanup_interval_seconds=900)

    assert asyncio.run(scheduler.tick(now_monotonic=100.0)) is True
    assert asyncio.run(scheduler.tick(now_monotonic=500.0)) is False
    assert asyncio.run(scheduler.tick(now_monotonic=1000.0)) is True

    assert tasks.calls == [
        ("prune_expired_albums", {"dry_run": False}),
        ("prune_expired_albums", {"dry_run": False}),
    ]


def test_scheduler_tick_records_enqueue_error() -> None:
    scheduler = SchedulerService(_RecordingTasks(fail=True), poll_seconds=30, cleanup_interval_seconds=900)

    enqueued = asyncio.run(scheduler.tick(now_monotonic=100.0))

    assert enqueued is False
    assert scheduler.last_tick_at is not None
    assert scheduler.last_enqueue_at is None
    assert scheduler.last_enqueue_error == "enqueue failed"
