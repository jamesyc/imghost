from __future__ import annotations

import logging
from typing import Any

from .audit import AuditService, JsonLogAuditSink, PostgresAuditSink, actions, register_audit_subscribers
from .audit.context import build_runtime_process_context
from .audit.models import AuditActor, AuditObject
from .config import Settings
from .db import Database
from .events import EventBus, MediaUploaded
from .observability import ObservabilityState
from .processors import build_processor_registry
from .rate_limits import build_rate_limiter
from .redis_support import RedisHandle
from .repositories import PostgresRepository
from .runtime_config import PostgresRuntimeConfig
from .service import UploadService
from .sessions import SessionBackend, build_session_backend
from .storage import build_storage_backend
from .tasks import AsyncTaskQueue, RedisTaskQueue, SyncTaskQueue, TaskContext, TaskQueue


class AppState:
    def __init__(self, settings: Settings, *, run_task_worker: bool | None = None) -> None:
        self.settings = settings
        self.run_task_worker = settings.task_worker_enabled if run_task_worker is None else run_task_worker
        self.database = Database(settings.database_url)
        self.observability = ObservabilityState()
        self.event_bus = EventBus()
        self.repository = PostgresRepository(self.database)
        audit_db_sink = PostgresAuditSink(self.database)
        self.audit = AuditService(
            [audit_db_sink, JsonLogAuditSink(logging.getLogger("imghost.audit"))],
            query_backend=audit_db_sink,
        )
        self.runtime_config = PostgresRuntimeConfig(self.database)
        self.redis = RedisHandle(settings)
        self.session_backend: SessionBackend = build_session_backend(settings, self.redis, self.observability)
        self.rate_limiter = build_rate_limiter(self.runtime_config, self.redis, self.observability)
        self.storage = build_storage_backend(settings)
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
        )
        self.tasks.register("generate_thumbnail", self.uploads.generate_thumbnail)
        self.event_bus.subscribe(MediaUploaded, self._enqueue_thumbnail)
        register_audit_subscribers(self.event_bus, self.audit)

    def _build_task_queue(self) -> TaskQueue:
        context = TaskContext(self.repository, self.storage, self.processors)
        if self.settings.task_queue_mode == "sync":
            return SyncTaskQueue(context)
        if self.settings.task_queue_mode == "redis" and self.redis.enabled:
            return RedisTaskQueue(
                self.redis,
                context,
                self.observability,
                worker_count=self.settings.thumbnail_worker_count,
                run_worker=self.run_task_worker,
            )
        return AsyncTaskQueue(context, worker_count=self.settings.thumbnail_worker_count)

    async def start(self) -> None:
        await self.database.connect()
        await self.redis.ensure_startup_ready()
        await self.tasks.start()
        recovered = await self.recover_thumbnails(include_failed=False)
        await self._audit_system_event(
            event_type=actions.SYSTEM_STARTUP,
            action="system.startup",
            source="system",
            metadata={
                "base_url": self.settings.base_url,
                "public_origin_enabled": self.settings.public_origin_enabled,
                "trusted_proxy_cidrs_enabled": self.settings.trusted_proxy_cidrs_enabled,
                "storage_backend": self.settings.storage_backend,
                "redis_enabled": self.redis.enabled,
                "redis_mode": self.settings.redis_mode,
                "task_queue_mode": self.settings.task_queue_mode,
                "task_worker_enabled": self.settings.task_worker_enabled,
                "run_task_worker": self.run_task_worker,
                "thumbnail_worker_count": self.settings.thumbnail_worker_count,
                "session_redis_fail_closed": self.settings.session_redis_fail_closed,
                "recovered_thumbnail_count": recovered,
            },
        )
        if self.run_task_worker:
            await self._audit_system_event(
                event_type=actions.WORKER_STARTED,
                action="worker.start",
                source="worker",
                metadata={
                    "task_queue_mode": self.settings.task_queue_mode,
                    "thumbnail_worker_count": self.settings.thumbnail_worker_count,
                    "recovered_thumbnail_count": recovered,
                },
            )

    async def stop(self) -> None:
        await self.tasks.stop()
        if self.run_task_worker:
            await self._audit_system_event(
                event_type=actions.WORKER_STOPPED,
                action="worker.stop",
                source="worker",
                metadata={
                    "task_queue_mode": self.settings.task_queue_mode,
                    "thumbnail_worker_count": self.settings.thumbnail_worker_count,
                },
            )
        await self._audit_system_event(
            event_type=actions.SYSTEM_SHUTDOWN,
            action="system.shutdown",
            source="system",
            metadata={
                "task_queue_mode": self.settings.task_queue_mode,
                "run_task_worker": self.run_task_worker,
                "last_worker_started_at": self.observability.last_worker_started_at,
                "last_worker_stopped_at": self.observability.last_worker_stopped_at,
            },
        )
        await self.redis.close()
        await self.database.close()

    async def _enqueue_thumbnail(self, event: MediaUploaded) -> None:
        await self.tasks.enqueue(
            "generate_thumbnail",
            queue="thumbnails",
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
                "generate_thumbnail",
                queue="thumbnails",
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
            "database": {"ok": database_ok},
            "storage": {"ok": storage_ok},
            "redis": {
                "configured": redis_configured,
                "reachable": redis_reachable,
                "session_fail_closed": self.settings.session_redis_fail_closed,
                "subsystems": {
                    "sessions": self.observability.subsystem_snapshot(
                        "sessions",
                        configured=redis_configured,
                        default_mode="redis" if redis_configured else "disabled",
                    ),
                    "rate_limits": self.observability.subsystem_snapshot(
                        "rate_limits",
                        configured=redis_configured,
                        default_mode="redis" if redis_configured else "disabled",
                    ),
                    "tasks": self.observability.subsystem_snapshot(
                        "tasks",
                        configured=tasks_configured,
                        default_mode="redis" if tasks_configured else "fallback",
                    ),
                },
            },
            "worker": {
                "enabled_in_this_process": self.run_task_worker,
                "last_started_at": self.observability.last_worker_started_at,
                "last_stopped_at": self.observability.last_worker_stopped_at,
                "last_task_failure_at": self.observability.last_task_failure_at,
                "last_task_failure": self.observability.last_task_failure,
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
        }

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

    async def _audit_system_event(
        self,
        *,
        event_type: str,
        action: str,
        source: str,
        metadata: dict[str, Any],
    ) -> None:
        await self.audit.emit_action(
            event_type=event_type,
            action=action,
            result="success",
            actor=AuditActor(id=None, type=source),
            object=AuditObject(type="system", id="imghost"),
            metadata={**metadata, "source": source},
            process=build_runtime_process_context(source),
        )
