# Configuration

This document describes the environment variables currently consumed by the application and the Docker stack.

## App-level settings

These are defined in [`.env.example`](/home/james/imghost/.env.example).
That file is intentionally a hardened deployment example, not a localhost quick-start template.

### Public URL and proxy trust

- `BASE_URL`
  Canonical fallback public URL used when request-derived origin data is absent or rejected.
- `PUBLIC_ORIGIN_ENABLED`
  When `true`, only configured public origins and `BASE_URL` are trusted for reflected public URLs and browser-session origin checks. When `false`, the app reflects the direct browser-visible host for local/LAN access.
- `TRUSTED_PUBLIC_ORIGINS`
  Exact allowlist of public origins that may be reflected into generated URLs.
- `TRUSTED_PROXY_CIDRS_ENABLED`
  When `true`, forwarded headers are only trusted from peers inside `TRUSTED_PROXY_CIDRS`.
- `TRUSTED_PROXY_CIDRS`
  CIDR list for the immediate reverse proxies trusted to supply `X-Forwarded-*` headers when the gate is enabled.

Practical modes:

- local mode:
  - set `PUBLIC_ORIGIN_ENABLED=false`
  - leave `TRUSTED_PROXY_CIDRS_ENABLED=false`
  - use `BASE_URL` as fallback only
  - best when you run the app directly on localhost or on a private machine without a separate reverse proxy
- deployed mode:
  - set `PUBLIC_ORIGIN_ENABLED=true`
  - set `BASE_URL` to the public site URL
  - list every browser-visible hostname in `TRUSTED_PUBLIC_ORIGINS`
  - set `TRUSTED_PROXY_CIDRS_ENABLED=true`
  - set `TRUSTED_PROXY_CIDRS` to only the proxy/container-network addresses that should be allowed to set `X-Forwarded-*`

### Core secrets and data stores

- `SECRET_KEY`
  Used to sign browser-session cookies.
- `PROMOTE_USERNAME_TO_ADMIN`
  Optional startup-time username to promote to admin if that user already exists.
- `DATABASE_URL`
  Direct PostgreSQL DSN for non-Compose or app-only runs.
- `DATABASE_USE_PGBOUNCER`
  When `true`, the app adjusts asyncpg pool behavior for PgBouncer transaction pooling by disabling statement caching.
- `REDIS_URL`
  Canonical Redis connection URL.
- `REDIS_PASSWORD`
  Optional convenience setting. If `REDIS_URL` has no credentials, the app injects this password into the resolved Redis URL.
- `REDIS_MODE`
  One of `auto`, `required`, or `disabled`.
- `REDIS_PREFIX`
  Key prefix used for Redis data.

### Browser-session cookie settings

- `SESSION_COOKIE_NAME`
- `SESSION_COOKIE_SECURE`
- `SESSION_REDIS_FAIL_CLOSED`
  When `true`, browser-session creation and resolution fail closed if Redis-backed sessions are unavailable. When `false`, the app falls back to signed-cookie session validation during Redis outages.
- `SESSION_REMEMBER_DAYS`

### OAuth settings

- `GOOGLE_OAUTH_ENABLED`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`

### Storage settings

- `STORAGE_BACKEND`
  `filesystem` or `garage`
- `IMGHOST_DATA_DIR`
  Local data root for filesystem storage and other local data paths
- `S3_ENDPOINT_URL`
- `S3_ACCESS_KEY_ID`
- `S3_SECRET_ACCESS_KEY`
- `S3_BUCKET`
- `S3_REGION`

### Upload, media, and quota settings

- `MAX_UPLOAD_BYTES`
- `ANON_EXPIRY_HOURS`
- `MAX_PIXEL_MEGAPIXELS`
- `DEFAULT_USER_QUOTA_BYTES`
- `SERVER_QUOTA_BYTES`
- `VIDEO_THUMB_FRAMES`

### Task queue and worker settings

- `TASK_QUEUE_MODE`
  `sync`, `async`, or `redis`
- `TASK_WORKER_ENABLED`
  Process-level safety switch for queue consumers when the queue backend supports workers
- `TASK_WORKER_QUEUES`
  Queue list used by the generic `run-worker` compatibility command
- `THUMBNAIL_WORKER_COUNT`
- `SCHEDULER_ENABLED`
- `APP_SCHEDULER_ENABLED`
- `SCHEDULER_POLL_SECONDS`
- `SCHEDULER_LEASE_SECONDS`
- `CLEANUP_INTERVAL_SECONDS`
- `AUDIT_RETENTION_DAYS`

Practical deployment shapes:

- beginner Docker stack:
  - `DATABASE_USE_PGBOUNCER=false`
  - `REDIS_MODE=disabled`
  - `TASK_QUEUE_MODE=async`
  - no separate worker or scheduler service
  - app-hosted scheduler enabled by default with `APP_SCHEDULER_ENABLED=true`
- advanced Docker stack:
  - `DATABASE_USE_PGBOUNCER=true`
  - `REDIS_MODE=auto`
  - `TASK_QUEUE_MODE=redis`
  - split worker services, scheduler, and PgBouncer

## Docker/Compose settings

These are defined in [`.env.example`](/home/james/imghost/.env.example).
That file is also intentionally deployment-oriented: strict public-origin mode is enabled, proxy trust gating is on, and session cookies are secure by default.

They include all of the app-level concepts above, plus Docker-stack-specific settings:

### Host/service ports

- `PORT`
- `PGBOUNCER_PORT`
- `REDIS_PORT`
- `POSTGRES_PORT`
- `GARAGE_S3_PORT`
- `GARAGE_ADMIN_PORT`

### Postgres bootstrap values

- `POSTGRES_DB`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `POSTGRES_HOST`
- `POSTGRES_CONNECT_PORT`

Compose derives `DATABASE_URL` from these values instead of storing a second copy in `.env`.

### PgBouncer values

- `PGBOUNCER_PORT`
- `PGBOUNCER_MAX_CLIENT_CONN`
- `PGBOUNCER_DEFAULT_POOL_SIZE`

The advanced stack uses these to configure the bundled PgBouncer service and publishes it on the host for optional inspection/debugging.

### Garage cluster/bootstrap values

- `GARAGE_RPC_SECRET`
- `GARAGE_ADMIN_TOKEN`
- `GARAGE_METRICS_TOKEN`
- `GARAGE_ZONE`
- `GARAGE_CAPACITY`
- `GARAGE_KEY_NAME`

These are primarily consumed by the Garage services and the bootstrap job, not by the Python application itself.

## Runtime config in the database

Some behavior is not purely env-driven. The app also has mutable runtime config stored in PostgreSQL, implemented in [`src/imghost/runtime_config.py`](/home/james/imghost/src/imghost/runtime_config.py).

Current keys:

- `allow_registration`
- `anon_upload_enabled`
- `anon_expiry_hours`
- `max_upload_bytes`
- `video_thumb_frames`
- `default_user_quota_bytes`
- `server_quota_bytes`
- `rate_limit_anon_rpm`
- `rate_limit_anon_bph`
- `rate_limit_global_anon_rpm`
- `rate_limit_global_anon_bph`
- `rate_limit_user_rpm`
- `rate_limit_user_bph`
- `auth_rate_limit_login_ip_rpm`
- `auth_rate_limit_login_account_failures`
- `auth_rate_limit_login_account_window_seconds`
- `auth_rate_limit_login_lock_seconds`
- `auth_rate_limit_registration_ip_rpm`
- `auth_rate_limit_api_key_ip_failures`
- `auth_rate_limit_api_key_ip_window_seconds`
- `auth_rate_limit_api_key_lock_seconds`
- `auth_rate_limit_admin_ip_failures`
- `auth_rate_limit_admin_ip_window_seconds`
- `auth_rate_limit_admin_lock_seconds`

Auth rate-limit key meanings:

- `auth_rate_limit_login_ip_rpm`
  Client-IP login attempt ceiling per minute.
- `auth_rate_limit_login_account_failures`
  Failed login threshold for the same normalized login identifier before a temporary lock is applied.
- `auth_rate_limit_login_account_window_seconds`
  Rolling window used for account-scoped failed login counting.
- `auth_rate_limit_login_lock_seconds`
  Temporary lock duration after the account-scoped login failure threshold is exceeded.
- `auth_rate_limit_registration_ip_rpm`
  Client-IP registration attempt ceiling per minute.
- `auth_rate_limit_api_key_ip_failures`
  Failed bearer API-key authentication threshold per client IP before a temporary lock is applied.
- `auth_rate_limit_api_key_ip_window_seconds`
  Rolling window used for failed bearer API-key counting.
- `auth_rate_limit_api_key_lock_seconds`
  Temporary lock duration after the bearer API-key failure threshold is exceeded.
- `auth_rate_limit_admin_ip_failures`
  Failed or forbidden admin-access threshold per client IP before a temporary lock is applied.
- `auth_rate_limit_admin_ip_window_seconds`
  Rolling window used for admin denial counting.
- `auth_rate_limit_admin_lock_seconds`
  Temporary lock duration after the admin denial threshold is exceeded.

These can be environment-locked with:

- `LOCK_ALLOW_REGISTRATION`
- `LOCK_ANON_UPLOAD`
- `LOCK_ANON_EXPIRY`
- `LOCK_MAX_UPLOAD_BYTES`
- `LOCK_VIDEO_THUMB_FRAMES`
- `LOCK_DEFAULT_USER_QUOTA_BYTES`
- `LOCK_SERVER_QUOTA_BYTES`
- `LOCK_RATE_LIMITS`

`LOCK_RATE_LIMITS=true` locks both upload rate limits and the auth-throttling runtime keys above.

## Resolution notes

- `SESSION_COOKIE_SECURE` defaults from the scheme of `BASE_URL` when unset.
- `SESSION_REDIS_FAIL_CLOSED` defaults to `false`.
- `PUBLIC_ORIGIN_ENABLED` defaults to `true`.
- `TRUSTED_PROXY_CIDRS_ENABLED` defaults to `false`.
- `GOOGLE_OAUTH_ENABLED` defaults to `false`.
- `TRUSTED_PROXY_CIDRS_ENABLED=true` requires a non-empty `TRUSTED_PROXY_CIDRS` list.
- `REDIS_PASSWORD` is only injected when `REDIS_URL` does not already contain credentials.
- `APP_SCHEDULER_ENABLED` defaults to `false` in code and is turned on explicitly by the beginner Compose stack.
- `AUDIT_RETENTION_DAYS` defaults to `90`.
