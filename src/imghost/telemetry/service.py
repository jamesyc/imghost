from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Mapping, Sequence

from ..models import AuditEvent
from .models import TelemetryActor, TelemetryEvent, TelemetryObject, TelemetryProcessContext, TelemetryRequestContext
from .redaction import redact_record
from .sinks.base import TelemetryQueryBackend, TelemetrySink

logger = logging.getLogger(__name__)


class TelemetryService:
    def __init__(self, sinks: Sequence[TelemetrySink], *, query_backend: TelemetryQueryBackend | None = None) -> None:
        self._sinks = list(sinks)
        self._query_backend = query_backend

    async def emit(self, record: TelemetryEvent) -> None:
        redacted = redact_record(record)
        for sink in self._sinks:
            try:
                await sink.write(redacted)
            except Exception:
                logger.exception("audit_sink_write_failed", extra={"event_type": redacted.event_type, "sink": type(sink).__name__})
                await asyncio.sleep(0)

    async def emit_event(
        self,
        *,
        event_type: str,
        action: str,
        result: str,
        actor: TelemetryActor,
        object: TelemetryObject,
        metadata: Mapping[str, Any] | None = None,
        request: TelemetryRequestContext | None = None,
        process: TelemetryProcessContext | None = None,
        reason: str | None = None,
        actor_ip_hash: str | None = None,
    ) -> None:
        await self.emit(
            TelemetryEvent(
                event_type=event_type,
                action=action,
                result=result,
                actor=actor,
                object=object,
                metadata=dict(metadata or {}),
                request=request,
                process=process,
                reason=reason,
                actor_ip_hash=actor_ip_hash,
            )
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
        if self._query_backend is None:
            raise RuntimeError("Audit query backend is not configured.")
        return await self._query_backend.query_audit_log(
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

    async def count_audit_events_older_than(self, before: datetime) -> int:
        if self._query_backend is None:
            raise RuntimeError("Audit query backend is not configured.")
        return await self._query_backend.count_audit_events_older_than(before)

    async def delete_audit_events_older_than(self, before: datetime) -> int:
        if self._query_backend is None:
            raise RuntimeError("Audit query backend is not configured.")
        return await self._query_backend.delete_audit_events_older_than(before)
