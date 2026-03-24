from __future__ import annotations

from imghost.telemetry.state import TelemetryState


def test_telemetry_state_tracks_subsystem_degradation_and_recovery() -> None:
    state = TelemetryState()

    state.mark_subsystem_degraded("tasks", operation="enqueue task", reason="redis_unavailable")
    degraded = state.subsystem_snapshot("tasks", configured=True, default_mode="redis")
    assert degraded["degraded"] is True
    assert degraded["reachable"] is False
    assert degraded["effective_mode"] == "fallback"
    assert degraded["last_operation"] == "enqueue task"
    assert degraded["last_error"] == "redis_unavailable"

    state.mark_subsystem_recovered("tasks", operation="enqueue task")
    recovered = state.subsystem_snapshot("tasks", configured=True, default_mode="redis")
    assert recovered["degraded"] is False
    assert recovered["reachable"] is True
    assert recovered["effective_mode"] == "redis"
    assert recovered["last_operation"] == "enqueue task"


def test_telemetry_state_tracks_worker_and_task_failure_state() -> None:
    state = TelemetryState()

    state.mark_worker_started()
    assert state.last_worker_started_at is not None

    state.record_task_failure(task_name="generate_thumbnail", details={"reason": "boom", "media_id": "media-1"})
    assert state.last_task_failure_at is not None
    assert state.last_task_failure == {
        "task_name": "generate_thumbnail",
        "reason": "boom",
        "media_id": "media-1",
    }

    state.mark_worker_stopped()
    assert state.last_worker_stopped_at is not None


def test_telemetry_state_throttles_duplicate_untrusted_origin_warnings() -> None:
    state = TelemetryState(origin_warning_cooldown_seconds=60.0)

    assert state.should_log_untrusted_origin("request", "https://bad.example") is True
    assert state.should_log_untrusted_origin("request", "https://bad.example") is False
    assert state.should_log_untrusted_origin("forwarded", "https://bad.example") is True
