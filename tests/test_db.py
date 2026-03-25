import asyncio
import json
import os
from datetime import UTC, datetime

import asyncpg
from imghost.db import Database
from imghost.models import Album, User
from imghost.repositories import PostgresRepository


def test_database_connect_uses_default_asyncpg_settings_when_pgbouncer_disabled(monkeypatch) -> None:
    recorded: dict[str, object] = {}

    class DummyPool:
        async def close(self) -> None:
            return None

    async def fake_create_pool(*, dsn, min_size, max_size, init, **kwargs):
        recorded.update(
            {
                "dsn": dsn,
                "min_size": min_size,
                "max_size": max_size,
                "kwargs": kwargs,
            }
        )

        class DummyConnection:
            async def set_type_codec(self, name, encoder, decoder, schema, format="text"):
                if name == "json":
                    assert encoder({"ok": True}) == json.dumps({"ok": True})

        await init(DummyConnection())
        return DummyPool()

    monkeypatch.setattr("imghost.db.asyncpg.create_pool", fake_create_pool)

    async def run() -> None:
        database = Database("postgresql://example/db")
        await database.connect()

    asyncio.run(run())

    assert recorded["dsn"] == "postgresql://example/db"
    assert recorded["min_size"] == 1
    assert recorded["max_size"] == 10
    assert recorded["kwargs"] == {}


def test_database_connect_disables_statement_cache_when_pgbouncer_enabled(monkeypatch) -> None:
    recorded: dict[str, object] = {}

    class DummyPool:
        async def close(self) -> None:
            return None

    async def fake_create_pool(*, dsn, min_size, max_size, init, **kwargs):
        recorded.update(kwargs)

        class DummyConnection:
            async def set_type_codec(self, name, encoder, decoder, schema, format="text"):
                return None

        await init(DummyConnection())
        return DummyPool()

    monkeypatch.setattr("imghost.db.asyncpg.create_pool", fake_create_pool)

    async def run() -> None:
        database = Database("postgresql://example/db", use_pgbouncer=True)
        await database.connect()

    asyncio.run(run())

    assert recorded["statement_cache_size"] == 0


def test_init_sql_registers_updated_at_triggers_for_mutable_tables() -> None:
    async def run() -> list[tuple[str, str]]:
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            rows = await conn.fetch(
                """
                SELECT tgrelid::regclass::text AS table_name, tgname
                FROM pg_trigger
                WHERE NOT tgisinternal
                  AND tgname IN ('users_set_updated_at', 'albums_set_updated_at', 'config_set_updated_at')
                ORDER BY table_name
                """
            )
            return [(row["table_name"], row["tgname"]) for row in rows]
        finally:
            await conn.close()

    assert asyncio.run(run()) == [
        ("albums", "albums_set_updated_at"),
        ("config", "config_set_updated_at"),
        ("users", "users_set_updated_at"),
    ]


def test_users_updated_at_trigger_overrides_stale_manual_timestamp() -> None:
    stale = datetime(2000, 1, 1, tzinfo=UTC)

    async def run() -> tuple[datetime, datetime]:
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            await conn.execute(
                """
                INSERT INTO users (id, username, email, password_hash, is_admin, is_suspended, quota_bytes, created_at, updated_at)
                VALUES ($1::uuid, $2, $3, $4, false, false, NULL, $5, $5)
                """,
                "11111111-1111-1111-1111-111111111111",
                "triggeruser",
                "triggeruser@example.com",
                None,
                stale,
            )
            row = await conn.fetchrow(
                """
                UPDATE users
                SET quota_bytes = $2,
                    updated_at = $3
                WHERE id = $1::uuid
                RETURNING updated_at
                """,
                "11111111-1111-1111-1111-111111111111",
                2048,
                stale,
            )
            stored = await conn.fetchrow(
                "SELECT updated_at FROM users WHERE id = $1::uuid",
                "11111111-1111-1111-1111-111111111111",
            )
            return row["updated_at"], stored["updated_at"]
        finally:
            await conn.close()

    returned_updated_at, stored_updated_at = asyncio.run(run())

    assert returned_updated_at == stored_updated_at
    assert stored_updated_at > stale


def test_albums_updated_at_trigger_applies_to_no_op_updates() -> None:
    stale = datetime(2000, 1, 1, tzinfo=UTC)

    async def run() -> tuple[datetime, datetime]:
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            await conn.execute(
                """
                INSERT INTO albums (id, user_id, title, cover_media_id, delete_token, created_at, updated_at, expires_at)
                VALUES ($1, NULL, $2, NULL, $3, $4, $4, NULL)
                """,
                "album-trigger-noop",
                "Trigger Album",
                "delete-token",
                stale,
            )
            before = await conn.fetchval("SELECT updated_at FROM albums WHERE id = $1", "album-trigger-noop")
            row = await conn.fetchrow(
                """
                UPDATE albums
                SET title = $2,
                    updated_at = $3
                WHERE id = $1
                RETURNING updated_at
                """,
                "album-trigger-noop",
                "Trigger Album",
                stale,
            )
            return before, row["updated_at"]
        finally:
            await conn.close()

    before_updated_at, after_updated_at = asyncio.run(run())

    assert before_updated_at == stale
    assert after_updated_at > before_updated_at


def test_config_updated_at_trigger_overrides_manual_timestamp() -> None:
    stale = datetime(2000, 1, 1, tzinfo=UTC)

    async def run() -> datetime:
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            await conn.execute(
                """
                INSERT INTO config (key, value, updated_at, updated_by)
                VALUES ($1, $2, $3, NULL)
                """,
                "allow_registration",
                "true",
                stale,
            )
            row = await conn.fetchrow(
                """
                UPDATE config
                SET value = $2,
                    updated_at = $3
                WHERE key = $1
                RETURNING updated_at
                """,
                "allow_registration",
                "false",
                stale,
            )
            return row["updated_at"]
        finally:
            await conn.close()

    assert asyncio.run(run()) > stale


def test_users_updated_at_trigger_applies_to_bulk_updates() -> None:
    stale = datetime(2000, 1, 1, tzinfo=UTC)

    async def run() -> list[datetime]:
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            await conn.executemany(
                """
                INSERT INTO users (id, username, email, password_hash, is_admin, is_suspended, quota_bytes, created_at, updated_at)
                VALUES ($1::uuid, $2, $3, NULL, false, false, NULL, $4, $4)
                """,
                [
                    ("22222222-2222-2222-2222-222222222222", "bulkuser1", "bulkuser1@example.com", stale),
                    ("33333333-3333-3333-3333-333333333333", "bulkuser2", "bulkuser2@example.com", stale),
                ],
            )
            rows = await conn.fetch(
                """
                UPDATE users
                SET quota_bytes = 4096,
                    updated_at = $1
                WHERE username LIKE 'bulkuser%'
                RETURNING updated_at
                """,
                stale,
            )
            return [row["updated_at"] for row in rows]
        finally:
            await conn.close()

    updated_values = asyncio.run(run())

    assert len(updated_values) == 2
    assert all(value > stale for value in updated_values)


def test_repository_update_user_returns_db_managed_updated_at() -> None:
    stale = datetime(2000, 1, 1, tzinfo=UTC)

    async def run() -> tuple[datetime, datetime]:
        database = Database(os.environ["DATABASE_URL"])
        await database.connect()
        try:
            repository = PostgresRepository(database)
            created = await repository.create_user(
                User(
                    id="44444444-4444-4444-4444-444444444444",
                    username="repo-user",
                    email="repo-user@example.com",
                    password_hash=None,
                    is_admin=False,
                    suspended=False,
                    quota_bytes=None,
                    rate_limit_rpm=None,
                    rate_limit_bph=None,
                    created_at=stale,
                    updated_at=stale,
                )
            )
            created.quota_bytes = 8192
            created.updated_at = stale
            updated = await repository.update_user(created)
            return created.updated_at, updated.updated_at
        finally:
            await database.close()

    before_updated_at, after_updated_at = asyncio.run(run())

    assert before_updated_at == stale
    assert after_updated_at > before_updated_at


def test_repository_update_album_returns_db_managed_updated_at() -> None:
    stale = datetime(2000, 1, 1, tzinfo=UTC)

    async def run() -> tuple[datetime, datetime]:
        database = Database(os.environ["DATABASE_URL"])
        await database.connect()
        try:
            repository = PostgresRepository(database)
            created = await repository.create_album(
                Album(
                    id="repo-album",
                    title="Original",
                    user_id=None,
                    cover_media_id=None,
                    delete_token="delete-token",
                    created_at=stale,
                    updated_at=stale,
                    expires_at=None,
                )
            )
            created.title = "Updated"
            created.updated_at = stale
            updated = await repository.update_album(created)
            return created.updated_at, updated.updated_at
        finally:
            await database.close()

    before_updated_at, after_updated_at = asyncio.run(run())

    assert before_updated_at == stale
    assert after_updated_at > before_updated_at
