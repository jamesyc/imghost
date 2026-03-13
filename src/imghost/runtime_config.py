from __future__ import annotations

import json
import os
from asyncio import Lock
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException

from .models import utcnow

ConfigType = Literal["bool", "int"]


@dataclass(frozen=True)
class RuntimeConfigSpec:
    key: str
    value_type: ConfigType
    default_provider: Callable[[], bool | int]
    lock_env: str

    def default(self) -> bool | int:
        return self.default_provider()


RUNTIME_CONFIG_SPECS: dict[str, RuntimeConfigSpec] = {
    "allow_registration": RuntimeConfigSpec("allow_registration", "bool", lambda: True, "LOCK_ALLOW_REGISTRATION"),
    "anon_upload_enabled": RuntimeConfigSpec("anon_upload_enabled", "bool", lambda: True, "LOCK_ANON_UPLOAD"),
    "anon_expiry_hours": RuntimeConfigSpec(
        "anon_expiry_hours",
        "int",
        lambda: int(os.getenv("ANON_EXPIRY_HOURS", "24")),
        "LOCK_ANON_EXPIRY",
    ),
    "rate_limit_anon_rpm": RuntimeConfigSpec("rate_limit_anon_rpm", "int", lambda: 5, "LOCK_RATE_LIMITS"),
    "rate_limit_anon_bph": RuntimeConfigSpec("rate_limit_anon_bph", "int", lambda: 104857600, "LOCK_RATE_LIMITS"),
    "rate_limit_global_anon_rpm": RuntimeConfigSpec("rate_limit_global_anon_rpm", "int", lambda: 50, "LOCK_RATE_LIMITS"),
    "rate_limit_global_anon_bph": RuntimeConfigSpec("rate_limit_global_anon_bph", "int", lambda: 1073741824, "LOCK_RATE_LIMITS"),
    "rate_limit_user_rpm": RuntimeConfigSpec("rate_limit_user_rpm", "int", lambda: 30, "LOCK_RATE_LIMITS"),
    "rate_limit_user_bph": RuntimeConfigSpec("rate_limit_user_bph", "int", lambda: 524288000, "LOCK_RATE_LIMITS"),
}


@dataclass(frozen=True)
class RuntimeConfigValue:
    key: str
    value: bool | int
    default: bool | int
    locked: bool
    source: str
    stored_value: bool | int | None
    updated_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "default": self.default,
            "locked": self.locked,
            "source": self.source,
            "stored_value": self.stored_value,
            "updated_at": self.updated_at,
        }


class JsonRuntimeConfig:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("{}", encoding="utf-8")

    def _load(self) -> dict[str, dict[str, Any]]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, payload: dict[str, dict[str, Any]]) -> None:
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def _is_locked(self, spec: RuntimeConfigSpec) -> bool:
        return os.getenv(spec.lock_env, "false").strip().lower() == "true"

    def _coerce_value(self, spec: RuntimeConfigSpec, raw_value: Any) -> bool | int:
        if spec.value_type == "bool":
            if not isinstance(raw_value, bool):
                raise HTTPException(status_code=400, detail=f"{spec.key} must be a boolean.")
            return raw_value
        if isinstance(raw_value, bool) or not isinstance(raw_value, int):
            raise HTTPException(status_code=400, detail=f"{spec.key} must be an integer.")
        if raw_value < 0:
            raise HTTPException(status_code=400, detail=f"{spec.key} must be non-negative.")
        return raw_value

    def _resolve_value(self, spec: RuntimeConfigSpec, state: dict[str, dict[str, Any]]) -> RuntimeConfigValue:
        default = spec.default()
        stored = state.get(spec.key)
        stored_value = stored.get("value") if stored else None
        updated_at = stored.get("updated_at") if stored else None
        if self._is_locked(spec):
            return RuntimeConfigValue(
                key=spec.key,
                value=default,
                default=default,
                locked=True,
                source="locked",
                stored_value=stored_value,
                updated_at=updated_at,
            )
        if stored_value is None:
            return RuntimeConfigValue(
                key=spec.key,
                value=default,
                default=default,
                locked=False,
                source="default",
                stored_value=None,
                updated_at=updated_at,
            )
        return RuntimeConfigValue(
            key=spec.key,
            value=self._coerce_value(spec, stored_value),
            default=default,
            locked=False,
            source="runtime",
            stored_value=stored_value,
            updated_at=updated_at,
        )

    async def list_effective(self) -> dict[str, RuntimeConfigValue]:
        async with self._lock:
            state = self._load()
        return {key: self._resolve_value(spec, state) for key, spec in RUNTIME_CONFIG_SPECS.items()}

    async def get_value(self, key: str) -> bool | int:
        if key not in RUNTIME_CONFIG_SPECS:
            raise KeyError(key)
        return (await self.list_effective())[key].value

    async def update_values(self, updates: dict[str, Any]) -> list[dict[str, Any]]:
        if not updates:
            raise HTTPException(status_code=400, detail="At least one config value is required.")
        unknown_keys = [key for key in updates if key not in RUNTIME_CONFIG_SPECS]
        if unknown_keys:
            raise HTTPException(status_code=400, detail=f"Unknown config key(s): {', '.join(sorted(unknown_keys))}.")

        async with self._lock:
            state = self._load()
            changes: list[dict[str, Any]] = []
            now = utcnow().isoformat()
            for key, raw_value in updates.items():
                spec = RUNTIME_CONFIG_SPECS[key]
                if self._is_locked(spec):
                    raise HTTPException(status_code=403, detail=f"{key} is locked by environment configuration.")
                coerced = self._coerce_value(spec, raw_value)
                current = self._resolve_value(spec, state)
                if current.value == coerced and current.source == "runtime":
                    continue
                state[key] = {"value": coerced, "updated_at": now}
                changes.append(
                    {
                        "key": key,
                        "old_value": current.value,
                        "new_value": coerced,
                    }
                )
            if changes:
                self._save(state)
        return changes
