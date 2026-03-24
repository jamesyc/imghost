from __future__ import annotations

from types import SimpleNamespace

from imghost.task_catalog import (
    GENERATE_THUMBNAIL_TASK,
    PRUNE_EXPIRED_ALBUMS_TASK,
    RECOVER_THUMBNAILS_TASK,
    default_queue_for,
    register_core_tasks,
)


class _RecordingTaskQueue:
    def __init__(self) -> None:
        self.registered: list[tuple[str, str]] = []

    def register(self, task_name: str, handler, *, default_queue: str = "default") -> None:
        self.registered.append((task_name, default_queue))


def test_task_catalog_registers_core_tasks_with_expected_queues() -> None:
    queue = _RecordingTaskQueue()
    uploads = SimpleNamespace(
        generate_thumbnail=lambda **kwargs: None,
        prune_expired_albums=lambda **kwargs: None,
    )

    async def recover_thumbnails(**kwargs) -> None:
        return None

    register_core_tasks(
        queue,  # type: ignore[arg-type]
        uploads=uploads,  # type: ignore[arg-type]
        recover_thumbnails=recover_thumbnails,
    )

    assert queue.registered == [
        (GENERATE_THUMBNAIL_TASK.name, GENERATE_THUMBNAIL_TASK.default_queue),
        (RECOVER_THUMBNAILS_TASK.name, RECOVER_THUMBNAILS_TASK.default_queue),
        (PRUNE_EXPIRED_ALBUMS_TASK.name, PRUNE_EXPIRED_ALBUMS_TASK.default_queue),
    ]


def test_task_catalog_exposes_default_queue_lookup() -> None:
    assert default_queue_for(GENERATE_THUMBNAIL_TASK.name) == "thumbnails"
    assert default_queue_for(RECOVER_THUMBNAILS_TASK.name) == "thumbnails"
    assert default_queue_for(PRUNE_EXPIRED_ALBUMS_TASK.name) == "cleanup"
