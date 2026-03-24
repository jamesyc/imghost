from __future__ import annotations

import asyncio
from types import SimpleNamespace

from imghost.app_state import AppState
from imghost.config import load_settings
from imghost.models import Media, utcnow
from imghost.task_catalog import default_queue_for


class _RecordingTasks:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, str, str]] = []

    async def enqueue(self, task_name: str, queue: str | None = None, **kwargs) -> None:
        resolved_queue = queue if queue is not None else default_queue_for(task_name)
        self.enqueued.append((task_name, resolved_queue, str(kwargs["media_id"])))


class _RecoveryRepository:
    def __init__(self, pending: list[Media], failed: list[Media]) -> None:
        self.pending = pending
        self.failed = failed
        self.updated: list[tuple[str, str]] = []

    async def find_pending_thumbnails(self) -> list[Media]:
        return list(self.pending)

    async def find_failed_thumbnails(self) -> list[Media]:
        return list(self.failed)

    async def update_media(self, media: Media) -> Media:
        self.updated.append((media.id, media.thumb_status))
        return media


class _RecordingLifecycleTelemetry:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.last_worker_started_at = None
        self.last_worker_stopped_at = None

    async def record_system_startup(self, *, metadata: dict[str, object]) -> None:
        self.order.append("telemetry.system_startup")

    async def record_worker_started_event(self, *, metadata: dict[str, object]) -> None:
        self.order.append("telemetry.worker_started")

    async def record_worker_stopped_event(self, *, metadata: dict[str, object]) -> None:
        self.order.append("telemetry.worker_stopped")

    async def record_system_shutdown(self, *, metadata: dict[str, object]) -> None:
        self.order.append("telemetry.system_shutdown")


def _make_media(media_id: str, *, thumb_status: str) -> Media:
    return Media(
        id=media_id,
        album_id="album-1",
        user_id=None,
        filename_orig=f"{media_id}.png",
        media_type="image",
        format="png",
        mime_type="image/png",
        storage_key=f"originals/anon/{media_id}.png",
        thumb_key=None,
        thumb_is_orig=False,
        thumb_status=thumb_status,
        file_size=123,
        thumb_size=None,
        width=1,
        height=1,
        duration_secs=None,
        is_animated=False,
        codec_hint=None,
        position=0,
        created_at=utcnow(),
    )


def test_recover_thumbnails_deduplicates_ids_and_resets_failed_items_to_pending() -> None:
    duplicate_pending = _make_media("media-1", thumb_status="processing")
    duplicate_failed = _make_media("media-1", thumb_status="failed")
    distinct_failed = _make_media("media-2", thumb_status="failed")
    repository = _RecoveryRepository([duplicate_pending], [duplicate_failed, distinct_failed])
    tasks = _RecordingTasks()
    state = SimpleNamespace(repository=repository, tasks=tasks)

    enqueued = asyncio.run(AppState.recover_thumbnails(state, include_failed=True))

    assert enqueued == 2
    assert tasks.enqueued == [
        ("generate_thumbnail", "thumbnails", "media-1"),
        ("generate_thumbnail", "thumbnails", "media-2"),
    ]
    assert repository.updated == [
        ("media-2", "pending"),
    ]


def test_app_state_allows_cli_worker_queue_override(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("TASK_WORKER_QUEUES", "default,cleanup")

    state = AppState(load_settings(), process_role="worker", task_worker_queues=("thumbnails",))

    assert state.run_task_worker is True
    assert state.task_worker_queues == ("thumbnails",)


def test_app_state_app_role_does_not_run_worker_or_recovery(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("TASK_WORKER_QUEUES", "thumbnails,cleanup")

    state = AppState(load_settings(), process_role="app")

    assert state.process_role == "app"
    assert state.run_task_worker is False
    assert state.task_worker_queues == ()
    assert state._should_run_thumbnail_startup_recovery() is False
    assert state._should_emit_worker_lifecycle_events() is False


def test_app_state_thumbnail_worker_role_runs_recovery_and_worker_lifecycle(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    state = AppState(load_settings(), process_role="worker", task_worker_queues=("thumbnails",))

    assert state.process_role == "worker"
    assert state.run_task_worker is True
    assert state.task_worker_queues == ("thumbnails",)
    assert state._should_run_thumbnail_startup_recovery() is True
    assert state._should_emit_worker_lifecycle_events() is True


def test_app_state_cleanup_worker_role_skips_thumbnail_recovery(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    state = AppState(load_settings(), process_role="worker", task_worker_queues=("cleanup",))

    assert state.process_role == "worker"
    assert state.run_task_worker is True
    assert state.task_worker_queues == ("cleanup",)
    assert state._should_run_thumbnail_startup_recovery() is False
    assert state._should_emit_worker_lifecycle_events() is True


def test_app_state_scheduler_role_skips_worker_lifecycle_and_recovery(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    state = AppState(load_settings(), process_role="scheduler")

    assert state.process_role == "scheduler"
    assert state.run_task_worker is False
    assert state.task_worker_queues == ()
    assert state._should_run_thumbnail_startup_recovery() is False
    assert state._should_emit_worker_lifecycle_events() is False
    assert state._should_run_scheduler_loop() is True


def test_app_state_runtime_status_reports_service_shape_for_worker_role(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")

    async def scenario() -> dict[str, object]:
        state = AppState(load_settings(), process_role="worker", task_worker_queues=("thumbnails", "cleanup"))
        await state.database.connect()
        try:
            return await state.runtime_status()
        finally:
            await state.database.close()

    payload = asyncio.run(scenario())

    assert payload["process_role"] == "worker"
    assert payload["services"]["app"]["enabled_in_this_process"] is False
    assert payload["services"]["worker"]["enabled_in_this_process"] is True
    assert payload["services"]["worker"]["queues"] == ["thumbnails", "cleanup"]
    assert payload["services"]["scheduler"]["enabled_in_this_process"] is False


def test_app_state_runtime_status_reports_scheduler_service_shape(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("SCHEDULER_POLL_SECONDS", "15")
    monkeypatch.setenv("SCHEDULER_LEASE_SECONDS", "120")
    monkeypatch.setenv("CLEANUP_INTERVAL_SECONDS", "600")

    async def scenario() -> dict[str, object]:
        state = AppState(load_settings(), process_role="scheduler")
        await state.database.connect()
        try:
            return await state.runtime_status()
        finally:
            await state.database.close()

    payload = asyncio.run(scenario())

    assert payload["process_role"] == "scheduler"
    assert payload["services"]["scheduler"]["enabled_in_this_process"] is True
    assert payload["services"]["scheduler"]["configured"] is True
    assert payload["services"]["scheduler"]["poll_seconds"] == 15
    assert payload["services"]["scheduler"]["lease_enabled"] is False
    assert payload["services"]["scheduler"]["lease_seconds"] == 600
    assert payload["services"]["scheduler"]["jobs"]["prune_expired_albums"]["interval_seconds"] == 600
    assert payload["services"]["scheduler"]["jobs"]["prune_expired_albums"]["queue"] == "cleanup"


def test_app_state_start_emits_lifecycle_telemetry_after_shared_and_role_startup() -> None:
    order: list[str] = []
    state = SimpleNamespace(
        telemetry=_RecordingLifecycleTelemetry(order),
        process_role="worker",
        settings=SimpleNamespace(
            base_url="http://testserver",
            public_origin_enabled=False,
            trusted_proxy_cidrs_enabled=False,
            storage_backend="filesystem",
            redis_mode="auto",
            task_queue_mode="async",
            task_worker_enabled=True,
            thumbnail_worker_count=1,
            session_redis_fail_closed=False,
        ),
        redis=SimpleNamespace(enabled=False),
        run_task_worker=True,
        task_worker_queues=("thumbnails",),
    )

    async def _start_shared() -> None:
        order.append("start_shared")

    async def _start_role() -> int:
        order.append("start_role")
        return 2

    state._start_shared = _start_shared
    state._start_role = _start_role
    state._should_emit_worker_lifecycle_events = lambda: True

    asyncio.run(AppState.start(state))

    assert order == [
        "start_shared",
        "start_role",
        "telemetry.system_startup",
        "telemetry.worker_started",
    ]


def test_app_state_stop_emits_lifecycle_telemetry_before_role_and_shared_shutdown() -> None:
    order: list[str] = []
    state = SimpleNamespace(
        telemetry=_RecordingLifecycleTelemetry(order),
        process_role="worker",
        settings=SimpleNamespace(
            task_queue_mode="async",
            thumbnail_worker_count=1,
        ),
        run_task_worker=True,
        task_worker_queues=("thumbnails",),
    )

    async def _stop_role() -> None:
        order.append("stop_role")

    async def _stop_shared() -> None:
        order.append("stop_shared")

    state._stop_role = _stop_role
    state._stop_shared = _stop_shared
    state._should_emit_worker_lifecycle_events = lambda: True

    asyncio.run(AppState.stop(state))

    assert order == [
        "telemetry.worker_stopped",
        "telemetry.system_shutdown",
        "stop_role",
        "stop_shared",
    ]
