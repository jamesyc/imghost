from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Mapping, Sequence

from ..models import AuditEvent
from .models import AuditActor, AuditObject, AuditProcessContext, AuditRecord, AuditRequestContext
from .redaction import redact_record
from .sinks.base import AuditQueryBackend, AuditSink

logger = logging.getLogger(__name__)


class AuditService:
    def __init__(self, sinks: Sequence[AuditSink], *, query_backend: AuditQueryBackend | None = None) -> None:
        self._sinks = list(sinks)
        self._query_backend = query_backend

    async def emit(self, record: AuditRecord) -> None:
        redacted = redact_record(record)
        for sink in self._sinks:
            try:
                await sink.write(redacted)
            except Exception:
                logger.exception("audit_sink_write_failed", extra={"event_type": redacted.event_type, "sink": type(sink).__name__})
                await asyncio.sleep(0)

    async def emit_action(
        self,
        *,
        event_type: str,
        action: str,
        result: str,
        actor: AuditActor,
        object: AuditObject,
        metadata: Mapping[str, Any] | None = None,
        request: AuditRequestContext | None = None,
        process: AuditProcessContext | None = None,
        reason: str | None = None,
        actor_ip_hash: str | None = None,
    ) -> None:
        await self.emit(
            AuditRecord(
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
