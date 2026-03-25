from __future__ import annotations

import logging
from typing import Any, Literal

from .telemetry import Telemetry, build_telemetry
from .config import Settings
from .db import Database
from .events import EventBus, MediaUploaded
from .models import utcnow
from .oauth import GoogleOAuthProvider, OAuthProvider, OAuthStateManager
from .processors import build_processor_registry
from .rate_limits import build_rate_limiter
from .redis_support import RedisHandle
from .repositories import PostgresRepository
from .runtime_config import PostgresRuntimeConfig
from .scheduler import SchedulerService
from .service import UploadService
from .sessions import SessionBackend, build_session_backend
from .storage import build_storage_backend
from .task_catalog import GENERATE_THUMBNAIL_TASK, register_core_tasks
from .tasks import AsyncTaskQueue, RedisTaskQueue, SyncTaskQueue, TaskContext, TaskQueue

ProcessRole = Literal["app", "worker", "scheduler"]


class AppState:
    def __init__(
        self,
        settings: Settings,
        *,
        process_role: ProcessRole | None = None,
        run_task_worker: bool | None = None,
        task_worker_queues: tuple[str, ...] | None = None,
    ) -> None:
        self.settings = settings
        if process_role is None:
            process_role = "worker" if run_task_worker else "app"
        self.process_role: ProcessRole = process_role
        self.run_task_worker = self.process_role == "worker"
        selected_worker_queues = settings.task_worker_queues if task_worker_queues is None else task_worker_queues
        self.task_worker_queues = selected_worker_queues if self.run_task_worker else ()
        self.database = Database(settings.database_url, use_pgbouncer=settings.database_use_pgbouncer)
        self.event_bus = EventBus()
        self.repository = PostgresRepository(self.database)
        self.telemetry: Telemetry = build_telemetry(self.database, self.event_bus)
        self.runtime_config = PostgresRuntimeConfig(self.database)
        self.redis = RedisHandle(settings)
        self.session_backend: SessionBackend = build_session_backend(settings, self.redis, self.telemetry)
        self.rate_limiter = build_rate_limiter(self.runtime_config, self.redis, self.telemetry)
        self.storage = build_storage_backend(settings)
        self.oauth_state = OAuthStateManager(settings.secret_key)
        self.oauth_providers: dict[str, OAuthProvider] = {}
        if settings.google_oauth_enabled and settings.google_client_id and settings.google_client_secret:
            self.oauth_providers["google"] = GoogleOAuthProvider(
                client_id=settings.google_client_id,
                client_secret=settings.google_client_secret,
            )
        self.processors = build_processor_registry(
            settings.max_pixel_megapixels * 1_000_000,
            settings.video_thumb_frames,
        )
        self.tasks = self._build_task_queue()
        self.uploads = UploadService(
            settings,
            self.repository,
            self.storage,
            self.event_bus,
            self.processors,
            self.runtime_config,
            self.rate_limiter,
            self.telemetry,
        )
        register_core_tasks(
            self.tasks,
            uploads=self.uploads,
            recover_thumbnails=self.recover_thumbnails,
        )
        self.scheduler = SchedulerService(
            self.tasks,
            poll_seconds=self.settings.scheduler_poll_seconds,
            cleanup_interval_seconds=self.settings.cleanup_interval_seconds,
            redis=self.redis,
            lease_seconds=self.settings.scheduler_lease_seconds,
        )
        self.event_bus.subscribe(MediaUploaded, self._enqueue_thumbnail)
        self.bootstrap_admin_status: dict[str, Any] = {
            "enabled": bool(settings.promote_username_to_admin),
            "configured_username": settings.promote_username_to_admin,
            "matched": False,
            "already_admin": False,
            "promoted": False,
            "user_id": None,
            "warning": None,
        }

    def _build_task_queue(self) -> TaskQueue:
        context = TaskContext(self.repository, self.storage, self.processors)
        if self.settings.task_queue_mode == "sync":
            return SyncTaskQueue(context, self.telemetry)
        if self.settings.task_queue_mode == "redis" and self.redis.enabled:
            return RedisTaskQueue(
                self.redis,
                context,
                self.telemetry,
                worker_count=self.settings.thumbnail_worker_count,
                run_worker=self.run_task_worker,
                worker_queues=self.task_worker_queues,
            )
        return AsyncTaskQueue(context, worker_count=self.settings.thumbnail_worker_count, telemetry=self.telemetry)

    async def start(self) -> None:
        await self._start_shared()
        recovered = await self._start_role()
        await self.telemetry.record_system_startup(
            metadata={
                "process_role": self.process_role,
                "base_url": self.settings.base_url,
                "public_origin_enabled": self.settings.public_origin_enabled,
                "trusted_proxy_cidrs_enabled": self.settings.trusted_proxy_cidrs_enabled,
                "storage_backend": self.settings.storage_backend,
                "redis_enabled": self.redis.enabled,
                "redis_mode": self.settings.redis_mode,
                "task_queue_mode": self.settings.task_queue_mode,
                "task_worker_enabled": self.settings.task_worker_enabled,
                "run_task_worker": self.run_task_worker,
                "task_worker_queues": list(self.task_worker_queues),
                "thumbnail_worker_count": self.settings.thumbnail_worker_count,
                "session_redis_fail_closed": self.settings.session_redis_fail_closed,
                "recovered_thumbnail_count": recovered,
            },
        )
        if self._should_emit_worker_lifecycle_events():
            await self.telemetry.record_worker_started_event(
                metadata={
                    "process_role": self.process_role,
                    "task_queue_mode": self.settings.task_queue_mode,
                    "task_worker_queues": list(self.task_worker_queues),
                    "thumbnail_worker_count": self.settings.thumbnail_worker_count,
                    "recovered_thumbnail_count": recovered,
                },
            )

    async def stop(self) -> None:
        if self._should_emit_worker_lifecycle_events():
            await self.telemetry.record_worker_stopped_event(
                metadata={
                    "process_role": self.process_role,
                    "task_queue_mode": self.settings.task_queue_mode,
                    "task_worker_queues": list(self.task_worker_queues),
                    "thumbnail_worker_count": self.settings.thumbnail_worker_count,
                },
            )
        await self.telemetry.record_system_shutdown(
            metadata={
                "process_role": self.process_role,
                "task_queue_mode": self.settings.task_queue_mode,
                "run_task_worker": self.run_task_worker,
                "task_worker_queues": list(self.task_worker_queues),
                "last_worker_started_at": self.telemetry.last_worker_started_at,
                "last_worker_stopped_at": self.telemetry.last_worker_stopped_at,
            },
        )
        await self._stop_role()
        await self._stop_shared()

    async def _start_shared(self) -> None:
        await self.database.connect()
        await self._apply_bootstrap_admin_promotion()
        await self.redis.ensure_startup_ready()
        if self._should_start_task_runtime():
            await self.tasks.start()

    async def _start_role(self) -> int:
        if self._should_run_scheduler_loop():
            await self.scheduler.start()
        if self._should_run_thumbnail_startup_recovery():
            return await self.recover_thumbnails(include_failed=False)
        return 0

    async def _stop_role(self) -> None:
        if self._should_run_scheduler_loop():
            await self.scheduler.stop()
        if self._should_start_task_runtime():
            await self.tasks.stop()

    async def _stop_shared(self) -> None:
        await self.redis.close()
        await self.database.close()

    def _should_start_task_runtime(self) -> bool:
        return True

    def _should_run_thumbnail_startup_recovery(self) -> bool:
        return self.process_role == "worker" and "thumbnails" in self.task_worker_queues

    def _should_emit_worker_lifecycle_events(self) -> bool:
        return self.process_role == "worker"

    def _should_run_scheduler_loop(self) -> bool:
        return self.process_role == "scheduler" or (
            self.process_role == "app" and self.settings.app_scheduler_enabled
        )

    async def _enqueue_thumbnail(self, event: MediaUploaded) -> None:
        await self.tasks.enqueue(
            GENERATE_THUMBNAIL_TASK.name,
            media_id=event.media_id,
            correlation_id=event.correlation_id,
        )

    async def recover_thumbnails(self, *, include_failed: bool) -> int:
        recoverable = await self.repository.find_pending_thumbnails()
        if include_failed:
            recoverable.extend(await self.repository.find_failed_thumbnails())
        seen: set[str] = set()
        enqueued = 0
        for media in recoverable:
            if media.id in seen:
                continue
            seen.add(media.id)
            if include_failed and media.thumb_status == "failed":
                media.thumb_status = "pending"
                await self.repository.update_media(media)
            await self.tasks.enqueue(
                GENERATE_THUMBNAIL_TASK.name,
                media_id=media.id,
                correlation_id=f"recovery-{media.id}",
            )
            enqueued += 1
        if enqueued:
            logging.getLogger(__name__).info(
                "thumbnail_recovery_enqueued",
                extra={"count": enqueued, "include_failed": include_failed},
            )
        return enqueued

    async def runtime_status(self) -> dict[str, Any]:
        database_ok = False
        try:
            pool = self.database.require_pool()
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            database_ok = True
        except Exception:
            database_ok = False
        storage_ok = await self.storage.health_check()
        redis_reachable = await self.redis.ping()
        redis_configured = self.redis.enabled
        tasks_configured = redis_configured and self.settings.task_queue_mode == "redis"
        proxy_trust_warning = None
        if not self.settings.trusted_proxy_cidrs_enabled:
            proxy_trust_warning = (
                "Forwarded proxy headers are currently trusted from any client. "
                "This is fine for local development, but if you run imghost behind nginx, Caddy, Traefik, "
                "or a cloud proxy, enable trusted proxy CIDRs so only that proxy can set the public host and protocol."
            )
        return {
            "process_role": self.process_role,
            "database": {"ok": database_ok},
            "storage": {"ok": storage_ok},
            "redis": {
                "configured": redis_configured,
                "reachable": redis_reachable,
                "session_fail_closed": self.settings.session_redis_fail_closed,
                "subsystems": {
                    "sessions": self.telemetry.subsystem_snapshot(
                        "sessions",
                        configured=redis_configured,
                        default_mode="redis" if redis_configured else "disabled",
                    ),
                    "rate_limits": self.telemetry.subsystem_snapshot(
                        "rate_limits",
                        configured=redis_configured,
                        default_mode="redis" if redis_configured else "disabled",
                    ),
                    "tasks": self.telemetry.subsystem_snapshot(
                        "tasks",
                        configured=tasks_configured,
                        default_mode="redis" if tasks_configured else "fallback",
                    ),
                },
            },
            "services": {
                "app": {
                    "enabled_in_this_process": self.process_role == "app",
                },
                "worker": {
                    "enabled_in_this_process": self.run_task_worker,
                    "queues": list(self.task_worker_queues),
                    "last_started_at": self.telemetry.last_worker_started_at,
                    "last_stopped_at": self.telemetry.last_worker_stopped_at,
                    "last_task_event_at": self.telemetry.last_task_event_at,
                    "last_task_event": self.telemetry.last_task_event,
                    "last_task_failure_at": self.telemetry.last_task_failure_at,
                    "last_task_failure": self.telemetry.last_task_failure,
                },
                "scheduler": {
                    "enabled_in_this_process": self._should_run_scheduler_loop(),
                    "hosted_by": "scheduler" if self.process_role == "scheduler" else "app" if self.settings.app_scheduler_enabled else None,
                    "configured": self.settings.scheduler_enabled,
                    **self.scheduler.runtime_status(),
                },
            },
            "tasks": {
                "mode": self.settings.task_queue_mode,
                **(await self.tasks.runtime_status()),
            },
            "trusted_public_origins": list(self.settings.trusted_public_origins),
            "public_origin_enabled": self.settings.public_origin_enabled,
            "public_origin_mode": "strict" if self.settings.public_origin_enabled else "direct_request",
            "forwarded_headers_policy": "trusted_proxies_only" if self.settings.trusted_proxy_cidrs_enabled else "permissive",
            "proxy_trust_warning": proxy_trust_warning,
            "trusted_proxy_cidrs_enabled": self.settings.trusted_proxy_cidrs_enabled,
            "trusted_proxy_cidrs": list(self.settings.trusted_proxy_cidrs),
            "bootstrap_admin": dict(self.bootstrap_admin_status),
        }

    async def _apply_bootstrap_admin_promotion(self) -> None:
        username = self.settings.promote_username_to_admin
        if not username:
            return
        user = await self.repository.get_user_by_username(username)
        self.bootstrap_admin_status = {
            "enabled": True,
            "configured_username": username,
            "matched": user is not None,
            "already_admin": bool(user.is_admin) if user is not None else False,
            "promoted": False,
            "user_id": user.id if user is not None else None,
            "warning": None,
        }
        logger = logging.getLogger(__name__)
        if user is None:
            warning = f"PROMOTE_USERNAME_TO_ADMIN set to {username!r}, but no matching user exists."
            self.bootstrap_admin_status["warning"] = warning
            logger.warning("bootstrap_admin_user_missing", extra={"username": username})
            return
        if user.is_admin:
            logger.info("bootstrap_admin_user_already_admin", extra={"username": username, "user_id": user.id})
            return

        user.is_admin = True
        await self.repository.update_user(user)
        self.bootstrap_admin_status["promoted"] = True
        logger.warning("bootstrap_admin_user_promoted", extra={"username": username, "user_id": user.id})
        await self.telemetry.record_bootstrap_admin_promoted(user_id=user.id, username=username)

    async def readiness_status(self) -> dict[str, Any]:
        runtime = await self.runtime_status()
        ok = runtime["database"]["ok"] and runtime["storage"]["ok"]
        if self.settings.redis_mode == "required" and runtime["redis"]["configured"] and not runtime["redis"]["reachable"]:
            ok = False
        return {
            "ok": bool(ok),
            "degraded": not bool(ok),
            "database": {"ok": runtime["database"]["ok"]},
            "storage": {"ok": runtime["storage"]["ok"]},
            "redis": {
                "configured": runtime["redis"]["configured"],
                "reachable": runtime["redis"]["reachable"],
            },
            "tasks": {
                "mode": runtime["tasks"]["mode"],
            },
        }
