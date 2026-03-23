from __future__ import annotations

from typing import Any

from .db import Database
from .models import Album, ApiKey, Media, OAuthStateNonce, User, UserSsoLink
from .repository_mapping import USER_SELECT, row_to_api_key, row_to_album, row_to_media, row_to_user, row_to_user_sso_link


class UserRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def create_user(self, user: User) -> User:
        pool = self.database.require_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO users (
                  id, username, email, password_hash, is_admin, is_suspended, quota_bytes, created_at, updated_at
                ) VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                user.id,
                user.username,
                user.email,
                user.password_hash,
                user.is_admin,
                user.suspended,
                user.quota_bytes,
                user.created_at,
                user.updated_at,
            )
            await self._set_rate_limit_overrides(conn, user.id, user.rate_limit_rpm, user.rate_limit_bph)
            row = await conn.fetchrow(f"{USER_SELECT} WHERE users.id = $1::uuid", user.id)
        return row_to_user(row)

    async def get_user(self, user_id: str) -> User | None:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(f"{USER_SELECT} WHERE users.id = $1::uuid", user_id)
        return row_to_user(row) if row else None

    async def get_user_by_email(self, email: str) -> User | None:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(f"{USER_SELECT} WHERE users.email = $1", email)
        return row_to_user(row) if row else None

    async def get_user_by_username(self, username: str) -> User | None:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(f"{USER_SELECT} WHERE users.username = $1", username)
        return row_to_user(row) if row else None

    async def update_user(self, user: User) -> User:
        pool = self.database.require_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                UPDATE users
                SET username = $2,
                    email = $3,
                    password_hash = $4,
                    is_admin = $5,
                    is_suspended = $6,
                    quota_bytes = $7,
                    updated_at = $8
                WHERE id = $1::uuid
                """,
                user.id,
                user.username,
                user.email,
                user.password_hash,
                user.is_admin,
                user.suspended,
                user.quota_bytes,
                user.updated_at,
            )
            await self._set_rate_limit_overrides(conn, user.id, user.rate_limit_rpm, user.rate_limit_bph)
            row = await conn.fetchrow(f"{USER_SELECT} WHERE users.id = $1::uuid", user.id)
        return row_to_user(row)

    async def _set_rate_limit_overrides(self, conn, user_id: str, rpm: int | None, bph: int | None) -> None:
        if rpm is None and bph is None:
            await conn.execute("DELETE FROM user_rate_limits WHERE user_id = $1::uuid", user_id)
            return
        await conn.execute(
            """
            INSERT INTO user_rate_limits (user_id, rpm, bph)
            VALUES ($1::uuid, $2, $3)
            ON CONFLICT (user_id)
            DO UPDATE SET rpm = EXCLUDED.rpm, bph = EXCLUDED.bph
            """,
            user_id,
            rpm,
            bph,
        )

    async def upsert_api_key(self, api_key: ApiKey) -> ApiKey:
        pool = self.database.require_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM api_keys WHERE user_id = $1::uuid", api_key.user_id)
            row = await conn.fetchrow(
                """
                INSERT INTO api_keys (id, user_id, key_hash, created_at, last_used_at)
                VALUES ($1::uuid, $2::uuid, $3, $4, $5)
                RETURNING id, user_id, key_hash, created_at, last_used_at
                """,
                api_key.id,
                api_key.user_id,
                api_key.key_hash,
                api_key.created_at,
                api_key.last_used_at,
            )
        return row_to_api_key(row)

    async def get_api_key_by_hash(self, key_hash: str) -> ApiKey | None:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, user_id, key_hash, created_at, last_used_at FROM api_keys WHERE key_hash = $1",
                key_hash,
            )
        return row_to_api_key(row) if row else None

    async def get_api_key_for_user(self, user_id: str) -> ApiKey | None:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, user_id, key_hash, created_at, last_used_at FROM api_keys WHERE user_id = $1::uuid",
                user_id,
            )
        return row_to_api_key(row) if row else None

    async def update_api_key(self, api_key: ApiKey) -> ApiKey:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE api_keys
                SET user_id = $2::uuid, key_hash = $3, created_at = $4, last_used_at = $5
                WHERE id = $1::uuid
                RETURNING id, user_id, key_hash, created_at, last_used_at
                """,
                api_key.id,
                api_key.user_id,
                api_key.key_hash,
                api_key.created_at,
                api_key.last_used_at,
            )
        return row_to_api_key(row)

    async def create_user_sso_link(self, link: UserSsoLink) -> UserSsoLink:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO user_sso_links (id, user_id, provider, provider_uid, linked_at)
                VALUES ($1::uuid, $2::uuid, $3, $4, $5)
                RETURNING id, user_id, provider, provider_uid, linked_at
                """,
                link.id,
                link.user_id,
                link.provider,
                link.provider_uid,
                link.linked_at,
            )
        return row_to_user_sso_link(row)

    async def get_user_sso_link(self, provider: str, provider_uid: str) -> UserSsoLink | None:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, user_id, provider, provider_uid, linked_at
                FROM user_sso_links
                WHERE provider = $1 AND provider_uid = $2
                """,
                provider,
                provider_uid,
            )
        return row_to_user_sso_link(row) if row else None

    async def list_user_sso_links(self, user_id: str) -> list[UserSsoLink]:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, user_id, provider, provider_uid, linked_at
                FROM user_sso_links
                WHERE user_id = $1::uuid
                ORDER BY provider, linked_at
                """,
                user_id,
            )
        return [row_to_user_sso_link(row) for row in rows]

    async def delete_user_sso_link(self, user_id: str, provider: str) -> UserSsoLink | None:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                DELETE FROM user_sso_links
                WHERE user_id = $1::uuid AND provider = $2
                RETURNING id, user_id, provider, provider_uid, linked_at
                """,
                user_id,
                provider,
            )
        return row_to_user_sso_link(row) if row else None

    async def create_oauth_state_nonce(self, nonce: OAuthStateNonce) -> OAuthStateNonce:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO oauth_state_nonces (jti, mode, user_id, created_at, expires_at)
                VALUES ($1, $2, $3::uuid, $4, $5)
                RETURNING jti, mode, user_id, created_at, expires_at
                """,
                nonce.jti,
                nonce.mode,
                nonce.user_id,
                nonce.created_at,
                nonce.expires_at,
            )
        return self._row_to_oauth_state_nonce(row)

    async def consume_oauth_state_nonce(self, jti: str) -> OAuthStateNonce | None:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                DELETE FROM oauth_state_nonces
                WHERE jti = $1 AND expires_at > now()
                RETURNING jti, mode, user_id, created_at, expires_at
                """,
                jti,
            )
        return self._row_to_oauth_state_nonce(row) if row else None

    async def delete_expired_oauth_state_nonces(self) -> None:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM oauth_state_nonces WHERE expires_at <= now()")

    async def get_oauth_state_nonce(self, jti: str) -> OAuthStateNonce | None:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT jti, mode, user_id, created_at, expires_at
                FROM oauth_state_nonces
                WHERE jti = $1
                """,
                jti,
            )
        return self._row_to_oauth_state_nonce(row) if row else None

    def _row_to_oauth_state_nonce(self, row) -> OAuthStateNonce:
        return OAuthStateNonce(
            jti=row["jti"],
            mode=row["mode"],
            user_id=str(row["user_id"]) if row["user_id"] is not None else None,
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    async def list_user_media(self, user_id: str) -> list[Media]:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM media WHERE user_id = $1::uuid ORDER BY created_at",
                user_id,
            )
        return [row_to_media(row) for row in rows]

    async def list_user_albums(self, user_id: str) -> list[Album]:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM albums WHERE user_id = $1::uuid ORDER BY created_at",
                user_id,
            )
        return [row_to_album(row) for row in rows]

    async def list_user_albums_page(self, user_id: str, *, limit: int = 10, offset: int = 0) -> tuple[list[Album], int]:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM albums WHERE user_id = $1::uuid", user_id)
            rows = await conn.fetch(
                "SELECT * FROM albums WHERE user_id = $1::uuid ORDER BY updated_at DESC, id DESC LIMIT $2 OFFSET $3",
                user_id,
                limit,
                offset,
            )
        return [row_to_album(row) for row in rows], int(total or 0)

    async def list_users(self) -> list[User]:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(f"{USER_SELECT} ORDER BY users.created_at")
        return [row_to_user(row) for row in rows]

    async def list_users_filtered(
        self,
        *,
        q: str | None = None,
        is_admin: bool | None = None,
        suspended: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[User], int]:
        filters: list[str] = []
        params: list[Any] = []

        if q:
            params.append(f"%{q.lower()}%")
            placeholder = f"${len(params)}"
            filters.append(
                f"(LOWER(users.username) LIKE {placeholder} OR LOWER(users.email) LIKE {placeholder} OR users.id::text LIKE {placeholder})"
            )
        if is_admin is not None:
            params.append(is_admin)
            filters.append(f"users.is_admin = ${len(params)}")
        if suspended is not None:
            params.append(suspended)
            filters.append(f"users.is_suspended = ${len(params)}")

        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
        count_sql = f"SELECT COUNT(*) FROM users {where_clause}"
        params.extend([limit, offset])
        paged_sql = (
            f"{USER_SELECT} {where_clause} ORDER BY users.created_at DESC, users.id DESC "
            f"LIMIT ${len(params) - 1} OFFSET ${len(params)}"
        )

        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            total = await conn.fetchval(count_sql, *params[:-2])
            rows = await conn.fetch(paged_sql, *params)
        return [row_to_user(row) for row in rows], int(total or 0)

    async def summarize_users(self, user_ids: list[str]) -> dict[str, dict[str, int]]:
        if not user_ids:
            return {}
        pool = self.database.require_pool()
        summary: dict[str, dict[str, int]] = {
            user_id: {"album_count": 0, "media_count": 0, "storage_used_bytes": 0} for user_id in user_ids
        }
        async with pool.acquire() as conn:
            album_rows = await conn.fetch(
                """
                SELECT user_id::text AS user_id, COUNT(*)::int AS album_count
                FROM albums
                WHERE user_id = ANY($1::uuid[])
                GROUP BY user_id
                """,
                user_ids,
            )
            media_rows = await conn.fetch(
                """
                SELECT
                  user_id::text AS user_id,
                  COUNT(*)::int AS media_count,
                  COALESCE(
                    SUM(
                      file_size +
                      CASE
                        WHEN thumb_key IS NOT NULL AND thumb_key <> storage_key THEN COALESCE(thumb_size, 0)
                        ELSE 0
                      END
                    ),
                    0
                  )::bigint AS storage_used_bytes
                FROM media
                WHERE user_id = ANY($1::uuid[])
                GROUP BY user_id
                """,
                user_ids,
            )
        for row in album_rows:
            summary[row["user_id"]]["album_count"] = row["album_count"]
        for row in media_rows:
            summary[row["user_id"]]["media_count"] = row["media_count"]
            summary[row["user_id"]]["storage_used_bytes"] = int(row["storage_used_bytes"] or 0)
        return summary

    async def delete_user(self, user_id: str) -> tuple[User | None, list[Album], list[Media]]:
        pool = self.database.require_pool()
        async with pool.acquire() as conn, conn.transaction():
            user_row = await conn.fetchrow(f"{USER_SELECT} WHERE users.id = $1::uuid", user_id)
            if user_row is None:
                return None, [], []
            album_rows = await conn.fetch("SELECT * FROM albums WHERE user_id = $1::uuid ORDER BY created_at", user_id)
            media_rows = await conn.fetch(
                """
                SELECT DISTINCT media.* FROM media
                LEFT JOIN albums ON albums.id = media.album_id
                WHERE media.user_id = $1::uuid OR albums.user_id = $1::uuid
                ORDER BY media.created_at
                """,
                user_id,
            )
            await conn.execute("DELETE FROM users WHERE id = $1::uuid", user_id)
        return row_to_user(user_row), [row_to_album(row) for row in album_rows], [row_to_media(row) for row in media_rows]
