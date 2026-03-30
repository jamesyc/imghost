import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import asyncpg
from imghost.db import Database
from imghost.models import Album, ApiKey, Media, ShareXDeleteCapability, User, utcnow
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
    monkeypatch.setattr("imghost.db._startup_sql_paths", lambda: ())

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
    monkeypatch.setattr("imghost.db._startup_sql_paths", lambda: ())

    async def run() -> None:
        database = Database("postgresql://example/db", use_pgbouncer=True)
        await database.connect()

    asyncio.run(run())

    assert recorded["statement_cache_size"] == 0


def test_database_connect_applies_startup_sql_when_present(monkeypatch, tmp_path: Path) -> None:
    applied: list[str] = []
    sql_path = tmp_path / "startup.sql"
    sql_path.write_text("SELECT 1;", encoding="utf-8")

    class DummyConnection:
        async def set_type_codec(self, name, encoder, decoder, schema, format="text"):
            return None

        async def execute(self, sql: str, *args) -> None:
            applied.append(sql)

    class DummyAcquire:
        def __init__(self, conn: DummyConnection) -> None:
            self.conn = conn

        async def __aenter__(self) -> DummyConnection:
            return self.conn

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    class DummyPool:
        def __init__(self, conn: DummyConnection) -> None:
            self.conn = conn

        def acquire(self) -> DummyAcquire:
            return DummyAcquire(self.conn)

        async def close(self) -> None:
            return None

    async def fake_create_pool(*, dsn, min_size, max_size, init, **kwargs):
        conn = DummyConnection()
        await init(conn)
        return DummyPool(conn)

    monkeypatch.setattr("imghost.db.asyncpg.create_pool", fake_create_pool)
    monkeypatch.setattr("imghost.db._startup_sql_paths", lambda: (sql_path,))

    async def run() -> None:
        database = Database("postgresql://example/db")
        await database.connect()

    asyncio.run(run())

    assert applied == [
        "SELECT pg_advisory_lock($1)",
        "SELECT 1;",
        "SELECT pg_advisory_unlock($1)",
    ]


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


def test_concurrent_media_inserts_assign_distinct_positions_per_album() -> None:
    async def run() -> list[int]:
        database = Database(os.environ["DATABASE_URL"])
        await database.connect()
        try:
            repository = PostgresRepository(database)
            now = utcnow()
            album = await repository.create_album(
                Album(
                    id="album-concurrent-positions",
                    title="Concurrent",
                    user_id=None,
                    cover_media_id=None,
                    delete_token="token",
                    created_at=now,
                    updated_at=now,
                    expires_at=None,
                )
            )

            async def insert_media(media_id: str) -> Media:
                return await repository.create_media_with_next_position(
                    Media(
                        id=media_id,
                        album_id=album.id,
                        user_id=None,
                        filename_orig=f"{media_id}.png",
                        media_type="image",
                        format="png",
                        mime_type="image/png",
                        storage_key=f"originals/anon/{media_id}.png",
                        thumb_key=None,
                        thumb_is_orig=False,
                        thumb_status="pending",
                        file_size=1,
                        thumb_size=None,
                        width=1,
                        height=1,
                        duration_secs=None,
                        is_animated=False,
                        codec_hint=None,
                        position=0,
                        created_at=utcnow(),
                    )
                )

            first, second = await asyncio.gather(
                insert_media("media-concurrent-1"),
                insert_media("media-concurrent-2"),
            )
            return sorted([first.position, second.position])
        finally:
            await database.close()

    assert asyncio.run(run()) == [1000, 2000]


def test_concurrent_api_key_upserts_leave_one_row_per_user() -> None:
    async def run() -> tuple[int, str | None]:
        database = Database(os.environ["DATABASE_URL"])
        await database.connect()
        try:
            repository = PostgresRepository(database)
            now = utcnow()
            user_id = str(uuid4())
            await repository.create_user(
                User(
                    id=user_id,
                    username=f"apikey-{uuid4().hex[:8]}",
                    email=f"apikey-{uuid4().hex[:8]}@example.com",
                    password_hash="hash",
                    is_admin=False,
                    suspended=False,
                    quota_bytes=None,
                    rate_limit_rpm=None,
                    rate_limit_bph=None,
                    created_at=now,
                    updated_at=now,
                )
            )

            hashes = [f"key-hash-{index}" for index in range(8)]

            async def upsert(key_hash: str) -> None:
                await repository.upsert_api_key(
                    ApiKey(
                        id=str(uuid4()),
                        user_id=user_id,
                        key_hash=key_hash,
                        created_at=utcnow(),
                        last_used_at=None,
                    )
                )

            await asyncio.gather(*(upsert(key_hash) for key_hash in hashes))

            pool = database.require_pool()
            async with pool.acquire() as conn:
                count = int(await conn.fetchval("SELECT COUNT(*) FROM api_keys WHERE user_id = $1::uuid", user_id))
                key_hash = await conn.fetchval("SELECT key_hash FROM api_keys WHERE user_id = $1::uuid", user_id)
            assert key_hash in hashes
            return count, key_hash
        finally:
            await database.close()

    count, key_hash = asyncio.run(run())

    assert count == 1
    assert key_hash is not None


def test_repository_update_album_missing_row_raises_lookup_error() -> None:
    async def run() -> None:
        database = Database(os.environ["DATABASE_URL"])
        await database.connect()
        try:
            repository = PostgresRepository(database)
            now = utcnow()
            missing = Album(
                id="missing-album",
                title="Missing",
                user_id=None,
                cover_media_id=None,
                delete_token="token",
                created_at=now,
                updated_at=now,
                expires_at=None,
            )
            try:
                await repository.update_album(missing)
            except LookupError as exc:
                assert str(exc) == "album_not_found"
            else:
                raise AssertionError("expected LookupError")
        finally:
            await database.close()

    asyncio.run(run())


def test_repository_update_media_missing_row_raises_lookup_error() -> None:
    async def run() -> None:
        database = Database(os.environ["DATABASE_URL"])
        await database.connect()
        try:
            repository = PostgresRepository(database)
            now = utcnow()
            missing = Media(
                id="missing-media",
                album_id="missing-album",
                user_id=None,
                filename_orig="missing.png",
                media_type="image",
                format="png",
                mime_type="image/png",
                storage_key="originals/anon/missing.png",
                thumb_key=None,
                thumb_is_orig=False,
                thumb_status="pending",
                file_size=1,
                thumb_size=None,
                width=1,
                height=1,
                duration_secs=None,
                is_animated=False,
                codec_hint=None,
                position=1000,
                created_at=now,
            )
            try:
                await repository.update_media(missing)
            except LookupError as exc:
                assert str(exc) == "media_not_found"
            else:
                raise AssertionError("expected LookupError")
        finally:
            await database.close()

    asyncio.run(run())


def test_concurrent_sharex_capability_consumption_is_single_use() -> None:
    async def run() -> list[bool]:
        database = Database(os.environ["DATABASE_URL"])
        await database.connect()
        try:
            repository = PostgresRepository(database)
            now = utcnow()
            await repository.create_user(
                User(
                    id="11111111-1111-1111-1111-111111111111",
                    username="sharexrace",
                    email="sharexrace@example.com",
                    password_hash=None,
                    is_admin=False,
                    suspended=False,
                    quota_bytes=None,
                    rate_limit_rpm=None,
                    rate_limit_bph=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            await repository.create_album(
                Album(
                    id="album-sharex-race",
                    title="ShareX Race",
                    user_id="11111111-1111-1111-1111-111111111111",
                    cover_media_id=None,
                    delete_token=None,
                    created_at=now,
                    updated_at=now,
                    expires_at=None,
                )
            )
            await repository.create_sharex_delete_capability(
                ShareXDeleteCapability(
                    selector="selector-race",
                    purpose="sharex_delete_album",
                    album_id="album-sharex-race",
                    user_id="11111111-1111-1111-1111-111111111111",
                    secret_hash="hash",
                    created_at=now,
                    expires_at=now + timedelta(days=1),
                    consumed_at=None,
                    revoked_at=None,
                    last_seen_at=None,
                )
            )

            first, second = await asyncio.gather(
                repository.consume_sharex_delete_capability("selector-race", "album-sharex-race"),
                repository.consume_sharex_delete_capability("selector-race", "album-sharex-race"),
            )
            return [first is not None, second is not None]
        finally:
            await database.close()

    assert sorted(asyncio.run(run())) == [False, True]


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
