from __future__ import annotations

import asyncio

from imghost.telemetry.api import Telemetry
from imghost.telemetry.state import TelemetryState


class _RecordingService:
    def __init__(self) -> None:
        self.emitted: list[dict[str, object]] = []
        self.queries: list[dict[str, object]] = []

    async def emit_event(self, **kwargs) -> None:
        self.emitted.append(kwargs)

    async def query_audit_log(self, **kwargs):
        self.queries.append(kwargs)
        return ["row-1"]


def test_telemetry_facade_delegates_emit_and_query() -> None:
    service = _RecordingService()
    telemetry = Telemetry(service, TelemetryState())

    async def run() -> None:
        await telemetry.emit_event(event_type="test_event", action="test.action", result="success")
        rows = await telemetry.query_audit_log(limit=5)
        assert rows == ["row-1"]

    asyncio.run(run())

    assert service.emitted == [{"event_type": "test_event", "action": "test.action", "result": "success"}]
    assert service.queries == [
        {
            "event_type": None,
            "action": None,
            "result": None,
            "source": None,
            "actor_id": None,
            "user_id": None,
            "correlation_id": None,
            "request_id": None,
            "after": None,
            "before": None,
            "limit": 5,
            "offset": 0,
        }
    ]


def test_telemetry_facade_delegates_state_helpers() -> None:
    telemetry = Telemetry(_RecordingService(), TelemetryState())

    telemetry.mark_subsystem_degraded("tasks", operation="enqueue", reason="redis_unavailable")
    degraded = telemetry.subsystem_snapshot("tasks", configured=True, default_mode="redis")
    assert degraded["degraded"] is True

    telemetry.mark_subsystem_recovered("tasks", operation="enqueue")
    recovered = telemetry.subsystem_snapshot("tasks", configured=True, default_mode="redis")
    assert recovered["degraded"] is False

    telemetry.record_task_failure(task_name="generate_thumbnail", details={"reason": "boom"})
    assert telemetry.last_task_failure == {"task_name": "generate_thumbnail", "reason": "boom"}
