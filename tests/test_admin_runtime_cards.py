from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest


CARD_RE = re.compile(
    r'<article class="admin-runtime-card">\s*'
    r'<div class="admin-status-pill" data-tone="(?P<tone>[^"]*)">(?P<status>.*?)</div>\s*'
    r"<h3>(?P<label>.*?)</h3>\s*"
    r'<p class="hint">(?P<hint>.*?)</p>',
    re.S,
)
ADMIN_COMMON_JS = Path(__file__).resolve().parents[1] / "src" / "imghost" / "static" / "js" / "admin-common.js"


@pytest.fixture(autouse=True)
def clean_database() -> None:
    return None


def _deep_update(target: dict, updates: dict) -> dict:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
            continue
        target[key] = value
    return target


def _base_payload() -> dict:
    return {
        "process_role": "app",
        "database": {"ok": True},
        "storage": {"ok": True},
        "redis": {
            "configured": True,
            "reachable": True,
            "session_fail_closed": True,
            "subsystems": {
                "sessions": {"effective_mode": "redis"},
                "rate_limits": {"effective_mode": "redis"},
            },
        },
        "tasks": {
            "mode": "redis",
            "queue_depth": 0,
            "queues": {},
        },
        "services": {
            "app": {"enabled_in_this_process": True},
            "worker": {
                "enabled_in_this_process": False,
                "queues": [],
                "last_task_failure": None,
                "last_started_at": None,
            },
            "scheduler": {
                "enabled_in_this_process": False,
                "last_enqueue_error": None,
                "poll_seconds": 30,
                "lease_enabled": True,
                "lease_seconds": 900,
                "jobs": {},
            },
        },
        "bootstrap_admin": {
            "enabled": False,
            "configured_username": None,
            "matched": False,
            "already_admin": False,
            "promoted": False,
            "warning": None,
        },
        "public_origin_enabled": True,
        "public_origin_mode": "strict",
        "forwarded_headers_policy": "trusted_proxies_only",
        "trusted_proxy_cidrs_enabled": True,
        "trusted_proxy_cidrs": ["127.0.0.1/32"],
        "proxy_trust_warning": None,
    }


def _render_cards(payload_updates: dict | None = None) -> dict[str, dict[str, str]]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to exercise admin-common.js")

    payload = _base_payload()
    if payload_updates:
        _deep_update(payload, deepcopy(payload_updates))

    script = f"""
const fs = require("fs");
global.window = globalThis;
eval(fs.readFileSync({json.dumps(str(ADMIN_COMMON_JS))}, "utf8"));
const html = window.renderAdminRuntimeCards({json.dumps(payload)});
process.stdout.write(html);
"""
    result = subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
    cards = {}
    for match in CARD_RE.finditer(result.stdout):
        label = html.unescape(match.group("label").strip())
        cards[label] = {
            "status": html.unescape(match.group("status").strip()),
            "tone": match.group("tone"),
            "hint": html.unescape(match.group("hint").strip()),
        }
    return cards


def _assert_card(cards: dict[str, dict[str, str]], label: str, *, status: str, tone: str, hint: str) -> None:
    assert label in cards
    card = cards[label]
    assert card["status"] == status
    assert card["tone"] == tone
    assert hint in card["hint"]


def test_render_admin_runtime_cards_includes_all_runtime_boxes() -> None:
    cards = _render_cards()
    assert set(cards) == {
        "Process role",
        "Database",
        "Storage",
        "Redis",
        "Sessions",
        "Rate limits",
        "Task queue",
        "Worker",
        "Scheduler",
        "Bootstrap admin",
        "Public origin mode",
        "Proxy trust",
    }


@pytest.mark.parametrize(
    ("updates", "status", "tone", "hint"),
    [
        ({}, "app", "ok", "App=true"),
        ({"process_role": "worker"}, "worker", "ok", "Worker=false"),
        ({"process_role": "scheduler"}, "scheduler", "neutral", "Scheduler=false"),
        ({"process_role": None}, "unknown", "ok", "App=true"),
    ],
)
def test_render_admin_runtime_cards_process_role_states(updates: dict, status: str, tone: str, hint: str) -> None:
    _assert_card(_render_cards(updates), "Process role", status=status, tone=tone, hint=hint)


@pytest.mark.parametrize(
    ("label", "updates", "status", "tone", "hint"),
    [
        ("Database", {}, "Healthy", "ok", "database health"),
        ("Database", {"database": {"ok": False}}, "Unavailable", "warn", "database health"),
        ("Storage", {}, "Healthy", "ok", "storage connection status"),
        ("Storage", {"storage": {"ok": False}}, "Unavailable", "warn", "storage connection status"),
    ],
)
def test_render_admin_runtime_cards_health_boxes_states(
    label: str, updates: dict, status: str, tone: str, hint: str
) -> None:
    _assert_card(_render_cards(updates), label, status=status, tone=tone, hint=hint)


@pytest.mark.parametrize(
    ("updates", "status", "tone", "hint"),
    [
        ({}, "Reachable", "ok", "Sessions redis"),
        ({"redis": {"reachable": False}}, "Configured, not reachable", "warn", "Sessions redis"),
        ({"redis": {"configured": False, "reachable": False}}, "Disabled", "neutral", "Sessions redis"),
    ],
)
def test_render_admin_runtime_cards_redis_states(updates: dict, status: str, tone: str, hint: str) -> None:
    _assert_card(_render_cards(updates), "Redis", status=status, tone=tone, hint=hint)


@pytest.mark.parametrize(
    ("updates", "status", "tone", "hint"),
    [
        ({}, "Redis-backed", "ok", "require Redis"),
        (
            {"redis": {"subsystems": {"sessions": {"effective_mode": "fallback"}}}},
            "Fail-closed",
            "warn",
            "require Redis",
        ),
        (
            {
                "redis": {
                    "session_fail_closed": False,
                    "subsystems": {"sessions": {"effective_mode": "fallback"}},
                }
            },
            "Signed-cookie fallback",
            "neutral",
            "signed-cookie validation",
        ),
    ],
)
def test_render_admin_runtime_cards_session_states(updates: dict, status: str, tone: str, hint: str) -> None:
    _assert_card(_render_cards(updates), "Sessions", status=status, tone=tone, hint=hint)


@pytest.mark.parametrize(
    ("updates", "status", "tone", "hint"),
    [
        ({}, "Redis-backed", "ok", "Shared counters survive"),
        (
            {"redis": {"subsystems": {"rate_limits": {"effective_mode": "memory"}}}},
            "In-memory fallback",
            "warn",
            "process-local",
        ),
        (
            {"redis": {"subsystems": {"rate_limits": {"effective_mode": "fallback"}}}},
            "In-memory fallback",
            "warn",
            "process-local",
        ),
        (
            {"redis": {"subsystems": {"rate_limits": {"effective_mode": "disabled"}}}},
            "Disabled",
            "neutral",
            "not backed by Redis",
        ),
        (
            {"redis": {"subsystems": {"rate_limits": {"effective_mode": None, "mode": None}}}},
            "Unknown",
            "neutral",
            "not backed by Redis",
        ),
    ],
)
def test_render_admin_runtime_cards_rate_limit_states(updates: dict, status: str, tone: str, hint: str) -> None:
    _assert_card(_render_cards(updates), "Rate limits", status=status, tone=tone, hint=hint)


@pytest.mark.parametrize(
    ("updates", "status", "tone", "hint"),
    [
        ({}, "redis", "ok", "Depth 0"),
        (
            {"tasks": {"mode": "async", "queue_depth": 2, "queues": {"default": 2}}},
            "async",
            "warn",
            "default: 2",
        ),
        ({"tasks": {"mode": None, "queue_depth": 0, "queues": {}}}, "Unknown", "ok", "Depth 0"),
    ],
)
def test_render_admin_runtime_cards_task_queue_states(updates: dict, status: str, tone: str, hint: str) -> None:
    _assert_card(_render_cards(updates), "Task queue", status=status, tone=tone, hint=hint)


@pytest.mark.parametrize(
    ("updates", "status", "tone", "hint"),
    [
        (
            {"services": {"worker": {"enabled_in_this_process": True, "last_task_failure": None}}},
            "Worker role active",
            "ok",
            "Last started",
        ),
        (
            {"services": {"worker": {"enabled_in_this_process": True, "last_task_failure": {"reason": "boom"}}}},
            "Worker role active",
            "warn",
            "Last failure",
        ),
        ({}, "Separate worker service", "ok", "Queues none"),
        (
            {"services": {"worker": {"last_task_failure": {"reason": "boom"}}}},
            "Separate worker service",
            "warn",
            "Last failure",
        ),
        (
            {"redis": {"configured": False}, "tasks": {"mode": "async"}},
            "In-process tasks",
            "warn",
            "tasks run in-process",
        ),
        (
            {"redis": {"configured": False}, "tasks": {"mode": "disabled"}},
            "Disabled",
            "neutral",
            "Last started",
        ),
    ],
)
def test_render_admin_runtime_cards_worker_states(updates: dict, status: str, tone: str, hint: str) -> None:
    _assert_card(_render_cards(updates), "Worker", status=status, tone=tone, hint=hint)


@pytest.mark.parametrize(
    ("updates", "status", "tone", "hint"),
    [
        (
            {
                "services": {
                    "scheduler": {
                        "enabled_in_this_process": True,
                        "configured": True,
                        "hosted_by": "app",
                        "last_enqueue_error": None,
                    }
                }
            },
            "Same service",
            "ok",
            "runs the scheduler loop",
        ),
        (
            {
                "services": {
                    "scheduler": {
                        "enabled_in_this_process": True,
                        "configured": True,
                        "hosted_by": "app",
                        "last_enqueue_error": {"reason": "boom"},
                    }
                }
            },
            "Same service",
            "warn",
            "runs the scheduler loop",
        ),
        (
            {
                "services": {
                    "scheduler": {
                        "enabled_in_this_process": False,
                        "configured": True,
                        "hosted_by": "scheduler",
                    }
                }
            },
            "Separate service",
            "ok",
            "separate scheduler service",
        ),
        (
            {"services": {"scheduler": {"enabled_in_this_process": False, "configured": False, "hosted_by": None}}},
            "Disabled",
            "neutral",
            "disabled overall",
        ),
    ],
)
def test_render_admin_runtime_cards_scheduler_states(updates: dict, status: str, tone: str, hint: str) -> None:
    _assert_card(_render_cards(updates), "Scheduler", status=status, tone=tone, hint=hint)


@pytest.mark.parametrize(
    ("updates", "status", "tone", "hint"),
    [
        ({}, "Disabled", "neutral", "No bootstrap admin"),
        (
            {"bootstrap_admin": {"enabled": True, "configured_username": "admin", "promoted": True}},
            "Promoted",
            "ok",
            "Configured username admin",
        ),
        (
            {"bootstrap_admin": {"enabled": True, "configured_username": "admin", "already_admin": True}},
            "Already admin",
            "ok",
            "Configured username admin",
        ),
        (
            {"bootstrap_admin": {"enabled": True, "configured_username": "admin", "matched": True}},
            "Matched user",
            "ok",
            "Configured username admin",
        ),
        (
            {"bootstrap_admin": {"enabled": True, "configured_username": "admin"}},
            "Configured",
            "ok",
            "Configured username admin",
        ),
        (
            {"bootstrap_admin": {"enabled": True, "configured_username": "admin", "warning": "duplicate usernames"}},
            "Configured",
            "warn",
            "duplicate usernames",
        ),
    ],
)
def test_render_admin_runtime_cards_bootstrap_admin_states(updates: dict, status: str, tone: str, hint: str) -> None:
    _assert_card(_render_cards(updates), "Bootstrap admin", status=status, tone=tone, hint=hint)


@pytest.mark.parametrize(
    ("updates", "status", "tone", "hint"),
    [
        ({}, "strict", "ok", "Strict mode"),
        (
            {"public_origin_enabled": False, "public_origin_mode": "direct_request"},
            "direct_request",
            "neutral",
            "Direct-request mode",
        ),
        (
            {"public_origin_enabled": False, "public_origin_mode": None},
            "Unknown",
            "neutral",
            "Direct-request mode",
        ),
    ],
)
def test_render_admin_runtime_cards_public_origin_states(updates: dict, status: str, tone: str, hint: str) -> None:
    _assert_card(_render_cards(updates), "Public origin mode", status=status, tone=tone, hint=hint)


@pytest.mark.parametrize(
    ("updates", "status", "tone", "hint"),
    [
        ({}, "Trusted proxies only", "ok", "trusted CIDR"),
        (
            {"trusted_proxy_cidrs_enabled": False, "forwarded_headers_policy": "permissive"},
            "Permissive local mode",
            "warn",
            "accepted from any client",
        ),
        (
            {"trusted_proxy_cidrs_enabled": False, "forwarded_headers_policy": None},
            "Unknown",
            "warn",
            "accepted from any client",
        ),
    ],
)
def test_render_admin_runtime_cards_proxy_trust_states(updates: dict, status: str, tone: str, hint: str) -> None:
    _assert_card(_render_cards(updates), "Proxy trust", status=status, tone=tone, hint=hint)
