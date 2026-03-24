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

    state = AppState(load_settings(), run_task_worker=True, task_worker_queues=("thumbnails",))

    assert state.run_task_worker is True
    assert state.task_worker_queues == ("thumbnails",)
