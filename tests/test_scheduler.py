from __future__ import annotations

import asyncio

from imghost.scheduler import SchedulerService


class _FakeRedisClient:
    def __init__(self) -> None:
        self.entries: dict[str, tuple[str, float]] = {}
        self.now = 0.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def _purge_expired(self) -> None:
        expired = [key for key, (_, expires_at) in self.entries.items() if self.now >= expires_at]
        for key in expired:
            self.entries.pop(key, None)

    async def set(self, key: str, value: str, *, ex: int, nx: bool = False) -> bool:
        self._purge_expired()
        if nx and key in self.entries:
            return False
        self.entries[key] = (value, self.now + ex)
        return True

    async def eval(self, script: str, numkeys: int, key: str, token: str) -> int:
        self._purge_expired()
        entry = self.entries.get(key)
        if entry is None:
            return 0
        if entry[0] != token:
            return 0
        self.entries.pop(key, None)
        return 1


class _FakeRedisHandle:
    def __init__(
        self,
        client: _FakeRedisClient,
        *,
        enabled: bool = True,
        unavailable: bool = False,
        fail_release: bool = False,
    ) -> None:
        self._client = client
        self.enabled = enabled
        self.unavailable = unavailable
        self.fail_release = fail_release

    def prefixed(self, key: str) -> str:
        return f"imghost:{key}"

    async def execute(self, operation: str, callback):
        if self.unavailable:
            from imghost.redis_support import RedisUnavailable

            raise RedisUnavailable("Redis unavailable during lease")
        if self.fail_release and operation == "release scheduler cleanup lease":
            from imghost.redis_support import RedisUnavailable

            raise RedisUnavailable("Redis unavailable during lease release")
        return await callback(self._client)


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


def test_scheduler_tick_enqueues_without_redis_lease_in_no_redis_mode() -> None:
    tasks = _RecordingTasks()
    scheduler = SchedulerService(tasks, poll_seconds=30, cleanup_interval_seconds=900, redis=None, lease_seconds=120)

    enqueued = asyncio.run(scheduler.tick(now_monotonic=100.0))

    assert enqueued is True
    assert tasks.calls == [("prune_expired_albums", {"dry_run": False})]
    assert scheduler.runtime_status()["lease_enabled"] is False
    assert scheduler.runtime_status()["lease_seconds"] == 900


def test_multiple_schedulers_without_redis_can_both_enqueue() -> None:
    first_tasks = _RecordingTasks()
    second_tasks = _RecordingTasks()
    first = SchedulerService(first_tasks, poll_seconds=30, cleanup_interval_seconds=900, redis=None)
    second = SchedulerService(second_tasks, poll_seconds=30, cleanup_interval_seconds=900, redis=None)

    assert asyncio.run(first.tick(now_monotonic=100.0)) is True
    assert asyncio.run(second.tick(now_monotonic=100.0)) is True

    assert first_tasks.calls == [("prune_expired_albums", {"dry_run": False})]
    assert second_tasks.calls == [("prune_expired_albums", {"dry_run": False})]


def test_scheduler_with_disabled_redis_handle_falls_back_to_no_lease_mode() -> None:
    redis_client = _FakeRedisClient()
    redis_handle = _FakeRedisHandle(redis_client, enabled=False)
    tasks = _RecordingTasks()
    scheduler = SchedulerService(tasks, poll_seconds=30, cleanup_interval_seconds=900, redis=redis_handle)

    enqueued = asyncio.run(scheduler.tick(now_monotonic=100.0))

    assert enqueued is True
    assert tasks.calls == [("prune_expired_albums", {"dry_run": False})]
    assert scheduler.runtime_status()["lease_enabled"] is False


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


def test_scheduler_tick_skips_enqueue_when_redis_lease_is_held_elsewhere() -> None:
    redis_client = _FakeRedisClient()
    redis_handle = _FakeRedisHandle(redis_client)
    first_tasks = _RecordingTasks()
    second_tasks = _RecordingTasks()
    first = SchedulerService(
        first_tasks,
        poll_seconds=30,
        cleanup_interval_seconds=900,
        redis=redis_handle,
        lease_seconds=120,
    )
    second = SchedulerService(
        second_tasks,
        poll_seconds=30,
        cleanup_interval_seconds=900,
        redis=redis_handle,
        lease_seconds=120,
    )

    assert asyncio.run(first.tick(now_monotonic=100.0)) is True
    assert asyncio.run(second.tick(now_monotonic=100.0)) is False

    assert first_tasks.calls == [("prune_expired_albums", {"dry_run": False})]
    assert second_tasks.calls == []
    assert second.last_lease_error is None


def test_scheduler_retries_after_poll_interval_when_lease_is_held_elsewhere() -> None:
    redis_client = _FakeRedisClient()
    redis_handle = _FakeRedisHandle(redis_client)
    first_tasks = _RecordingTasks()
    second_tasks = _RecordingTasks()
    first = SchedulerService(
        first_tasks,
        poll_seconds=30,
        cleanup_interval_seconds=900,
        redis=redis_handle,
        lease_seconds=120,
    )
    second = SchedulerService(
        second_tasks,
        poll_seconds=30,
        cleanup_interval_seconds=900,
        redis=redis_handle,
        lease_seconds=120,
    )

    assert asyncio.run(first.tick(now_monotonic=100.0)) is True
    assert asyncio.run(second.tick(now_monotonic=100.0)) is False
    assert asyncio.run(second.tick(now_monotonic=120.0)) is False
    assert asyncio.run(second.tick(now_monotonic=130.0)) is False

    assert second_tasks.calls == []


def test_scheduler_tick_can_take_over_after_lease_expires() -> None:
    redis_client = _FakeRedisClient()
    redis_handle = _FakeRedisHandle(redis_client)
    first_tasks = _RecordingTasks()
    second_tasks = _RecordingTasks()
    first = SchedulerService(
        first_tasks,
        poll_seconds=30,
        cleanup_interval_seconds=120,
        redis=redis_handle,
        lease_seconds=60,
    )
    second = SchedulerService(
        second_tasks,
        poll_seconds=30,
        cleanup_interval_seconds=120,
        redis=redis_handle,
        lease_seconds=60,
    )

    assert asyncio.run(first.tick(now_monotonic=100.0)) is True
    redis_client.advance(121)
    assert asyncio.run(second.tick(now_monotonic=221.0)) is True

    assert first_tasks.calls == [("prune_expired_albums", {"dry_run": False})]
    assert second_tasks.calls == [("prune_expired_albums", {"dry_run": False})]


def test_scheduler_effective_lease_seconds_uses_cleanup_interval_when_larger() -> None:
    scheduler = SchedulerService(_RecordingTasks(), poll_seconds=30, cleanup_interval_seconds=900, lease_seconds=60)

    assert scheduler.runtime_status()["lease_seconds"] == 900


def test_scheduler_effective_lease_seconds_uses_explicit_lease_when_larger() -> None:
    scheduler = SchedulerService(_RecordingTasks(), poll_seconds=30, cleanup_interval_seconds=120, lease_seconds=300)

    assert scheduler.runtime_status()["lease_seconds"] == 300


def test_scheduler_tick_records_enqueue_error() -> None:
    scheduler = SchedulerService(_RecordingTasks(fail=True), poll_seconds=30, cleanup_interval_seconds=900)

    enqueued = asyncio.run(scheduler.tick(now_monotonic=100.0))

    assert enqueued is False
    assert scheduler.last_tick_at is not None
    assert scheduler.last_enqueue_at is None
    assert scheduler.last_enqueue_error == "enqueue failed"


def test_scheduler_releases_redis_lease_after_enqueue_error() -> None:
    redis_client = _FakeRedisClient()
    redis_handle = _FakeRedisHandle(redis_client)
    failing = SchedulerService(
        _RecordingTasks(fail=True),
        poll_seconds=30,
        cleanup_interval_seconds=900,
        redis=redis_handle,
        lease_seconds=120,
    )
    succeeding_tasks = _RecordingTasks()
    succeeding = SchedulerService(
        succeeding_tasks,
        poll_seconds=30,
        cleanup_interval_seconds=900,
        redis=redis_handle,
        lease_seconds=120,
    )

    assert asyncio.run(failing.tick(now_monotonic=100.0)) is False
    assert asyncio.run(succeeding.tick(now_monotonic=100.0)) is True

    assert succeeding_tasks.calls == [("prune_expired_albums", {"dry_run": False})]


def test_scheduler_records_release_error_when_enqueue_fails_and_lease_release_fails() -> None:
    redis_client = _FakeRedisClient()
    redis_handle = _FakeRedisHandle(redis_client, fail_release=True)
    scheduler = SchedulerService(
        _RecordingTasks(fail=True),
        poll_seconds=30,
        cleanup_interval_seconds=900,
        redis=redis_handle,
        lease_seconds=120,
    )

    enqueued = asyncio.run(scheduler.tick(now_monotonic=100.0))

    assert enqueued is False
    assert scheduler.last_enqueue_error == "enqueue failed"
    assert scheduler.last_lease_error == "Redis unavailable during lease release"


def test_scheduler_tick_records_lease_error_and_skips_enqueue() -> None:
    redis_client = _FakeRedisClient()
    redis_handle = _FakeRedisHandle(redis_client, unavailable=True)
    scheduler = SchedulerService(
        _RecordingTasks(),
        poll_seconds=30,
        cleanup_interval_seconds=900,
        redis=redis_handle,
        lease_seconds=120,
    )

    enqueued = asyncio.run(scheduler.tick(now_monotonic=100.0))

    assert enqueued is False
    assert scheduler.last_enqueue_at is None
    assert scheduler.last_lease_error == "Redis unavailable during lease"


def test_scheduler_clears_previous_lease_error_after_successful_no_redis_tick() -> None:
    scheduler = SchedulerService(_RecordingTasks(), poll_seconds=30, cleanup_interval_seconds=900)
    scheduler.last_lease_error = "old lease error"

    enqueued = asyncio.run(scheduler.tick(now_monotonic=100.0))

    assert enqueued is True
    assert scheduler.last_lease_error is None
