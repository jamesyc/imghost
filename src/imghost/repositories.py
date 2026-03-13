from __future__ import annotations

from datetime import datetime

from .db import Database
from .models import Album, ApiKey, Media, User


def _row_to_user(row) -> User:
    return User(
        id=str(row["id"]),
        username=row["username"],
        email=row["email"],
        password_hash=row["password_hash"],
        is_admin=row["is_admin"],
        suspended=row["is_suspended"],
        quota_bytes=row["quota_bytes"],
        rate_limit_rpm=row["rate_limit_rpm"],
        rate_limit_bph=row["rate_limit_bph"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_api_key(row) -> ApiKey:
    return ApiKey(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        key_hash=row["key_hash"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
    )


def _row_to_album(row) -> Album:
    return Album(
        id=row["id"],
        title=row["title"],
        user_id=str(row["user_id"]) if row["user_id"] is not None else None,
        cover_media_id=row["cover_media_id"],
        delete_token=row["delete_token"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        expires_at=row["expires_at"],
    )


def _row_to_media(row) -> Media:
    return Media(
        id=row["id"],
        album_id=row["album_id"],
        user_id=str(row["user_id"]) if row["user_id"] is not None else None,
        filename_orig=row["filename_orig"],
        media_type=row["media_type"],
        format=row["format"],
        mime_type=row["mime_type"],
        storage_key=row["storage_key"],
        thumb_key=row["thumb_key"],
        thumb_is_orig=row["thumb_is_orig"],
        thumb_status=row["thumb_status"],
        file_size=row["file_size"],
        thumb_size=row["thumb_size"],
        width=row["width"],
        height=row["height"],
        duration_secs=row["duration_secs"],
        is_animated=row["is_animated"],
        codec_hint=row["codec_hint"],
        position=row["position"],
        created_at=row["created_at"],
    )


USER_SELECT = """
SELECT
  users.id,
  users.username,
  users.email,
  users.password_hash,
  users.is_admin,
  users.is_suspended,
  users.quota_bytes,
  users.created_at,
  users.updated_at,
  user_rate_limits.rpm AS rate_limit_rpm,
  user_rate_limits.bph AS rate_limit_bph
FROM users
LEFT JOIN user_rate_limits ON user_rate_limits.user_id = users.id
"""


class PostgresRepository:
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
        return _row_to_user(row)

    async def get_user(self, user_id: str) -> User | None:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(f"{USER_SELECT} WHERE users.id = $1::uuid", user_id)
        return _row_to_user(row) if row else None

    async def get_user_by_email(self, email: str) -> User | None:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(f"{USER_SELECT} WHERE users.email = $1", email)
        return _row_to_user(row) if row else None

    async def get_user_by_username(self, username: str) -> User | None:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(f"{USER_SELECT} WHERE users.username = $1", username)
        return _row_to_user(row) if row else None

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
        return _row_to_user(row)

    async def _set_rate_limit_overrides(
        self,
        conn,
        user_id: str,
        rpm: int | None,
        bph: int | None,
    ) -> None:
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
        return _row_to_api_key(row)

    async def get_api_key_by_hash(self, key_hash: str) -> ApiKey | None:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, user_id, key_hash, created_at, last_used_at FROM api_keys WHERE key_hash = $1",
                key_hash,
            )
        return _row_to_api_key(row) if row else None

    async def get_api_key_for_user(self, user_id: str) -> ApiKey | None:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, user_id, key_hash, created_at, last_used_at FROM api_keys WHERE user_id = $1::uuid",
                user_id,
            )
        return _row_to_api_key(row) if row else None

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
        return _row_to_api_key(row)

    async def list_user_media(self, user_id: str) -> list[Media]:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM media WHERE user_id = $1::uuid ORDER BY created_at",
                user_id,
            )
        return [_row_to_media(row) for row in rows]

    async def list_user_albums(self, user_id: str) -> list[Album]:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM albums WHERE user_id = $1::uuid ORDER BY created_at",
                user_id,
            )
        return [_row_to_album(row) for row in rows]

    async def list_users(self) -> list[User]:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(f"{USER_SELECT} ORDER BY users.created_at")
        return [_row_to_user(row) for row in rows]

    async def list_all_media(self) -> list[Media]:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM media ORDER BY created_at")
        return [_row_to_media(row) for row in rows]

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
        return _row_to_user(user_row), [_row_to_album(row) for row in album_rows], [_row_to_media(row) for row in media_rows]

    async def create_album(self, album: Album) -> Album:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO albums (id, user_id, title, cover_media_id, delete_token, created_at, updated_at, expires_at)
                VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, $8)
                RETURNING *
                """,
                album.id,
                album.user_id,
                album.title,
                album.cover_media_id,
                album.delete_token,
                album.created_at,
                album.updated_at,
                album.expires_at,
            )
        return _row_to_album(row)

    async def get_album(self, album_id: str) -> Album | None:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM albums WHERE id = $1", album_id)
        return _row_to_album(row) if row else None

    async def update_album(self, album: Album) -> Album:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE albums
                SET user_id = $2::uuid,
                    title = $3,
                    cover_media_id = $4,
                    delete_token = $5,
                    updated_at = $6,
                    expires_at = $7
                WHERE id = $1
                RETURNING *
                """,
                album.id,
                album.user_id,
                album.title,
                album.cover_media_id,
                album.delete_token,
                album.updated_at,
                album.expires_at,
            )
        return _row_to_album(row)

    async def create_media(self, media: Media) -> Media:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO media (
                  id, album_id, user_id, filename_orig, media_type, format, mime_type, storage_key,
                  thumb_key, thumb_is_orig, thumb_status, file_size, thumb_size, width, height,
                  duration_secs, is_animated, codec_hint, position, created_at
                ) VALUES (
                  $1, $2, $3::uuid, $4, $5, $6, $7, $8,
                  $9, $10, $11, $12, $13, $14, $15,
                  $16, $17, $18, $19, $20
                )
                RETURNING *
                """,
                media.id,
                media.album_id,
                media.user_id,
                media.filename_orig,
                media.media_type,
                media.format,
                media.mime_type,
                media.storage_key,
                media.thumb_key,
                media.thumb_is_orig,
                media.thumb_status,
                media.file_size,
                media.thumb_size,
                media.width,
                media.height,
                media.duration_secs,
                media.is_animated,
                media.codec_hint,
                media.position,
                media.created_at,
            )
        return _row_to_media(row)

    async def get_media(self, media_id: str) -> Media | None:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM media WHERE id = $1", media_id)
        return _row_to_media(row) if row else None

    async def update_media(self, media: Media) -> Media:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE media
                SET album_id = $2,
                    user_id = $3::uuid,
                    filename_orig = $4,
                    media_type = $5,
                    format = $6,
                    mime_type = $7,
                    storage_key = $8,
                    thumb_key = $9,
                    thumb_is_orig = $10,
                    thumb_status = $11,
                    file_size = $12,
                    thumb_size = $13,
                    width = $14,
                    height = $15,
                    duration_secs = $16,
                    is_animated = $17,
                    codec_hint = $18,
                    position = $19,
                    created_at = $20
                WHERE id = $1
                RETURNING *
                """,
                media.id,
                media.album_id,
                media.user_id,
                media.filename_orig,
                media.media_type,
                media.format,
                media.mime_type,
                media.storage_key,
                media.thumb_key,
                media.thumb_is_orig,
                media.thumb_status,
                media.file_size,
                media.thumb_size,
                media.width,
                media.height,
                media.duration_secs,
                media.is_animated,
                media.codec_hint,
                media.position,
                media.created_at,
            )
        return _row_to_media(row)

    async def list_media_by_thumb_status(self, *statuses: str) -> list[Media]:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM media WHERE thumb_status = ANY($1::text[]) ORDER BY created_at",
                list(statuses),
            )
        return [_row_to_media(row) for row in rows]

    async def find_pending_thumbnails(self) -> list[Media]:
        return await self.list_media_by_thumb_status("pending", "processing")

    async def find_failed_thumbnails(self) -> list[Media]:
        return await self.list_media_by_thumb_status("failed")

    async def list_expired_albums(self, now: datetime) -> list[Album]:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM albums WHERE expires_at IS NOT NULL AND expires_at <= $1 ORDER BY expires_at",
                now,
            )
        return [_row_to_album(row) for row in rows]

    async def list_albums(self) -> list[Album]:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM albums ORDER BY created_at")
        return [_row_to_album(row) for row in rows]

    async def list_album_media(self, album_id: str) -> list[Media]:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM media WHERE album_id = $1 ORDER BY position", album_id)
        return [_row_to_media(row) for row in rows]

    async def next_position(self, album_id: str) -> int:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT COALESCE(MAX(position) + 1000, 1000) FROM media WHERE album_id = $1",
                album_id,
            )

    async def delete_media(self, media_id: str) -> Media | None:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("DELETE FROM media WHERE id = $1 RETURNING *", media_id)
        return _row_to_media(row) if row else None

    async def delete_album(self, album_id: str) -> tuple[Album | None, list[Media]]:
        pool = self.database.require_pool()
        async with pool.acquire() as conn, conn.transaction():
            media_rows = await conn.fetch("SELECT * FROM media WHERE album_id = $1 ORDER BY position", album_id)
            album_row = await conn.fetchrow("DELETE FROM albums WHERE id = $1 RETURNING *", album_id)
            if album_row is None:
                return None, []
        return _row_to_album(album_row), [_row_to_media(row) for row in media_rows]

    async def update_media_positions(self, album_id: str, positions: dict[str, int]) -> list[Media]:
        pool = self.database.require_pool()
        async with pool.acquire() as conn, conn.transaction():
            for media_id, position in positions.items():
                await conn.execute(
                    "UPDATE media SET position = $3 WHERE id = $1 AND album_id = $2",
                    media_id,
                    album_id,
                    position,
                )
            rows = await conn.fetch("SELECT * FROM media WHERE album_id = $1 ORDER BY position", album_id)
        return [_row_to_media(row) for row in rows]
