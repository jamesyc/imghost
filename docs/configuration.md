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
- `DATABASE_URL`
  Direct PostgreSQL DSN for non-Compose or app-only runs.
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
  Whether this process should run queue consumers when the queue backend supports workers
- `THUMBNAIL_WORKER_COUNT`

## Docker/Compose settings

These are defined in [`docker/.env.example`](/home/james/imghost/docker/.env.example).
That file is also intentionally deployment-oriented: strict public-origin mode is enabled, proxy trust gating is on, and session cookies are secure by default.

They include all of the app-level concepts above, plus Docker-stack-specific settings:

### Host/service ports

- `PORT`
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

Compose derives `DATABASE_URL` from these values instead of storing a second copy in `docker/.env`.

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
- `rate_limit_anon_rpm`
- `rate_limit_anon_bph`
- `rate_limit_global_anon_rpm`
- `rate_limit_global_anon_bph`
- `rate_limit_user_rpm`
- `rate_limit_user_bph`

These can be environment-locked with:

- `LOCK_ALLOW_REGISTRATION`
- `LOCK_ANON_UPLOAD`
- `LOCK_ANON_EXPIRY`
- `LOCK_RATE_LIMITS`

## Resolution notes

- `SESSION_COOKIE_SECURE` defaults from the scheme of `BASE_URL` when unset.
- `SESSION_REDIS_FAIL_CLOSED` defaults to `false`.
- `PUBLIC_ORIGIN_ENABLED` defaults to `true`.
- `TRUSTED_PROXY_CIDRS_ENABLED` defaults to `false`.
- `TRUSTED_PROXY_CIDRS_ENABLED=true` requires a non-empty `TRUSTED_PROXY_CIDRS` list.
- `REDIS_PASSWORD` is only injected when `REDIS_URL` does not already contain credentials.
