from __future__ import annotations

import json
from pathlib import Path

import asyncpg


def _startup_sql_paths() -> tuple[Path, ...]:
    candidates = (
        Path.cwd() / "db" / "init" / "001-init.sql",
        Path("/app/db/init/001-init.sql"),
        Path(__file__).resolve().parents[2] / "db" / "init" / "001-init.sql",
    )
    paths: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen or not resolved.exists():
            continue
        seen.add(resolved)
        paths.append(resolved)
    return tuple(paths)


class Database:
    def __init__(self, dsn: str, *, use_pgbouncer: bool = False) -> None:
        self.dsn = dsn
        self.use_pgbouncer = use_pgbouncer
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

        connect_kwargs: dict[str, object] = {}
        if self.use_pgbouncer:
            # Disable asyncpg's prepared-statement cache for PgBouncer transaction pooling.
            connect_kwargs["statement_cache_size"] = 0

        self.pool = await asyncpg.create_pool(dsn=self.dsn, min_size=1, max_size=10, init=init_connection, **connect_kwargs)
        await self._apply_startup_sql()

    async def close(self) -> None:
        if self.pool is None:
            return
        await self.pool.close()
        self.pool = None

    def require_pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("Database pool is not initialized.")
        return self.pool

    async def _apply_startup_sql(self) -> None:
        sql_paths = _startup_sql_paths()
        if not sql_paths:
            return
        pool = self.require_pool()
        async with pool.acquire() as conn:
            await conn.execute("SELECT pg_advisory_lock($1)", 184537478640522980)
            try:
                for path in sql_paths:
                    await conn.execute(path.read_text(encoding="utf-8"))
            finally:
                await conn.execute("SELECT pg_advisory_unlock($1)", 184537478640522980)
