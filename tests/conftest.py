from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest

INIT_SQL = (Path(__file__).resolve().parent.parent / "db" / "init" / "001-init.sql").read_text(encoding="utf-8")


def _database_url() -> str:
    return os.getenv("DATABASE_URL", "postgresql://imghost:imghost@localhost:5432/imghost")


def _database_name(dsn: str) -> str:
    parsed = urlparse(dsn)
    return parsed.path.lstrip("/") or "postgres"


def _admin_database_url(dsn: str) -> str:
    parsed = urlparse(dsn)
    return urlunparse(parsed._replace(path="/postgres"))


async def _ensure_database_exists(dsn: str) -> None:
    try:
        conn = await asyncpg.connect(dsn)
    except asyncpg.InvalidCatalogNameError:
        admin = await asyncpg.connect(_admin_database_url(dsn))
        try:
            database_name = _database_name(dsn).replace('"', '""')
            exists = await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", _database_name(dsn))
            if not exists:
                await admin.execute(f'CREATE DATABASE "{database_name}"')
        finally:
            await admin.close()
        conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(INIT_SQL)
    finally:
        await conn.close()


async def _truncate_database() -> None:
    dsn = _database_url()
    await _ensure_database_exists(dsn)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            """
            TRUNCATE TABLE
              audit_log,
              config,
              media,
              albums,
              api_keys,
              user_rate_limits,
              user_sso_links,
              users
            RESTART IDENTITY CASCADE
            """
        )
    finally:
        await conn.close()


@pytest.fixture(autouse=True)
def clean_database() -> None:
    asyncio.run(_truncate_database())
