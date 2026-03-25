from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ...models import AuditEvent
from ..models import TelemetryEvent


class TelemetrySink(Protocol):
    async def write(self, record: TelemetryEvent) -> None: ...


class TelemetryQueryBackend(Protocol):
    async def count_audit_events_older_than(self, before: datetime) -> int: ...

    async def delete_audit_events_older_than(self, before: datetime) -> int: ...

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
    ) -> list[AuditEvent]: ...
