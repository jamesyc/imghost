from __future__ import annotations

import logging
from time import time
from typing import Any

logger = logging.getLogger(__name__)


class TelemetryState:
    def __init__(self, *, origin_warning_cooldown_seconds: float = 300.0) -> None:
        self.origin_warning_cooldown_seconds = origin_warning_cooldown_seconds
        self._subsystems: dict[str, dict[str, Any]] = {}
        self._origin_warning_deadlines: dict[tuple[str, str], float] = {}
        self.last_worker_started_at: float | None = None
        self.last_worker_stopped_at: float | None = None
        self.last_task_event_at: float | None = None
        self.last_task_event: dict[str, Any] | None = None
        self.recent_task_events: list[dict[str, Any]] = []
        self.last_task_failure_at: float | None = None
        self.last_task_failure: dict[str, Any] | None = None

    def mark_subsystem_degraded(self, subsystem: str, *, operation: str, reason: str) -> None:
        current = self._subsystems.setdefault(subsystem, {"degraded": False})
        now = time()
        current["reachable"] = False
        current["effective_mode"] = "fallback"
        current["last_error"] = reason
        current["last_operation"] = operation
        if current.get("degraded"):
            return
        current["degraded"] = True
        current["last_degraded_at"] = now
        logger.warning(
            "redis_subsystem_degraded",
            extra={"subsystem": subsystem, "operation": operation, "reason": reason},
        )

    def mark_subsystem_recovered(self, subsystem: str, *, operation: str) -> None:
        current = self._subsystems.setdefault(subsystem, {"degraded": False})
        now = time()
        current["reachable"] = True
        current["effective_mode"] = "redis"
        current["last_operation"] = operation
        if not current.get("degraded"):
            return
        current["degraded"] = False
        current["last_recovered_at"] = now
        logger.info("redis_subsystem_recovered", extra={"subsystem": subsystem, "operation": operation})

    def subsystem_snapshot(self, subsystem: str, *, configured: bool, default_mode: str) -> dict[str, Any]:
        current = self._subsystems.get(subsystem, {})
        if not configured:
            return {
                "configured": False,
                "reachable": False,
                "effective_mode": "disabled",
                "degraded": False,
                "last_operation": current.get("last_operation"),
                "last_error": current.get("last_error"),
                "last_degraded_at": current.get("last_degraded_at"),
                "last_recovered_at": current.get("last_recovered_at"),
            }
        return {
            "configured": True,
            "reachable": current.get("reachable", default_mode == "redis"),
            "effective_mode": current.get("effective_mode", default_mode),
            "degraded": current.get("degraded", default_mode == "fallback"),
            "last_operation": current.get("last_operation"),
            "last_error": current.get("last_error"),
            "last_degraded_at": current.get("last_degraded_at"),
            "last_recovered_at": current.get("last_recovered_at"),
        }

    def should_log_untrusted_origin(self, source: str, candidate_origin: str) -> bool:
        now = time()
        key = (source, candidate_origin)
        deadline = self._origin_warning_deadlines.get(key, 0.0)
        if now < deadline:
            return False
        self._origin_warning_deadlines[key] = now + self.origin_warning_cooldown_seconds
        return True

    def record_task_failure(self, *, task_name: str, details: dict[str, Any]) -> None:
        self.last_task_failure_at = time()
        self.last_task_failure = {"task_name": task_name, **details}

    def record_task_state(self, *, task_name: str, state: str, details: dict[str, Any]) -> None:
        self.last_task_event_at = time()
        event = {"task_name": task_name, "state": state, **details}
        self.last_task_event = event
        self.recent_task_events.append(event)
        if len(self.recent_task_events) > 25:
            self.recent_task_events = self.recent_task_events[-25:]

    def mark_worker_started(self) -> None:
        self.last_worker_started_at = time()
        logger.info("worker_started")

    def mark_worker_stopped(self) -> None:
        self.last_worker_stopped_at = time()
        logger.info("worker_stopped")
