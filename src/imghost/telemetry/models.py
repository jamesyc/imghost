from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from ..models import utcnow


@dataclass(slots=True)
class TelemetryActor:
    id: str | None
    type: str
    display: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TelemetryObject:
    type: str
    id: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TelemetryRequestContext:
    request_id: str | None
    correlation_id: str | None
    method: str | None
    route: str | None
    path: str | None
    host: str | None
    origin: str | None
    referer: str | None
    user_agent: str | None
    client_ip: str | None
    forwarded_for: str | None
    auth_method: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TelemetryProcessContext:
    source: str
    hostname: str | None
    pid: int | None
    command: str | None
    build_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TelemetryEvent:
    event_type: str
    action: str
    result: str
    actor: TelemetryActor
    object: TelemetryObject
    metadata: dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=utcnow)
    request: TelemetryRequestContext | None = None
    process: TelemetryProcessContext | None = None
    reason: str | None = None
    actor_ip_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "event_type": self.event_type,
            "action": self.action,
            "result": self.result,
            "actor": self.actor.to_dict(),
            "object": self.object.to_dict(),
            "metadata": dict(self.metadata),
            "occurred_at": self.occurred_at.isoformat(),
            "reason": self.reason,
            "actor_ip_hash": self.actor_ip_hash,
        }
        if self.request is not None:
            payload["request"] = self.request.to_dict()
        if self.process is not None:
            payload["process"] = self.process.to_dict()
        return payload
