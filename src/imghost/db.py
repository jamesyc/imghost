from __future__ import annotations

import json

import asyncpg


class Database:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if self.pool is not None:
            return

        async def init_connection(connection: asyncpg.Connection) -> None:
            await connection.set_type_codec(
                "json",
                encoder=json.dumps,
                decoder=json.loads,
                schema="pg_catalog",
            )
            await connection.set_type_codec(
                "jsonb",
                encoder=json.dumps,
                decoder=json.loads,
                schema="pg_catalog",
                format="text",
            )

        self.pool = await asyncpg.create_pool(
            dsn=self.dsn,
            min_size=1,
            max_size=10,
            init=init_connection,
        )

    async def close(self) -> None:
        if self.pool is None:
            return
        await self.pool.close()
        self.pool = None

    def require_pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("Database pool is not initialized.")
        return self.pool
