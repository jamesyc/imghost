from __future__ import annotations

import asyncio

from imghost.tasks import (
    TASK_STATES,
    TASK_STATE_FAILED_EXHAUSTED,
    TASK_STATE_FAILED_PERMANENT,
    TASK_STATE_QUEUED,
    TASK_STATE_RETRY_SCHEDULED,
    TASK_STATE_SKIPPED,
    TASK_STATE_STARTED,
    TASK_STATE_SUCCEEDED,
    AsyncTaskQueue,
    SyncTaskQueue,
    TaskContext,
    TaskRunResult,
)
from imghost.telemetry.state import TelemetryState


class RecordingTaskTelemetry:
    def __init__(self) -> None:
        self.state = TelemetryState()
        self.enqueued: list[tuple[str, str]] = []

    def record_task_enqueued(self, *, queue: str, task_name: str) -> None:
        self.enqueued.append((queue, task_name))

    def record_task_state(self, *, task_name: str, state: str, details: dict[str, object]) -> None:
        self.state.record_task_state(task_name=task_name, state=state, details=details)

    def record_task_failure(self, *, task_name: str, details: dict[str, object]) -> None:
        self.state.record_task_failure(task_name=task_name, details=details)


def _states(telemetry: RecordingTaskTelemetry) -> list[str]:
    return [event["state"] for event in telemetry.state.recent_task_events]


def test_task_state_constants_cover_all_retry_lifecycle_states() -> None:
    assert TASK_STATES == (
        TASK_STATE_QUEUED,
        TASK_STATE_STARTED,
        TASK_STATE_SKIPPED,
        TASK_STATE_RETRY_SCHEDULED,
        TASK_STATE_SUCCEEDED,
        TASK_STATE_FAILED_PERMANENT,
        TASK_STATE_FAILED_EXHAUSTED,
    )


def test_sync_task_queue_records_success_lifecycle_states() -> None:
    telemetry = RecordingTaskTelemetry()
    queue = SyncTaskQueue(TaskContext(repository=None, storage=None, processors=None), telemetry=telemetry)

    async def handler() -> None:
        return None

    queue.register("demo", handler)
    asyncio.run(queue.enqueue("demo"))

    assert _states(telemetry) == [TASK_STATE_QUEUED, TASK_STATE_STARTED, TASK_STATE_SUCCEEDED]


def test_sync_task_queue_records_skipped_state() -> None:
    telemetry = RecordingTaskTelemetry()
    queue = SyncTaskQueue(TaskContext(repository=None, storage=None, processors=None), telemetry=telemetry)

    async def handler() -> TaskRunResult:
        return TaskRunResult(state=TASK_STATE_SKIPPED, reason="already_done")

    queue.register("demo", handler)
    asyncio.run(queue.enqueue("demo"))

    assert _states(telemetry) == [TASK_STATE_QUEUED, TASK_STATE_STARTED, TASK_STATE_SKIPPED]


def test_sync_task_queue_records_retry_then_success_states() -> None:
    telemetry = RecordingTaskTelemetry()
    queue = SyncTaskQueue(TaskContext(repository=None, storage=None, processors=None), telemetry=telemetry)
    attempts = {"count": 0}

    async def handler(*, attempt: int, max_attempts: int) -> TaskRunResult | None:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return TaskRunResult(state=TASK_STATE_RETRY_SCHEDULED, reason="storage_read_failed", retryable=True)
        return None

    queue.register("demo", handler, max_attempts=3, pass_retry_metadata=True)
    asyncio.run(queue.enqueue("demo"))

    assert _states(telemetry) == [
        TASK_STATE_QUEUED,
        TASK_STATE_STARTED,
        TASK_STATE_RETRY_SCHEDULED,
        TASK_STATE_QUEUED,
        TASK_STATE_STARTED,
        TASK_STATE_SUCCEEDED,
    ]


def test_sync_task_queue_records_failed_exhausted_state() -> None:
    telemetry = RecordingTaskTelemetry()
    queue = SyncTaskQueue(TaskContext(repository=None, storage=None, processors=None), telemetry=telemetry)

    async def handler(*, attempt: int, max_attempts: int) -> TaskRunResult:
        return TaskRunResult(state=TASK_STATE_RETRY_SCHEDULED, reason="thumbnail_generate_failed", retryable=True)

    queue.register("demo", handler, max_attempts=2, pass_retry_metadata=True)
    asyncio.run(queue.enqueue("demo"))

    assert _states(telemetry) == [
        TASK_STATE_QUEUED,
        TASK_STATE_STARTED,
        TASK_STATE_RETRY_SCHEDULED,
        TASK_STATE_QUEUED,
        TASK_STATE_STARTED,
        TASK_STATE_FAILED_EXHAUSTED,
    ]
    assert telemetry.state.last_task_failure is not None
    assert telemetry.state.last_task_failure["reason"] == "thumbnail_generate_failed"


def test_async_task_queue_records_failed_permanent_state() -> None:
    telemetry = RecordingTaskTelemetry()
    queue = AsyncTaskQueue(TaskContext(repository=None, storage=None, processors=None), telemetry=telemetry)

    async def handler() -> None:
        raise RuntimeError("boom")

    queue.register("demo", handler)

    async def run() -> None:
        await queue.start()
        try:
            await queue.enqueue("demo")
            await queue.join()
        finally:
            await queue.stop()

    asyncio.run(run())

    assert _states(telemetry) == [TASK_STATE_QUEUED, TASK_STATE_STARTED, TASK_STATE_FAILED_PERMANENT]
    assert telemetry.state.last_task_failure is not None
    assert telemetry.state.last_task_failure["reason"] == "boom"
