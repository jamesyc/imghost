from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from ...db import Database
from ...models import AuditEvent
from ..models import AuditRecord


class PostgresAuditSink:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def write(self, record: AuditRecord) -> None:
        pool = self.database.require_pool()
        event_id = str(uuid4())
        correlation_id = None
        if record.request is not None:
            correlation_id = record.request.correlation_id
        if correlation_id is None:
            raw_correlation = record.metadata.get("correlation_id")
            correlation_id = raw_correlation if isinstance(raw_correlation, str) and raw_correlation else None
        metadata = dict(record.metadata)
        metadata.setdefault("action", record.action)
        metadata.setdefault("result", record.result)
        metadata.setdefault("actor_type", record.actor.type)
        if record.reason is not None:
            metadata.setdefault("reason", record.reason)
        if record.request is not None:
            metadata.setdefault("request_id", record.request.request_id)
            metadata.setdefault("request", record.request.to_dict())
            metadata.setdefault("source", metadata.get("source") or "web")
        if record.process is not None:
            metadata.setdefault("process", record.process.to_dict())
            metadata.setdefault("source", metadata.get("source") or record.process.source)
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO audit_log (
                  id, event_type, action, result, source, actor_type, actor_id, actor_ip_hash, request_id, route, method, reason,
                  target_type, target_id, correlation_id, metadata, created_at
                ) VALUES (
                  $1::uuid, $2, $3, $4, $5, $6, $7::uuid, $8, $9, $10, $11, $12, $13, $14, $15, $16::jsonb, $17
                )
                """,
                event_id,
                record.event_type,
                record.action,
                record.result,
                metadata.get("source"),
                record.actor.type,
                record.actor.id,
                record.actor_ip_hash,
                record.request.request_id if record.request is not None else metadata.get("request_id"),
                (record.request.route if record.request is not None else None) or (record.request.path if record.request is not None else None),
                record.request.method if record.request is not None else None,
                record.reason,
                record.object.type,
                record.object.id,
                correlation_id,
                metadata,
                record.occurred_at,
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
        query = """
        SELECT
          id,
          event_type,
          action,
          result,
          source,
          actor_type,
          actor_id,
          actor_ip_hash,
          request_id,
          route,
          method,
          reason,
          target_type,
          target_id,
          correlation_id,
          metadata,
          created_at
        FROM audit_log
        WHERE ($1::text IS NULL OR event_type = $1)
          AND ($2::text IS NULL OR action = $2)
          AND ($3::text IS NULL OR result = $3)
          AND ($4::text IS NULL OR source = $4)
          AND ($5::uuid IS NULL OR actor_id = $5::uuid)
          AND ($6::text IS NULL OR correlation_id = $6)
          AND ($7::text IS NULL OR request_id = $7)
          AND ($8::timestamptz IS NULL OR created_at >= $8)
          AND ($9::timestamptz IS NULL OR created_at <= $9)
          AND ($10::text IS NULL OR actor_id::text = $10 OR target_id = $10)
        ORDER BY created_at DESC
        LIMIT $11 OFFSET $12
        """
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                query,
                event_type,
                action,
                result,
                source,
                actor_id,
                correlation_id,
                request_id,
                after,
                before,
                user_id,
                limit,
                offset,
            )
        return [
            AuditEvent(
                id=str(row["id"]),
                event_type=row["event_type"],
                actor_id=str(row["actor_id"]) if row["actor_id"] is not None else None,
                actor_ip_hash=row["actor_ip_hash"],
                target_type=row["target_type"],
                target_id=row["target_id"],
                correlation_id=row["correlation_id"],
                metadata=row["metadata"] or {},
                created_at=row["created_at"],
                action=row["action"] or (row["metadata"] or {}).get("action"),
                result=row["result"] or (row["metadata"] or {}).get("result"),
                source=row["source"] or (row["metadata"] or {}).get("source"),
                actor_type=row["actor_type"] or (row["metadata"] or {}).get("actor_type"),
                request_id=row["request_id"] or (row["metadata"] or {}).get("request_id"),
                route=row["route"] or (((row["metadata"] or {}).get("request") or {}).get("route") or ((row["metadata"] or {}).get("request") or {}).get("path")),
                method=row["method"] or ((row["metadata"] or {}).get("request") or {}).get("method"),
                reason=row["reason"] or (row["metadata"] or {}).get("reason"),
            )
            for row in rows
        ]
