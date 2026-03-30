from __future__ import annotations

import asyncio
import os
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest

INIT_SQL = (Path(__file__).resolve().parent.parent / "db" / "init" / "001-init.sql").read_text(encoding="utf-8")
DEFAULT_TEST_DATABASE_URL = "postgresql://imghost:imghost@localhost:5432/imghost_test"


def _database_url() -> str:
    configured = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL") or DEFAULT_TEST_DATABASE_URL
    _assert_safe_test_database_url(configured)
    return configured


def _database_name(dsn: str) -> str:
    parsed = urlparse(dsn)
    return parsed.path.lstrip("/") or "postgres"


def _admin_database_url(dsn: str) -> str:
    parsed = urlparse(dsn)
    return urlunparse(parsed._replace(path="/postgres"))


def _assert_safe_test_database_url(dsn: str) -> None:
    database_name = _database_name(dsn).lower()
    if "test" not in database_name:
        raise RuntimeError(
            "Refusing to run tests against a non-test database. "
            "Use TEST_DATABASE_URL or DATABASE_URL pointing to a dedicated test database such as 'imghost_test'."
        )


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
              sharex_delete_capabilities,
              media,
              albums,
              oauth_state_nonces,
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
    os.environ["DATABASE_URL"] = _database_url()
    os.environ.setdefault("SECRET_KEY", "test-secret")
    asyncio.run(_truncate_database())
