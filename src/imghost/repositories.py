from __future__ import annotations

from .db import Database
from .models import Album, ApiKey, Media, OAuthStateNonce, ShareXDeleteCapability, User, UserSsoLink
from .repository_media import AlbumMediaRepository
from .repository_users import UserRepository


class PostgresRepository:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.users = UserRepository(database)
        self.albums = AlbumMediaRepository(database)

    async def create_user(self, user: User) -> User:
        return await self.users.create_user(user)

    async def get_user(self, user_id: str) -> User | None:
        return await self.users.get_user(user_id)

    async def get_user_by_email(self, email: str) -> User | None:
        return await self.users.get_user_by_email(email)

    async def get_user_by_username(self, username: str) -> User | None:
        return await self.users.get_user_by_username(username)

    async def update_user(self, user: User) -> User:
        return await self.users.update_user(user)

    async def upsert_api_key(self, api_key: ApiKey) -> ApiKey:
        return await self.users.upsert_api_key(api_key)

    async def get_api_key_by_hash(self, key_hash: str) -> ApiKey | None:
        return await self.users.get_api_key_by_hash(key_hash)

    async def get_api_key_for_user(self, user_id: str) -> ApiKey | None:
        return await self.users.get_api_key_for_user(user_id)

    async def update_api_key(self, api_key: ApiKey) -> ApiKey:
        return await self.users.update_api_key(api_key)

    async def create_user_sso_link(self, link: UserSsoLink) -> UserSsoLink:
        return await self.users.create_user_sso_link(link)

    async def get_user_sso_link(self, provider: str, provider_uid: str) -> UserSsoLink | None:
        return await self.users.get_user_sso_link(provider, provider_uid)

    async def list_user_sso_links(self, user_id: str) -> list[UserSsoLink]:
        return await self.users.list_user_sso_links(user_id)

    async def delete_user_sso_link(self, user_id: str, provider: str) -> UserSsoLink | None:
        return await self.users.delete_user_sso_link(user_id, provider)

    async def create_oauth_state_nonce(self, nonce: OAuthStateNonce) -> OAuthStateNonce:
        return await self.users.create_oauth_state_nonce(nonce)

    async def consume_oauth_state_nonce(self, jti: str) -> OAuthStateNonce | None:
        return await self.users.consume_oauth_state_nonce(jti)

    async def delete_expired_oauth_state_nonces(self) -> None:
        await self.users.delete_expired_oauth_state_nonces()

    async def get_oauth_state_nonce(self, jti: str) -> OAuthStateNonce | None:
        return await self.users.get_oauth_state_nonce(jti)

    async def list_user_media(self, user_id: str) -> list[Media]:
        return await self.users.list_user_media(user_id)

    async def list_user_albums(self, user_id: str) -> list[Album]:
        return await self.users.list_user_albums(user_id)

    async def list_user_albums_page(self, user_id: str, *, limit: int = 10, offset: int = 0) -> tuple[list[Album], int]:
        return await self.users.list_user_albums_page(user_id, limit=limit, offset=offset)

    async def list_media_for_album_ids(self, album_ids: list[str]) -> dict[str, list[Media]]:
        return await self.albums.list_media_for_album_ids(album_ids)

    async def list_users(self) -> list[User]:
        return await self.users.list_users()

    async def list_users_filtered(
        self,
        *,
        q: str | None = None,
        is_admin: bool | None = None,
        suspended: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[User], int]:
        return await self.users.list_users_filtered(
            q=q,
            is_admin=is_admin,
            suspended=suspended,
            limit=limit,
            offset=offset,
        )

    async def summarize_users(self, user_ids: list[str]) -> dict[str, dict[str, int]]:
        return await self.users.summarize_users(user_ids)

    async def list_all_media(self) -> list[Media]:
        return await self.albums.list_all_media()

    async def create_sharex_delete_capability(self, capability: ShareXDeleteCapability) -> ShareXDeleteCapability:
        return await self.albums.create_sharex_delete_capability(capability)

    async def get_sharex_delete_capability(self, selector: str) -> ShareXDeleteCapability | None:
        return await self.albums.get_sharex_delete_capability(selector)

    async def touch_sharex_delete_capability(self, selector: str) -> ShareXDeleteCapability | None:
        return await self.albums.touch_sharex_delete_capability(selector)

    async def consume_sharex_delete_capability(self, selector: str, album_id: str) -> ShareXDeleteCapability | None:
        return await self.albums.consume_sharex_delete_capability(selector, album_id)

    async def revoke_sharex_delete_capabilities_for_album(self, album_id: str) -> int:
        return await self.albums.revoke_sharex_delete_capabilities_for_album(album_id)

    async def revoke_sharex_delete_capabilities_for_user(self, user_id: str) -> int:
        return await self.albums.revoke_sharex_delete_capabilities_for_user(user_id)

    async def delete_expired_sharex_delete_capabilities(self, now) -> int:
        return await self.albums.delete_expired_sharex_delete_capabilities(now)

    async def delete_consumed_sharex_delete_capabilities_older_than(self, cutoff) -> int:
        return await self.albums.delete_consumed_sharex_delete_capabilities_older_than(cutoff)

    async def delete_revoked_sharex_delete_capabilities_older_than(self, cutoff) -> int:
        return await self.albums.delete_revoked_sharex_delete_capabilities_older_than(cutoff)

    async def list_albums_page(
        self,
        *,
        q: str | None = None,
        owner: str | None = None,
        anonymous: bool | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> tuple[list[Album], int]:
        return await self.albums.list_albums_page(
            q=q,
            owner=owner,
            anonymous=anonymous,
            limit=limit,
            offset=offset,
        )

    async def delete_user(self, user_id: str) -> tuple[User | None, list[Album], list[Media]]:
        return await self.users.delete_user(user_id)

    async def create_album(self, album: Album) -> Album:
        return await self.albums.create_album(album)

    async def get_album(self, album_id: str) -> Album | None:
        return await self.albums.get_album(album_id)

    async def update_album(self, album: Album) -> Album:
        return await self.albums.update_album(album)

    async def create_media(self, media: Media) -> Media:
        return await self.albums.create_media(media)

    async def get_media(self, media_id: str) -> Media | None:
        return await self.albums.get_media(media_id)

    async def update_media(self, media: Media) -> Media:
        return await self.albums.update_media(media)

    async def list_media_by_thumb_status(self, *statuses: str) -> list[Media]:
        return await self.albums.list_media_by_thumb_status(*statuses)

    async def find_pending_thumbnails(self) -> list[Media]:
        return await self.albums.find_pending_thumbnails()

    async def find_failed_thumbnails(self) -> list[Media]:
        return await self.albums.find_failed_thumbnails()

    async def list_expired_albums(self, now) -> list[Album]:
        return await self.albums.list_expired_albums(now)

    async def list_albums(self) -> list[Album]:
        return await self.albums.list_albums()

    async def list_album_media(self, album_id: str) -> list[Media]:
        return await self.albums.list_album_media(album_id)

    async def next_position(self, album_id: str) -> int:
        return await self.albums.next_position(album_id)

    async def delete_media(self, media_id: str) -> Media | None:
        return await self.albums.delete_media(media_id)

    async def delete_album(self, album_id: str) -> tuple[Album | None, list[Media]]:
        return await self.albums.delete_album(album_id)

    async def update_media_positions(self, album_id: str, positions: dict[str, int]) -> list[Media]:
        return await self.albums.update_media_positions(album_id, positions)
