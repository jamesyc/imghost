
• Based on DESIGN.md:912, the intended database is PostgreSQL with these core tables:

  - users
      - id UUID PK
      - username TEXT UNIQUE NOT NULL
      - email TEXT UNIQUE
      - password_hash TEXT NULL
      - is_admin BOOLEAN NOT NULL DEFAULT false
      - is_suspended BOOLEAN NOT NULL DEFAULT false
      - quota_bytes BIGINT NULL
      - created_at TIMESTAMPTZ NOT NULL DEFAULT now()
      - updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
  - user_sso_links
      - id UUID PK
      - user_id UUID NOT NULL FK -> users(id) ON DELETE CASCADE
      - provider TEXT NOT NULL
      - provider_uid TEXT NOT NULL
      - linked_at TIMESTAMPTZ NOT NULL DEFAULT now()
      - UNIQUE(provider, provider_uid)
  - api_keys
      - id UUID PK
      - user_id UUID NOT NULL FK -> users(id) ON DELETE CASCADE
      - key_hash TEXT UNIQUE NOT NULL
      - created_at TIMESTAMPTZ NOT NULL DEFAULT now()
      - last_used_at TIMESTAMPTZ NULL
  - albums
      - id TEXT PK (9-char public album ID)
      - user_id UUID NULL FK -> users(id) ON DELETE CASCADE
      - title TEXT NULL
      - cover_media_id TEXT NULL (soft reference, not FK)
      - created_at TIMESTAMPTZ NOT NULL DEFAULT now()
      - updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
      - expires_at TIMESTAMPTZ NULL
  - media
      - id TEXT PK (12-char public media ID)
      - album_id TEXT NOT NULL FK -> albums(id) ON DELETE CASCADE
      - user_id UUID NULL FK -> users(id) ON DELETE SET NULL
      - filename_orig TEXT
      - media_type TEXT NOT NULL
      - format TEXT NOT NULL
      - mime_type TEXT NOT NULL
      - storage_key TEXT NOT NULL
      - thumb_key TEXT NULL
      - thumb_is_orig BOOLEAN NOT NULL DEFAULT false
      - thumb_status TEXT NOT NULL DEFAULT 'pending'
      - file_size BIGINT NOT NULL
      - thumb_size BIGINT NULL
      - width INT NULL
      - height INT NULL
      - duration_secs FLOAT NULL
      - is_animated BOOLEAN NOT NULL DEFAULT false
      - codec_hint TEXT NULL
      - position BIGINT NOT NULL
      - created_at TIMESTAMPTZ NOT NULL DEFAULT now()
  - config
      - key TEXT PK
      - value TEXT NULL
      - updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
      - updated_by UUID NULL FK -> users(id) ON DELETE SET NULL
  - audit_log
      - id UUID PK
      - event_type TEXT NOT NULL
      - actor_id UUID NULL
      - actor_ip_hash TEXT NULL
      - target_type TEXT NULL
      - target_id TEXT NULL
      - correlation_id TEXT NULL
      - metadata JSONB
      - created_at TIMESTAMPTZ NOT NULL DEFAULT now()
  - user_rate_limits
      - user_id UUID PK FK -> users(id) ON DELETE CASCADE
      - rpm INT NULL
      - bph BIGINT NULL

  Important indexes from DESIGN.md:1019:

  - albums(user_id, updated_at DESC)
  - partial index on albums(expires_at) where not null
  - media(album_id, position)
  - media(user_id)
  - partial index on media(thumb_status) for pending/processing
  - audit indexes on created_at, actor_id, correlation_id, event_type
  - api_keys(user_id)
  - user_sso_links(user_id)

  Two implementation details matter:

  - albums.cover_media_id should stay a soft reference, not a foreign key.
  - updated_at on albums, config, and ideally users should be maintained by Postgres triggers, not application code.

  Compared to the current prototype in models.py, the design adds user_sso_links, config, and user_rate_limits, and renames/clarifies a few fields like is_suspended.