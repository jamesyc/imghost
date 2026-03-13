from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import HTTPException

from .db import Database

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


class PostgresRuntimeConfig:
    def __init__(self, database: Database) -> None:
        self.database = database

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

    def _decode_stored_value(self, spec: RuntimeConfigSpec, raw_value: str | None) -> bool | int | None:
        if raw_value is None:
            return None
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=500, detail=f"Invalid stored config value for {spec.key}.") from exc
        return self._coerce_value(spec, parsed)

    def _resolve_value(self, spec: RuntimeConfigSpec, stored_value: str | None, updated_at: str | None) -> RuntimeConfigValue:
        default = spec.default()
        decoded = self._decode_stored_value(spec, stored_value)
        if self._is_locked(spec):
            return RuntimeConfigValue(
                key=spec.key,
                value=default,
                default=default,
                locked=True,
                source="locked",
                stored_value=decoded,
                updated_at=updated_at,
            )
        if decoded is None:
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
            value=decoded,
            default=default,
            locked=False,
            source="runtime",
            stored_value=decoded,
            updated_at=updated_at,
        )

    async def list_effective(self) -> dict[str, RuntimeConfigValue]:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT key, value, updated_at FROM config")
        state = {row["key"]: row for row in rows}
        return {
            key: self._resolve_value(
                spec,
                state.get(key)["value"] if key in state else None,
                state.get(key)["updated_at"].isoformat() if key in state else None,
            )
            for key, spec in RUNTIME_CONFIG_SPECS.items()
        }

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

        effective = await self.list_effective()
        changes: list[dict[str, Any]] = []
        pool = self.database.require_pool()
        async with pool.acquire() as conn, conn.transaction():
            for key, raw_value in updates.items():
                spec = RUNTIME_CONFIG_SPECS[key]
                if self._is_locked(spec):
                    raise HTTPException(status_code=403, detail=f"{key} is locked by environment configuration.")
                coerced = self._coerce_value(spec, raw_value)
                current = effective[key]
                if current.value == coerced and current.source == "runtime":
                    continue
                await conn.execute(
                    """
                    INSERT INTO config (key, value)
                    VALUES ($1, $2)
                    ON CONFLICT (key)
                    DO UPDATE SET value = EXCLUDED.value
                    """,
                    key,
                    json.dumps(coerced),
                )
                changes.append({"key": key, "old_value": current.value, "new_value": coerced})
        return changes
