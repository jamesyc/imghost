from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from uuid import uuid4

import bcrypt
from fastapi import HTTPException

from .config import Settings
from .account_delete_reauth import AccountDeleteReauthPayload, AccountDeleteReauthTokenManager
from .events import (
    ApiKeyIssued,
    EventBus,
    UserAdminStatusChanged,
    UserDeleted,
    UserLimitsChanged,
    UserPasswordChanged,
    UserPasswordReset,
    UserRegistered,
    UserSuspended,
)
from .models import ApiKey, Media, User, utcnow
from .oauth import OAuthIdentity
from .repositories import PostgresRepository
from .storage import StorageBackend

UNSET = object()
MIN_PASSWORD_LENGTH = 8


@dataclass
class ApiKeyIssueResult:
    api_key: ApiKey
    raw_key: str


@dataclass
class UserCreateInput:
    username: str
    email: str
    password: str | None
    is_admin: bool
    quota_bytes: int | None
    rate_limit_rpm: int | None = None
    rate_limit_bph: int | None = None


@dataclass
class UserUpdateInput:
    is_admin: bool | object = UNSET
    suspended: bool | None = None
    quota_bytes: int | None | object = UNSET
    rate_limit_rpm: int | None | object = UNSET
    rate_limit_bph: int | None | object = UNSET
    password: str | None = None


@dataclass
class PasswordChangeInput:
    current_password: str
    new_password: str


@dataclass
class LocalLoginInput:
    login: str
    password: str


@dataclass
class AccountDeletionConfirmationInput:
    method: str
    current_password: str | None = None
    reauth_token: str | None = None


class AccountService:
    def __init__(
        self,
        settings: Settings,
        repository: PostgresRepository,
        storage: StorageBackend,
        event_bus: EventBus,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.storage = storage
        self.event_bus = event_bus

    def _provider_label(self, provider: str) -> str:
        normalized = provider.strip().lower()
        return {
            "google": "Google",
            "github": "GitHub",
        }.get(normalized, normalized.capitalize() or "OAuth")

    def _require_password_value(self, password: str, *, label: str) -> str:
        if not password.strip():
            raise HTTPException(status_code=400, detail=f"{label} is required.")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise HTTPException(
                status_code=400,
                detail=f"{label} must be at least {MIN_PASSWORD_LENGTH} characters.",
            )
        return password

    def _storage_keys_for_media(self, media_items: list[Media]) -> list[str]:
        keys: list[str] = []
        seen: set[str] = set()
        for media in media_items:
            for key in (media.storage_key, media.thumb_key):
                if not key or key in seen:
                    continue
                seen.add(key)
                keys.append(key)
        return keys

    def _storage_bytes_for_media(self, media_items: list[Media]) -> int:
        total = 0
        for media in media_items:
            total += media.file_size
            if media.thumb_key and media.thumb_key != media.storage_key:
                total += media.thumb_size or 0
        return total

    async def get_current_user_summary(self, user: User) -> dict[str, object]:
        items = await self.repository.list_user_media(user.id)
        albums = await self.repository.list_user_albums(user.id)
        usage = self._storage_bytes_for_media(items)
        api_key = await self.repository.get_api_key_for_user(user.id)
        sso_links = await self.repository.list_user_sso_links(user.id)
        effective_quota = user.quota_bytes if user.quota_bytes is not None else self.settings.default_user_quota_bytes
        return {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "is_admin": user.is_admin,
            "has_password": user.password_hash is not None,
            "quota_bytes": effective_quota,
            "storage_used_bytes": usage,
            "album_count": len(albums),
            "media_count": len(items),
            "has_api_key": api_key is not None,
            "api_key_created_at": api_key.created_at.isoformat() if api_key else None,
            "api_key_last_used_at": api_key.last_used_at.isoformat() if api_key and api_key.last_used_at else None,
            "sso_providers": [
                {
                    "provider": link.provider,
                    "linked_at": link.linked_at.isoformat(),
                }
                for link in sso_links
            ],
        }

    async def issue_api_key(
        self,
        user: User,
        *,
        correlation_id: str | None = None,
        actor_id: str | None = None,
        source: str = "api",
    ) -> ApiKeyIssueResult:
        replaced_existing = await self.repository.get_api_key_for_user(user.id) is not None
        raw_key = secrets.token_hex(16)
        api_key = ApiKey(
            id=str(uuid4()),
            user_id=user.id,
            key_hash=self._hash_api_key(raw_key),
            created_at=utcnow(),
            last_used_at=None,
        )
        await self.repository.upsert_api_key(api_key)
        if correlation_id is not None:
            await self.event_bus.emit(
                ApiKeyIssued(
                    user_id=user.id,
                    actor_id=actor_id if actor_id is not None else user.id,
                    replaced_existing=replaced_existing,
                    source=source,
                    correlation_id=correlation_id,
                )
            )
        return ApiKeyIssueResult(api_key=api_key, raw_key=raw_key)

    async def list_users_with_usage(self) -> list[dict[str, object]]:
        users = await self.repository.list_users()
        all_media = await self.repository.list_all_media()
        usage_by_user: dict[str, int] = {}
        count_by_user: dict[str, int] = {}
        album_count_by_user: dict[str, int] = {}
        for album in await self.repository.list_albums():
            if album.user_id is None:
                continue
            album_count_by_user[album.user_id] = album_count_by_user.get(album.user_id, 0) + 1
        for media in all_media:
            if media.user_id is None:
                continue
            usage_by_user[media.user_id] = usage_by_user.get(media.user_id, 0) + media.file_size
            if media.thumb_key and media.thumb_key != media.storage_key:
                usage_by_user[media.user_id] += media.thumb_size or 0
            count_by_user[media.user_id] = count_by_user.get(media.user_id, 0) + 1

        return [
            {
                "id": user.id,
                "username": user.username,
                "email": user.email,
                "is_admin": user.is_admin,
                "suspended": user.suspended,
                "quota_bytes": user.quota_bytes if user.quota_bytes is not None else self.settings.default_user_quota_bytes,
                "rate_limit_rpm": user.rate_limit_rpm,
                "rate_limit_bph": user.rate_limit_bph,
                "album_count": album_count_by_user.get(user.id, 0),
                "storage_used_bytes": usage_by_user.get(user.id, 0),
                "media_count": count_by_user.get(user.id, 0),
                "created_at": user.created_at.isoformat(),
            }
            for user in users
        ]

    async def list_users_with_usage_page(
        self,
        *,
        q: str | None = None,
        is_admin: bool | None = None,
        suspended: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, object]:
        users, total = await self.repository.list_users_filtered(
            q=q,
            is_admin=is_admin,
            suspended=suspended,
            limit=limit,
            offset=offset,
        )
        summaries = await self.repository.summarize_users([user.id for user in users])
        items = []
        for user in users:
            summary = summaries.get(user.id, {})
            items.append(
                {
                    "id": user.id,
                    "username": user.username,
                    "email": user.email,
                    "is_admin": user.is_admin,
                    "suspended": user.suspended,
                    "quota_bytes": user.quota_bytes if user.quota_bytes is not None else self.settings.default_user_quota_bytes,
                    "rate_limit_rpm": user.rate_limit_rpm,
                    "rate_limit_bph": user.rate_limit_bph,
                    "album_count": summary.get("album_count", 0),
                    "storage_used_bytes": summary.get("storage_used_bytes", 0),
                    "media_count": summary.get("media_count", 0),
                    "created_at": user.created_at.isoformat(),
                }
            )
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total,
        }

    async def get_user_with_usage_for_admin(self, user_id: str) -> dict[str, object]:
        user = await self.repository.get_user(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found.")

        media_items = await self.repository.list_user_media(user.id)
        albums = await self.repository.list_user_albums(user.id)
        effective_quota = user.quota_bytes if user.quota_bytes is not None else self.settings.default_user_quota_bytes
        return {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "is_admin": user.is_admin,
            "suspended": user.suspended,
            "quota_bytes": effective_quota,
            "rate_limit_rpm": user.rate_limit_rpm,
            "rate_limit_bph": user.rate_limit_bph,
            "album_count": len(albums),
            "storage_used_bytes": self._storage_bytes_for_media(media_items),
            "media_count": len(media_items),
            "created_at": user.created_at.isoformat(),
        }

    async def get_user_storage_stats_for_admin(self, user_id: str) -> dict[str, object]:
        user = await self.repository.get_user(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found.")

        media_items = await self.repository.list_user_media(user.id)
        albums = await self.repository.list_user_albums(user.id)
        effective_quota = user.quota_bytes if user.quota_bytes is not None else self.settings.default_user_quota_bytes
        return {
            "user_id": user.id,
            "username": user.username,
            "quota_bytes": effective_quota,
            "storage_used_bytes": self._storage_bytes_for_media(media_items),
            "album_count": len(albums),
            "media_count": len(media_items),
        }

    async def create_user(
        self,
        payload: UserCreateInput,
        *,
        method: str = "admin",
        correlation_id: str | None = None,
        actor_id: str | None = None,
        source: str = "api",
    ) -> User:
        username = payload.username.strip()
        email = payload.email.strip().lower()
        if not username:
            raise HTTPException(status_code=400, detail="Username is required.")
        if not email:
            raise HTTPException(status_code=400, detail="Email is required.")
        if payload.password is not None:
            self._require_password_value(payload.password, label="Password")
        if await self.repository.get_user_by_username(username):
            raise HTTPException(status_code=409, detail="Username already exists.")
        if await self.repository.get_user_by_email(email):
            raise HTTPException(status_code=409, detail="Email already exists.")

        now = utcnow()
        user = User(
            id=str(uuid4()),
            username=username,
            email=email,
            password_hash=self._hash_password(payload.password) if payload.password else None,
            is_admin=payload.is_admin,
            suspended=False,
            quota_bytes=payload.quota_bytes,
            rate_limit_rpm=payload.rate_limit_rpm,
            rate_limit_bph=payload.rate_limit_bph,
            created_at=now,
            updated_at=now,
        )
        await self.repository.create_user(user)
        if correlation_id is not None:
            await self.event_bus.emit(
                UserRegistered(
                    user_id=user.id,
                    actor_id=actor_id if actor_id is not None else (user.id if method == "registration" else None),
                    method=method,
                    source=source,
                    correlation_id=correlation_id,
                )
            )
        return user

    async def update_user(self, user_id: str, payload: UserUpdateInput, correlation_id: str, *, actor_id: str | None = None) -> User:
        user = await self.repository.get_user(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found.")

        limits_changes: dict[str, dict[str, int | None]] = {}
        if payload.is_admin is not UNSET:
            next_is_admin = bool(payload.is_admin)
            if next_is_admin != user.is_admin:
                old_is_admin = user.is_admin
                user.is_admin = next_is_admin
                await self.event_bus.emit(
                    UserAdminStatusChanged(
                        user_id=user.id,
                        actor_id=actor_id,
                        old_is_admin=old_is_admin,
                        new_is_admin=next_is_admin,
                        source="api",
                        correlation_id=correlation_id,
                    )
                )
        if payload.suspended is not None and payload.suspended != user.suspended:
            user.suspended = payload.suspended
            await self.event_bus.emit(
                UserSuspended(
                    user_id=user.id,
                    actor_id=actor_id,
                    suspended=user.suspended,
                    source="api",
                    correlation_id=correlation_id,
                )
            )
        if payload.quota_bytes is not UNSET:
            next_quota = payload.quota_bytes if payload.quota_bytes is None or isinstance(payload.quota_bytes, int) else None
            if next_quota != user.quota_bytes:
                limits_changes["quota_bytes"] = {"old": user.quota_bytes, "new": next_quota}
                user.quota_bytes = next_quota
        if payload.rate_limit_rpm is not UNSET:
            next_rpm = payload.rate_limit_rpm if payload.rate_limit_rpm is None or isinstance(payload.rate_limit_rpm, int) else None
            if next_rpm != user.rate_limit_rpm:
                limits_changes["rate_limit_rpm"] = {"old": user.rate_limit_rpm, "new": next_rpm}
                user.rate_limit_rpm = next_rpm
        if payload.rate_limit_bph is not UNSET:
            next_bph = payload.rate_limit_bph if payload.rate_limit_bph is None or isinstance(payload.rate_limit_bph, int) else None
            if next_bph != user.rate_limit_bph:
                limits_changes["rate_limit_bph"] = {"old": user.rate_limit_bph, "new": next_bph}
                user.rate_limit_bph = next_bph
        if payload.password is not None:
            user.password_hash = self._hash_password(self._require_password_value(payload.password, label="Password"))
        if limits_changes:
            await self.event_bus.emit(
                UserLimitsChanged(
                    user_id=user.id,
                    actor_id=actor_id,
                    changes=limits_changes,
                    source="api",
                    correlation_id=correlation_id,
                )
            )
        user.updated_at = utcnow()
        await self.repository.update_user(user)
        return user

    async def reset_user_password(self, user_id: str, new_password: str, correlation_id: str, *, actor_id: str | None = None) -> User:
        user = await self.repository.get_user(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found.")
        new_password = self._require_password_value(new_password, label="New password")

        user.password_hash = self._hash_password(new_password)
        user.updated_at = utcnow()
        await self.repository.update_user(user)
        await self.event_bus.emit(
            UserPasswordReset(
                user_id=user.id,
                actor_id=actor_id,
                source="api",
                correlation_id=correlation_id,
            )
        )
        return user

    async def change_password(
        self,
        user: User,
        payload: PasswordChangeInput,
        *,
        correlation_id: str | None = None,
        source: str = "api",
    ) -> User:
        if user.password_hash is not None and not self._verify_password(payload.current_password, user.password_hash):
            raise HTTPException(status_code=403, detail="Current password is incorrect.")
        user.password_hash = self._hash_password(self._require_password_value(payload.new_password, label="New password"))
        user.updated_at = utcnow()
        await self.repository.update_user(user)
        if correlation_id is not None:
            await self.event_bus.emit(
                UserPasswordChanged(
                    user_id=user.id,
                    actor_id=user.id,
                    source=source,
                    correlation_id=correlation_id,
                )
            )
        return user

    async def authenticate_local_user(self, payload: LocalLoginInput) -> User:
        login = payload.login.strip()
        if not login or not payload.password:
            raise HTTPException(status_code=400, detail="Login and password are required.")

        user = await self.repository.get_user_by_email(login.lower())
        if user is None:
            user = await self.repository.get_user_by_username(login)
        if user is None or user.password_hash is None:
            raise HTTPException(status_code=401, detail="Invalid credentials.")
        if user.suspended:
            raise HTTPException(status_code=403, detail="User is not allowed to authenticate.")
        if not self._verify_password(payload.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid credentials.")
        return user

    async def global_storage_stats(self) -> dict[str, object]:
        all_media = await self.repository.list_all_media()
        total_storage = self._storage_bytes_for_media(all_media)
        anonymous_storage = self._storage_bytes_for_media([item for item in all_media if item.user_id is None])
        return {
            "server_quota_bytes": self.settings.server_quota_bytes,
            "total_storage_used_bytes": total_storage,
            "anonymous_storage_used_bytes": anonymous_storage,
            "user_count": len(await self.repository.list_users()),
            "users": await self.list_users_with_usage(),
        }

    async def delete_user_account(self, user: User, correlation_id: str) -> dict[str, int]:
        return await self.delete_user_by_id(user.id, correlation_id, deleted_by="self", actor_id=user.id)

    async def issue_account_delete_reauth_token(self, user: User, *, provider: str, provider_uid: str) -> str:
        existing_link = await self.repository.get_user_sso_link(provider, provider_uid)
        if existing_link is None or existing_link.user_id != user.id:
            raise HTTPException(status_code=403, detail="The selected OAuth account is not linked to this user.")
        return AccountDeleteReauthTokenManager(self.settings.secret_key).dumps(
            AccountDeleteReauthPayload(user_id=user.id, provider=provider, provider_uid=provider_uid)
        )

    async def validate_account_deletion_confirmation(
        self,
        user: User,
        payload: AccountDeletionConfirmationInput,
    ) -> None:
        method = payload.method.strip().lower()
        if method == "password":
            if user.password_hash is None:
                raise HTTPException(status_code=400, detail="This account does not have a local password.")
            current_password = (payload.current_password or "").strip()
            if not current_password:
                raise HTTPException(status_code=400, detail="Current password is required.")
            if not self._verify_password(current_password, user.password_hash):
                raise HTTPException(status_code=403, detail="Current password is incorrect.")
            return
        if method == "oauth_reauth":
            reauth_token = (payload.reauth_token or "").strip()
            if not reauth_token:
                raise HTTPException(status_code=400, detail="OAuth re-authentication is required.")
            try:
                reauth = AccountDeleteReauthTokenManager(self.settings.secret_key).loads(reauth_token)
            except ValueError as exc:
                raise HTTPException(status_code=403, detail="OAuth re-authentication has expired or is invalid.") from exc
            if reauth.user_id != user.id:
                raise HTTPException(status_code=403, detail="OAuth re-authentication has expired or is invalid.")
            links = await self.repository.list_user_sso_links(user.id)
            if not any(link.provider == reauth.provider and link.provider_uid == reauth.provider_uid for link in links):
                raise HTTPException(status_code=403, detail="OAuth re-authentication has expired or is invalid.")
            return
        raise HTTPException(status_code=400, detail="Unsupported account deletion confirmation method.")

    async def delete_user_by_id(
        self,
        user_id: str,
        correlation_id: str,
        *,
        deleted_by: str,
        actor_id: str | None = None,
    ) -> dict[str, int]:
        user = await self.repository.get_user(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found.")

        albums = await self.repository.list_user_albums(user.id)
        media_items = await self.repository.list_user_media(user.id)

        for key in self._storage_keys_for_media(media_items):
            await self.storage.delete(key)

        deleted_user, deleted_albums, deleted_media = await self.repository.delete_user(user.id)
        if deleted_user is None:
            raise HTTPException(status_code=404, detail="User not found.")

        await self.event_bus.emit(
            UserDeleted(
                user_id=deleted_user.id,
                actor_id=actor_id,
                actor_kind="admin" if deleted_by == "admin" else ("user" if deleted_by == "self" else "system"),
                deleted_by=deleted_by,
                album_count=len(deleted_albums),
                media_count=len(deleted_media),
                source="api",
                correlation_id=correlation_id,
            )
        )
        return {
            "album_count": len(albums),
            "media_count": len(media_items),
        }

    async def complete_oauth_login(
        self,
        identity: OAuthIdentity,
        *,
        current_user: User | None,
        allow_registration: bool,
        correlation_id: str | None = None,
        source: str = "web",
    ) -> tuple[User, str]:
        if not identity.provider_uid.strip() or not identity.email.strip():
            raise HTTPException(status_code=403, detail=f"{self._provider_label(identity.provider)} sign-in could not be verified.")
        if not identity.email_verified:
            raise HTTPException(
                status_code=403,
                detail=f"Your {self._provider_label(identity.provider)} account must have a verified email address.",
            )

        existing_link = await self.repository.get_user_sso_link(identity.provider, identity.provider_uid)
        if existing_link is not None:
            linked_user = await self.repository.get_user(existing_link.user_id)
            if linked_user is None:
                raise HTTPException(status_code=403, detail="Linked account is no longer available.")
            if linked_user.suspended:
                raise HTTPException(status_code=403, detail="User is not allowed to authenticate.")
            if current_user is not None and current_user.id != linked_user.id:
                raise HTTPException(
                    status_code=409,
                    detail=f"This {self._provider_label(identity.provider)} account is already linked to a different user.",
                )
            return linked_user, "existing_link"

        if current_user is not None:
            await self.repository.create_user_sso_link(
                self._build_user_sso_link(current_user.id, identity.provider, identity.provider_uid)
            )
            return current_user, "linked"

        if not allow_registration:
            raise HTTPException(status_code=403, detail="Registration is disabled.")

        existing_email_user = await self.repository.get_user_by_email(identity.email)
        if existing_email_user is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "An account with this email already exists. "
                    f"Sign in locally first, then connect {self._provider_label(identity.provider)} from Settings."
                ),
            )

        new_user = await self.create_user(
            UserCreateInput(
                username=await self._generate_unique_username(identity),
                email=identity.email,
                password=None,
                is_admin=False,
                quota_bytes=None,
            ),
            method="oauth",
            correlation_id=correlation_id,
            actor_id=None,
            source=source,
        )
        await self.repository.create_user_sso_link(
            self._build_user_sso_link(new_user.id, identity.provider, identity.provider_uid)
        )
        return new_user, "created"

    async def disconnect_oauth_provider(self, user: User, provider: str) -> None:
        links = await self.repository.list_user_sso_links(user.id)
        remaining = [link for link in links if link.provider != provider]
        if len(remaining) == len(links):
            raise HTTPException(status_code=404, detail="OAuth provider is not linked.")
        if user.password_hash is None and not remaining:
            raise HTTPException(
                status_code=400,
                detail=(
                    "You must keep at least one login method. "
                    f"Set a password before disconnecting {self._provider_label(provider)}."
                ),
            )
        await self.repository.delete_user_sso_link(user.id, provider)

    def _build_user_sso_link(self, user_id: str, provider: str, provider_uid: str):
        from .models import UserSsoLink

        return UserSsoLink(
            id=str(uuid4()),
            user_id=user_id,
            provider=provider,
            provider_uid=provider_uid,
            linked_at=utcnow(),
        )

    async def _generate_unique_username(self, identity: OAuthIdentity) -> str:
        seed = identity.email.split("@", 1)[0]
        if identity.display_name:
            display_candidate = re.sub(r"[^a-z0-9]+", "", identity.display_name.lower())
            if display_candidate:
                seed = display_candidate
        normalized = re.sub(r"[^a-z0-9]+", "", seed.lower())[:20]
        candidate = normalized or "user"
        if await self.repository.get_user_by_username(candidate) is None:
            return candidate
        for index in range(2, 1000):
            next_candidate = f"{candidate[:16]}{index}"
            if await self.repository.get_user_by_username(next_candidate) is None:
                return next_candidate
        raise HTTPException(status_code=409, detail="Unable to generate a unique username for this OAuth account.")

    def _hash_password(self, password: str) -> str:
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    def _verify_password(self, password: str, password_hash: str) -> bool:
        try:
            return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
        except ValueError:
            return False

    def _hash_api_key(self, raw_key: str) -> str:
        from hashlib import sha256

        return sha256(raw_key.encode("utf-8")).hexdigest()
