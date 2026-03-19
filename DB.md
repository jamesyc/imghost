# Database Notes

This file describes the schema that actually exists today in [`docker/db/init/001-init.sql`](/home/james/imghost/docker/db/init/001-init.sql).

## Database Engine

- PostgreSQL
- `pgcrypto` extension enabled for UUID generation

## Triggers

The schema defines a shared `set_updated_at()` trigger function and uses it on:

- `users`
- `albums`
- `config`

## Tables

### `users`

- `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`
- `username TEXT UNIQUE NOT NULL`
- `email TEXT UNIQUE`
- `password_hash TEXT`
- `is_admin BOOLEAN NOT NULL DEFAULT false`
- `is_suspended BOOLEAN NOT NULL DEFAULT false`
- `quota_bytes BIGINT`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`

Notes:

- local-password accounts and password-less CLI-created accounts both fit this schema
- suspension and quota live directly on the user row

### `user_sso_links`

- `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`
- `user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE`
- `provider TEXT NOT NULL`
- `provider_uid TEXT NOT NULL`
- `linked_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `UNIQUE(provider, provider_uid)`

Notes:

- the table exists now even though SSO flows are not implemented yet

### `api_keys`

- `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`
- `user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE`
- `key_hash TEXT UNIQUE NOT NULL`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `last_used_at TIMESTAMPTZ`

Notes:

- API keys are persisted as hashes, not raw values

### `albums`

- `id TEXT PRIMARY KEY`
- `user_id UUID REFERENCES users(id) ON DELETE CASCADE`
- `title TEXT`
- `cover_media_id TEXT`
- `delete_token TEXT`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `expires_at TIMESTAMPTZ`

Notes:

- `cover_media_id` is intentionally a soft reference, not a foreign key
- `delete_token` is used for anonymous/public album management
- authenticated owned albums store `delete_token=NULL`

### `media`

- `id TEXT PRIMARY KEY`
- `album_id TEXT NOT NULL REFERENCES albums(id) ON DELETE CASCADE`
- `user_id UUID REFERENCES users(id) ON DELETE SET NULL`
- `filename_orig TEXT`
- `media_type TEXT NOT NULL`
- `format TEXT NOT NULL`
- `mime_type TEXT NOT NULL`
- `storage_key TEXT NOT NULL`
- `thumb_key TEXT`
- `thumb_is_orig BOOLEAN NOT NULL DEFAULT false`
- `thumb_status TEXT NOT NULL DEFAULT 'pending'`
- `file_size BIGINT NOT NULL`
- `thumb_size BIGINT`
- `width INT`
- `height INT`
- `duration_secs DOUBLE PRECISION`
- `is_animated BOOLEAN NOT NULL DEFAULT false`
- `codec_hint TEXT`
- `position BIGINT NOT NULL`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`

Notes:

- media ordering is persisted through `position`
- thumbnails may point at the original or a separate object

### `config`

- `key TEXT PRIMARY KEY`
- `value TEXT`
- `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`
- `updated_by UUID REFERENCES users(id) ON DELETE SET NULL`

Notes:

- runtime config overrides are stored here

### `audit_log`

- `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`
- `event_type TEXT NOT NULL`
- `actor_id UUID`
- `actor_ip_hash TEXT`
- `target_type TEXT`
- `target_id TEXT`
- `correlation_id TEXT`
- `metadata JSONB`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`

### `user_rate_limits`

- `user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE`
- `rpm INT`
- `bph BIGINT`

Notes:

- the row exists for persisted per-user rate limit overrides

## Indexes

The schema currently defines:

- `idx_albums_user_updated` on `albums (user_id, updated_at DESC)`
- `idx_albums_expires` on `albums (expires_at)` where not null
- `idx_media_album_position` on `media (album_id, position)`
- `idx_media_user_id` on `media (user_id)`
- `idx_media_thumb_pending` on `media (thumb_status)` for pending/processing rows
- `idx_audit_created` on `audit_log (created_at)`
- `idx_audit_actor` on `audit_log (actor_id)`
- `idx_audit_correlation` on `audit_log (correlation_id)`
- `idx_audit_event_type` on `audit_log (event_type)`
- `idx_api_keys_user_id` on `api_keys (user_id)`
- `idx_sso_links_user_id` on `user_sso_links (user_id)`

## Practical Notes

- Tests bootstrap this schema automatically when needed.
- The pytest suite truncates tables between tests.
- Do not run the test suite against a shared or production database.
