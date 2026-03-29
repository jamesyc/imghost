from __future__ import annotations

from .models import Album, ApiKey, Media, ShareXDeleteCapability, User, UserSsoLink


def row_to_user(row) -> User:
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


def row_to_api_key(row) -> ApiKey:
    return ApiKey(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        key_hash=row["key_hash"],
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
    )


def row_to_user_sso_link(row) -> UserSsoLink:
    return UserSsoLink(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        provider=row["provider"],
        provider_uid=row["provider_uid"],
        linked_at=row["linked_at"],
    )


def row_to_album(row) -> Album:
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


def row_to_media(row) -> Media:
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


def row_to_sharex_delete_capability(row) -> ShareXDeleteCapability:
    return ShareXDeleteCapability(
        selector=row["selector"],
        purpose=row["purpose"],
        album_id=row["album_id"],
        user_id=str(row["user_id"]),
        secret_hash=row["secret_hash"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        consumed_at=row["consumed_at"],
        revoked_at=row["revoked_at"],
        last_seen_at=row["last_seen_at"],
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
