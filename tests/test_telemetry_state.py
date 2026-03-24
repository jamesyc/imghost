from __future__ import annotations

import pytest

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


def test_telemetry_state_snapshot_for_disabled_and_fallback_defaults() -> None:
    state = TelemetryState()

    disabled = state.subsystem_snapshot("sessions", configured=False, default_mode="redis")
    fallback = state.subsystem_snapshot("rate_limits", configured=True, default_mode="fallback")

    assert disabled == {
        "configured": False,
        "reachable": False,
        "effective_mode": "disabled",
        "degraded": False,
        "last_operation": None,
        "last_error": None,
        "last_degraded_at": None,
        "last_recovered_at": None,
    }
    assert fallback["configured"] is True
    assert fallback["reachable"] is False
    assert fallback["effective_mode"] == "fallback"
    assert fallback["degraded"] is True


def test_telemetry_state_repeated_degrade_and_recover_updates_fields_without_resetting_transition_timestamps(monkeypatch) -> None:
    times = iter([10.0, 20.0, 30.0, 40.0])
    monkeypatch.setattr("imghost.telemetry.state.time", lambda: next(times))
    state = TelemetryState()

    state.mark_subsystem_degraded("tasks", operation="enqueue", reason="redis_down")
    first = state.subsystem_snapshot("tasks", configured=True, default_mode="redis")
    state.mark_subsystem_degraded("tasks", operation="poll", reason="still_down")
    second = state.subsystem_snapshot("tasks", configured=True, default_mode="redis")
    state.mark_subsystem_recovered("tasks", operation="enqueue")
    third = state.subsystem_snapshot("tasks", configured=True, default_mode="redis")
    state.mark_subsystem_recovered("tasks", operation="poll")
    fourth = state.subsystem_snapshot("tasks", configured=True, default_mode="redis")

    assert first["last_degraded_at"] == 10.0
    assert second["last_degraded_at"] == 10.0
    assert second["last_error"] == "still_down"
    assert second["last_operation"] == "poll"
    assert third["last_recovered_at"] == 30.0
    assert fourth["last_recovered_at"] == 30.0
    assert fourth["last_operation"] == "poll"


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


def test_telemetry_state_task_failure_overwrites_previous_failure() -> None:
    state = TelemetryState()

    state.record_task_failure(task_name="generate_thumbnail", details={"reason": "boom"})
    state.record_task_failure(task_name="enqueue_task", details={"reason": "still_boom", "queue": "default"})

    assert state.last_task_failure == {
        "task_name": "enqueue_task",
        "reason": "still_boom",
        "queue": "default",
    }


def test_telemetry_state_throttles_duplicate_untrusted_origin_warnings() -> None:
    state = TelemetryState(origin_warning_cooldown_seconds=60.0)

    assert state.should_log_untrusted_origin("request", "https://bad.example") is True
    assert state.should_log_untrusted_origin("request", "https://bad.example") is False
    assert state.should_log_untrusted_origin("forwarded", "https://bad.example") is True


def test_telemetry_state_untrusted_origin_cooldown_expires(monkeypatch) -> None:
    now = {"value": 100.0}
    monkeypatch.setattr("imghost.telemetry.state.time", lambda: now["value"])
    state = TelemetryState(origin_warning_cooldown_seconds=60.0)

    assert state.should_log_untrusted_origin("request", "https://bad.example") is True
    now["value"] = 120.0
    assert state.should_log_untrusted_origin("request", "https://bad.example") is False
    now["value"] = 161.0
    assert state.should_log_untrusted_origin("request", "https://bad.example") is True
