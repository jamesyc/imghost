from __future__ import annotations

from datetime import datetime
from typing import Any

from .db import Database
from .models import Album, Media, ShareXDeleteCapability
from .repository_mapping import row_to_album, row_to_media, row_to_sharex_delete_capability


class AlbumMediaRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def list_media_for_album_ids(self, album_ids: list[str]) -> dict[str, list[Media]]:
        grouped: dict[str, list[Media]] = {album_id: [] for album_id in album_ids}
        if not album_ids:
            return grouped
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM media WHERE album_id = ANY($1::text[]) ORDER BY album_id, position",
                album_ids,
            )
        for row in rows:
            media = row_to_media(row)
            grouped.setdefault(media.album_id, []).append(media)
        return grouped

    async def create_sharex_delete_capability(self, capability: ShareXDeleteCapability) -> ShareXDeleteCapability:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO sharex_delete_capabilities (
                  selector, purpose, album_id, user_id, secret_hash, created_at, expires_at, consumed_at, revoked_at, last_seen_at
                ) VALUES (
                  $1, $2, $3, $4::uuid, $5, $6, $7, $8, $9, $10
                )
                RETURNING *
                """,
                capability.selector,
                capability.purpose,
                capability.album_id,
                capability.user_id,
                capability.secret_hash,
                capability.created_at,
                capability.expires_at,
                capability.consumed_at,
                capability.revoked_at,
                capability.last_seen_at,
            )
        return row_to_sharex_delete_capability(row)

    async def get_sharex_delete_capability(self, selector: str) -> ShareXDeleteCapability | None:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM sharex_delete_capabilities
                WHERE selector = $1
                """,
                selector,
            )
        return row_to_sharex_delete_capability(row) if row else None

    async def touch_sharex_delete_capability(self, selector: str) -> ShareXDeleteCapability | None:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE sharex_delete_capabilities
                SET last_seen_at = now()
                WHERE selector = $1
                RETURNING *
                """,
                selector,
            )
        return row_to_sharex_delete_capability(row) if row else None

    async def consume_sharex_delete_capability(self, selector: str, album_id: str) -> ShareXDeleteCapability | None:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE sharex_delete_capabilities
                SET consumed_at = now(), last_seen_at = now()
                WHERE selector = $1
                  AND album_id = $2
                  AND revoked_at IS NULL
                  AND consumed_at IS NULL
                  AND expires_at > now()
                RETURNING *
                """,
                selector,
                album_id,
            )
        return row_to_sharex_delete_capability(row) if row else None

    async def revoke_sharex_delete_capabilities_for_album(self, album_id: str) -> int:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            status = await conn.execute(
                """
                UPDATE sharex_delete_capabilities
                SET revoked_at = COALESCE(revoked_at, now()), last_seen_at = now()
                WHERE album_id = $1
                  AND consumed_at IS NULL
                  AND revoked_at IS NULL
                """,
                album_id,
            )
        return int(status.rsplit(" ", 1)[-1])

    async def revoke_sharex_delete_capabilities_for_user(self, user_id: str) -> int:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            status = await conn.execute(
                """
                UPDATE sharex_delete_capabilities
                SET revoked_at = COALESCE(revoked_at, now()), last_seen_at = now()
                WHERE user_id = $1::uuid
                  AND consumed_at IS NULL
                  AND revoked_at IS NULL
                """,
                user_id,
            )
        return int(status.rsplit(" ", 1)[-1])

    async def delete_expired_sharex_delete_capabilities(self, now: datetime) -> int:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM sharex_delete_capabilities WHERE expires_at <= $1",
                now,
            )
        return int(status.rsplit(" ", 1)[-1])

    async def delete_consumed_sharex_delete_capabilities_older_than(self, cutoff: datetime) -> int:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            status = await conn.execute(
                """
                DELETE FROM sharex_delete_capabilities
                WHERE consumed_at IS NOT NULL
                  AND consumed_at <= $1
                """,
                cutoff,
            )
        return int(status.rsplit(" ", 1)[-1])

    async def delete_revoked_sharex_delete_capabilities_older_than(self, cutoff: datetime) -> int:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            status = await conn.execute(
                """
                DELETE FROM sharex_delete_capabilities
                WHERE revoked_at IS NOT NULL
                  AND revoked_at <= $1
                """,
                cutoff,
            )
        return int(status.rsplit(" ", 1)[-1])

    async def list_all_media(self) -> list[Media]:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM media ORDER BY created_at")
        return [row_to_media(row) for row in rows]

    async def list_albums_page(
        self,
        *,
        q: str | None = None,
        owner: str | None = None,
        anonymous: bool | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> tuple[list[Album], int]:
        filters: list[str] = []
        params: list[Any] = []

        if q:
            params.append(f"%{q.lower()}%")
            placeholder = f"${len(params)}"
            filters.append(f"(LOWER(albums.title) LIKE {placeholder} OR albums.id LIKE {placeholder})")
        if owner:
            params.append(f"%{owner.lower()}%")
            placeholder = f"${len(params)}"
            filters.append(
                f"""albums.user_id IN (
                    SELECT id FROM users
                    WHERE LOWER(username) LIKE {placeholder} OR LOWER(email) LIKE {placeholder}
                )"""
            )
        if anonymous is True:
            filters.append("albums.user_id IS NULL")
        elif anonymous is False:
            filters.append("albums.user_id IS NOT NULL")

        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
        count_sql = f"SELECT COUNT(*) FROM albums {where_clause}"
        params.extend([limit, offset])
        query_sql = (
            f"SELECT * FROM albums {where_clause} ORDER BY updated_at DESC, id DESC "
            f"LIMIT ${len(params) - 1} OFFSET ${len(params)}"
        )

        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            total = await conn.fetchval(count_sql, *params[:-2])
            rows = await conn.fetch(query_sql, *params)
        return [row_to_album(row) for row in rows], int(total or 0)

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
        return row_to_album(row)

    async def get_album(self, album_id: str) -> Album | None:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM albums WHERE id = $1", album_id)
        return row_to_album(row) if row else None

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
                    expires_at = $6
                WHERE id = $1
                RETURNING *
                """,
                album.id,
                album.user_id,
                album.title,
                album.cover_media_id,
                album.delete_token,
                album.expires_at,
            )
        if row is None:
            raise LookupError("album_not_found")
        return row_to_album(row)

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
        return row_to_media(row)

    async def create_media_with_next_position(self, media: Media) -> Media:
        pool = self.database.require_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.fetchrow("SELECT id FROM albums WHERE id = $1 FOR UPDATE", media.album_id)
            position = await conn.fetchval(
                "SELECT COALESCE(MAX(position) + 1000, 1000) FROM media WHERE album_id = $1",
                media.album_id,
            )
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
                int(position),
                media.created_at,
            )
        return row_to_media(row)

    async def get_media(self, media_id: str) -> Media | None:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM media WHERE id = $1", media_id)
        return row_to_media(row) if row else None

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
        if row is None:
            raise LookupError("media_not_found")
        return row_to_media(row)

    async def update_media_thumbnail_state(
        self,
        media_id: str,
        *,
        thumb_key: str | None,
        thumb_is_orig: bool,
        thumb_status: str,
        thumb_size: int | None,
    ) -> Media:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE media
                SET thumb_key = $2,
                    thumb_is_orig = $3,
                    thumb_status = $4,
                    thumb_size = $5
                WHERE id = $1
                RETURNING *
                """,
                media_id,
                thumb_key,
                thumb_is_orig,
                thumb_status,
                thumb_size,
            )
        if row is None:
            raise LookupError("media_not_found")
        return row_to_media(row)

    async def list_media_by_thumb_status(self, *statuses: str) -> list[Media]:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM media WHERE thumb_status = ANY($1::text[]) ORDER BY created_at",
                list(statuses),
            )
        return [row_to_media(row) for row in rows]

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
        return [row_to_album(row) for row in rows]

    async def list_albums(self) -> list[Album]:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM albums ORDER BY created_at")
        return [row_to_album(row) for row in rows]

    async def list_album_media(self, album_id: str) -> list[Media]:
        pool = self.database.require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM media WHERE album_id = $1 ORDER BY position", album_id)
        return [row_to_media(row) for row in rows]

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
        return row_to_media(row) if row else None

    async def delete_album(self, album_id: str) -> tuple[Album | None, list[Media]]:
        pool = self.database.require_pool()
        async with pool.acquire() as conn, conn.transaction():
            media_rows = await conn.fetch("SELECT * FROM media WHERE album_id = $1 ORDER BY position", album_id)
            album_row = await conn.fetchrow("DELETE FROM albums WHERE id = $1 RETURNING *", album_id)
            if album_row is None:
                return None, []
        return row_to_album(album_row), [row_to_media(row) for row in media_rows]

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
        return [row_to_media(row) for row in rows]
