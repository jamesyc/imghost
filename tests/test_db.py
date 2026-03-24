import asyncio
import json

from imghost.db import Database


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
