from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import Request

from ..db import Database
from ..events import EventBus
from ..models import Media, User
from ..models import AuditEvent
from . import actions
from .helpers import (
    record_admin_access_denied,
    record_admin_api_read,
    record_admin_page_viewed,
    record_api_key_auth_failed,
    record_api_key_authenticated,
    record_cli_command,
    record_csrf_blocked,
    record_login_failed,
    record_login_succeeded,
    record_oauth_disconnected,
    record_oauth_denied,
    record_oauth_succeeded,
    record_logout_succeeded,
    record_registration_denied,
    record_system_event,
    record_thumbnail_failure,
)
from .service import TelemetryService
from .sinks.jsonlog import JsonLogTelemetrySink
from .sinks.postgres import PostgresTelemetrySink
from .state import TelemetryState
from .subscribers import register_telemetry_subscribers


class Telemetry:
    def __init__(self, service: TelemetryService, state: TelemetryState) -> None:
        self._service = service
        self._state = state

    async def emit_event(self, **kwargs) -> None:
        await self._service.emit_event(**kwargs)

    async def record_login_failed(self, request: Request, *, login_identifier: str, reason: str) -> None:
        await record_login_failed(self._service, request, login_identifier=login_identifier, reason=reason)

    async def record_login_succeeded(self, request: Request, *, user: User, remember_me: bool) -> None:
        await record_login_succeeded(self._service, request, user=user, remember_me=remember_me)

    async def record_registration_denied(
        self,
        request: Request,
        *,
        username: str,
        email: str,
        reason: str = "registration_disabled",
    ) -> None:
        await record_registration_denied(self._service, request, username=username, email=email, reason=reason)

    async def record_logout_succeeded(self, request: Request, *, user: User) -> None:
        await record_logout_succeeded(self._service, request, user=user)

    async def record_api_key_auth_failed(
        self,
        request: Request,
        *,
        actor: User | None,
        object_type: str,
        object_id: str | None,
        reason: str,
        admin_denial: bool = False,
    ) -> None:
        await record_api_key_auth_failed(
            self._service,
            request,
            actor=actor,
            object_type=object_type,
            object_id=object_id,
            reason=reason,
            admin_denial=admin_denial,
        )

    async def record_api_key_authenticated(self, request: Request, *, user: User, api_key_id: str) -> None:
        await record_api_key_authenticated(self._service, request, user=user, api_key_id=api_key_id)

    async def record_admin_access_denied(
        self,
        request: Request,
        *,
        actor: User | None,
        object_type: str,
        reason: str,
        source: str,
    ) -> None:
        await record_admin_access_denied(self._service, request, actor=actor, object_type=object_type, reason=reason, source=source)

    async def record_csrf_blocked(self, request: Request) -> None:
        await record_csrf_blocked(self._service, request)

    async def record_admin_api_read(
        self,
        request: Request,
        *,
        admin: User,
        resource: str,
        object_type: str = "admin_api",
        object_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await record_admin_api_read(
            self._service,
            request,
            admin=admin,
            resource=resource,
            object_type=object_type,
            object_id=object_id,
            metadata=metadata,
        )

    async def record_admin_page_viewed(
        self,
        request: Request,
        *,
        user: User,
        page_name: str,
        object_id: str | None = None,
    ) -> None:
        await record_admin_page_viewed(self._service, request, user=user, page_name=page_name, object_id=object_id)

    async def record_oauth_denied(
        self,
        request: Request,
        *,
        reason: str,
        actor: User | None = None,
        object_id: str | None = None,
    ) -> None:
        await record_oauth_denied(self._service, request, reason=reason, actor=actor, object_id=object_id)

    async def record_oauth_succeeded(
        self,
        request: Request,
        *,
        user: User,
        provider: str,
        provider_uid: str,
        outcome: str,
    ) -> None:
        await record_oauth_succeeded(
            self._service,
            request,
            user=user,
            provider=provider,
            provider_uid=provider_uid,
            outcome=outcome,
        )

    async def record_oauth_disconnected(self, request: Request, *, user: User, provider: str) -> None:
        await record_oauth_disconnected(self._service, request, user=user, provider=provider)

    async def record_system_startup(self, *, metadata: dict[str, Any]) -> None:
        await record_system_event(
            self._service,
            event_type=actions.SYSTEM_STARTUP,
            action="system.startup",
            source="system",
            metadata=metadata,
        )

    async def record_system_shutdown(self, *, metadata: dict[str, Any]) -> None:
        await record_system_event(
            self._service,
            event_type=actions.SYSTEM_SHUTDOWN,
            action="system.shutdown",
            source="system",
            metadata=metadata,
        )

    async def record_worker_started_event(self, *, metadata: dict[str, Any]) -> None:
        await record_system_event(
            self._service,
            event_type=actions.WORKER_STARTED,
            action="worker.start",
            source="worker",
            metadata=metadata,
        )

    async def record_worker_stopped_event(self, *, metadata: dict[str, Any]) -> None:
        await record_system_event(
            self._service,
            event_type=actions.WORKER_STOPPED,
            action="worker.stop",
            source="worker",
            metadata=metadata,
        )

    async def record_bootstrap_admin_promoted(self, *, user_id: str, username: str) -> None:
        await record_system_event(
            self._service,
            event_type=actions.SYSTEM_BOOTSTRAP_ADMIN_PROMOTED,
            action="system.bootstrap_admin.promote",
            source="system",
            object_type="user",
            object_id=user_id,
            metadata={"username": username},
        )

    async def record_cli_command(
        self,
        *,
        action: str,
        object_type: str,
        object_id: str,
        metadata: dict[str, Any] | None = None,
        argv: list[str],
    ) -> None:
        await record_cli_command(
            self._service,
            action=action,
            object_type=object_type,
            object_id=object_id,
            metadata=metadata,
            argv=argv,
        )

    def record_thumbnail_failure(
        self,
        *,
        media: Media,
        correlation_id: str,
        reason: str,
        error: Exception,
    ) -> None:
        record_thumbnail_failure(
            telemetry_state=self._state,
            media=media,
            correlation_id=correlation_id,
            reason=reason,
            error=error,
        )

    async def query_audit_log(
        self,
        *,
        event_type: str | None = None,
        action: str | None = None,
        result: str | None = None,
        source: str | None = None,
        actor_id: str | None = None,
        user_id: str | None = None,
        correlation_id: str | None = None,
        request_id: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditEvent]:
        return await self._service.query_audit_log(
            event_type=event_type,
            action=action,
            result=result,
            source=source,
            actor_id=actor_id,
            user_id=user_id,
            correlation_id=correlation_id,
            request_id=request_id,
            after=after,
            before=before,
            limit=limit,
            offset=offset,
        )

    def mark_subsystem_degraded(self, subsystem: str, *, operation: str, reason: str) -> None:
        self._state.mark_subsystem_degraded(subsystem, operation=operation, reason=reason)

    def mark_subsystem_recovered(self, subsystem: str, *, operation: str) -> None:
        self._state.mark_subsystem_recovered(subsystem, operation=operation)

    def subsystem_snapshot(self, subsystem: str, *, configured: bool, default_mode: str) -> dict[str, Any]:
        return self._state.subsystem_snapshot(subsystem, configured=configured, default_mode=default_mode)

    def should_log_untrusted_origin(self, source: str, candidate_origin: str) -> bool:
        return self._state.should_log_untrusted_origin(source, candidate_origin)

    def record_task_failure(self, *, task_name: str, details: dict[str, Any]) -> None:
        self._state.record_task_failure(task_name=task_name, details=details)

    def mark_worker_started(self) -> None:
        self._state.mark_worker_started()

    def mark_worker_stopped(self) -> None:
        self._state.mark_worker_stopped()

    @property
    def last_worker_started_at(self) -> float | None:
        return self._state.last_worker_started_at

    @property
    def last_worker_stopped_at(self) -> float | None:
        return self._state.last_worker_stopped_at

    @property
    def last_task_failure_at(self) -> float | None:
        return self._state.last_task_failure_at

    @property
    def last_task_failure(self) -> dict[str, Any] | None:
        return self._state.last_task_failure


def build_telemetry(database: Database, event_bus: EventBus) -> Telemetry:
    telemetry_state = TelemetryState()
    telemetry_db_sink = PostgresTelemetrySink(database)
    service = TelemetryService(
        [telemetry_db_sink, JsonLogTelemetrySink(logging.getLogger("imghost.telemetry"))],
        query_backend=telemetry_db_sink,
    )
    telemetry = Telemetry(service, telemetry_state)
    register_telemetry_subscribers(event_bus, service)
    return telemetry
