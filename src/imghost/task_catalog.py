from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .service import UploadService
from .tasks import TaskQueue

TaskCallback = Callable[..., Awaitable[object]]


@dataclass(frozen=True, slots=True)
class TaskSpec:
    name: str
    default_queue: str


GENERATE_THUMBNAIL_TASK = TaskSpec(name="generate_thumbnail", default_queue="thumbnails")
RECOVER_THUMBNAILS_TASK = TaskSpec(name="recover_thumbnails", default_queue="thumbnails")
PRUNE_EXPIRED_ALBUMS_TASK = TaskSpec(name="prune_expired_albums", default_queue="cleanup")

ALL_TASKS = (
    GENERATE_THUMBNAIL_TASK,
    RECOVER_THUMBNAILS_TASK,
    PRUNE_EXPIRED_ALBUMS_TASK,
)


def default_queue_for(task_name: str) -> str:
    for task in ALL_TASKS:
        if task.name == task_name:
            return task.default_queue
    raise KeyError(task_name)


def register_core_tasks(
    task_queue: TaskQueue,
    *,
    uploads: UploadService,
    recover_thumbnails: TaskCallback,
) -> None:
    task_queue.register(
        GENERATE_THUMBNAIL_TASK.name,
        uploads.generate_thumbnail,
        default_queue=GENERATE_THUMBNAIL_TASK.default_queue,
    )
    task_queue.register(
        RECOVER_THUMBNAILS_TASK.name,
        recover_thumbnails,
        default_queue=RECOVER_THUMBNAILS_TASK.default_queue,
    )
    task_queue.register(
        PRUNE_EXPIRED_ALBUMS_TASK.name,
        uploads.prune_expired_albums,
        default_queue=PRUNE_EXPIRED_ALBUMS_TASK.default_queue,
    )
