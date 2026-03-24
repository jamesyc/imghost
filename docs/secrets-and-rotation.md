# Secrets And Rotation

This document is the practical answer to:

- what can I change in env and just restart
- what needs an in-service rotation first
- what is effectively bootstrap-time state once Docker volumes already exist

The short version is:

- app-only values are usually easy to rotate
- Redis password is easy to rotate if you restart Redis and its clients together
- Postgres password is not env-only once the database is initialized
- Garage secrets and imported S3 credentials are closer to bootstrap state than ordinary app config

## Rotation categories

Use these three buckets when thinking about any setting.

### Safe To Change And Restart

These are ordinary app/runtime values. Update env, restart the relevant processes, and expect the new value to apply.

Examples:

- `BASE_URL`
- `TRUSTED_PUBLIC_ORIGINS`
- `TRUSTED_PROXY_CIDRS_ENABLED`
- `TRUSTED_PROXY_CIDRS`
- `REDIS_MODE`
- `REDIS_PREFIX`
- `SESSION_COOKIE_NAME`
- `SESSION_COOKIE_SECURE`
- `SESSION_REMEMBER_DAYS`
- `MAX_UPLOAD_BYTES`
- `ANON_EXPIRY_HOURS`
- `MAX_PIXEL_MEGAPIXELS`
- `DEFAULT_USER_QUOTA_BYTES`
- `SERVER_QUOTA_BYTES`
- `VIDEO_THUMB_FRAMES`
- `TASK_QUEUE_MODE`
- `THUMBNAIL_WORKER_COUNT`

### Requires In-Service Rotation First

These values back an already-running persistent service, so changing env alone is not enough.

Examples:

- `POSTGRES_PASSWORD` on an existing Postgres data directory
- imported S3 credentials inside an already-initialized Garage deployment

### Bootstrap-Oriented / Fresh-Volume Friendly

These values behave most cleanly when set before the first startup of a persistent service or bootstrap job.

Examples:

- `GARAGE_RPC_SECRET`
- `GARAGE_ADMIN_TOKEN`
- `GARAGE_METRICS_TOKEN`
- `GARAGE_ZONE`
- `GARAGE_CAPACITY`
- `GARAGE_KEY_NAME`
- initial S3 credentials imported by `garage-init`

## App Secret: `SECRET_KEY`

Used for:

- signing browser-session cookies

Change behavior:

- update env
- restart the app

Effect:

- existing browser-session cookies stop validating immediately
- users will be logged out of browser sessions

This is straightforward operationally, but user-visible.

## Public URL / Proxy Trust Settings

Settings:

- `BASE_URL`
- `TRUSTED_PUBLIC_ORIGINS`
- `TRUSTED_PROXY_CIDRS_ENABLED`
- `TRUSTED_PROXY_CIDRS`

Change behavior:

- update env
- restart the app and any worker or scheduler services that use the same settings set

Effect:

- generated absolute URLs may change
- forwarded-header trust policy may become stricter or looser
- no persistent data migration is required

These are safe configuration-only changes.

## Redis Password

Settings:

- `REDIS_PASSWORD`
- sometimes `REDIS_URL` if you embed credentials directly

Used for:

- authenticating app, worker, and scheduler connections to Redis
- authenticating `redis-cli` and Compose healthchecks when password mode is enabled

Current implementation:

- if `REDIS_URL` already contains credentials, the app uses it as-is
- otherwise the app injects `REDIS_PASSWORD` into the resolved Redis URL
- Docker Redis runs with `--requirepass` when `REDIS_PASSWORD` is set

### Fresh deployment

This is easy:

1. set `REDIS_PASSWORD`
2. start Redis, app, workers, and scheduler

### Existing deployment

This is still relatively easy, but it is not just “change `.env` and only restart the app”.

Recommended sequence:

1. stop or coordinate app, worker, and scheduler processes that depend on Redis
2. update `REDIS_PASSWORD` in env
3. restart Redis with the new password
4. restart the app, workers, and scheduler so they reconnect with the new password

If you are using the local Docker stack and do not care about preserving Redis cache/queue/session contents, deleting the Redis volume is the simplest path.

Effect of resetting Redis state:

- Redis-backed session records disappear
- Redis-backed rate-limit counters disappear
- Redis-backed queued thumbnail jobs disappear
- the app still degrades gracefully for sessions and queueing

## Postgres Password

Settings:

- `POSTGRES_PASSWORD`
- derived `DATABASE_URL` in Compose

### Fresh deployment

Easy:

1. set `POSTGRES_PASSWORD` before first startup
2. start Postgres

### Existing deployment with persistent Postgres data

Changing env alone is not enough.

Why:

- Postgres initializes its cluster using the env value on first creation
- after that, the real password lives in the database cluster state, not just in Compose env

Recommended sequence:

1. connect to Postgres with the current credentials
2. change the password inside Postgres with SQL
3. update `POSTGRES_PASSWORD` in env
4. restart dependent services

If you only edit env and restart the containers against the existing `../postgres-data` directory, the password change may not actually take effect the way you expect.

## Garage Secrets And Tokens

Settings:

- `GARAGE_RPC_SECRET`
- `GARAGE_ADMIN_TOKEN`
- `GARAGE_METRICS_TOKEN`
- `GARAGE_ZONE`
- `GARAGE_CAPACITY`
- `GARAGE_KEY_NAME`
- `S3_ACCESS_KEY_ID`
- `S3_SECRET_ACCESS_KEY`
- `S3_BUCKET`

Garage is the most bootstrap-oriented part of the stack because:

- it persists cluster state in Docker volumes
- `garage-init` imports keys and bucket permissions into that persisted state

### Fresh deployment

This is the cleanest time to change Garage secrets and imported S3 credentials.

Recommended sequence:

1. set the desired Garage and S3 values in env
2. start the stack
3. let `garage-init` configure the cluster and bucket

### Existing deployment with persistent Garage volumes

Treat changes cautiously.

Why:

- changing env does not necessarily rewrite already-imported Garage key state
- changing bootstrap-related values like `GARAGE_KEY_NAME` or imported S3 credentials may require explicit Garage-side administrative updates
- changing zone/capacity values is not the same as merely changing app env

Practical rule:

- if the value affects only how the Python app talks to S3 and you also rotate Garage-side credentials correctly, it can be changed safely
- if the value is part of Garage bootstrap or cluster state, assume env-only edits are insufficient

## S3 / App Storage Credentials

Settings:

- `S3_ENDPOINT_URL`
- `S3_ACCESS_KEY_ID`
- `S3_SECRET_ACCESS_KEY`
- `S3_BUCKET`
- `S3_REGION`

These sit on the boundary between “app config” and “backing service state”.

### If the backend already accepts the new credentials

Then the app-side part is easy:

1. update env
2. restart app and any worker or scheduler services

### If the backend does not yet accept the new credentials

Then rotate the storage backend credentials first, then update the app.

This is especially relevant when Garage credentials were originally imported by `garage-init`.

## API Keys

User API keys are stored hash-only in PostgreSQL.

Implications:

- existing raw API keys cannot be recovered later
- browser-session ShareX export rotates or auto-issues a fresh key
- browser-session API-key reveal also rotates or issues a fresh key

This is not an env rotation issue, but it is important for operator expectations.

## Session Model Caveat

Sessions are hybrid:

- signed cookies always carry enough information to identify the user
- Redis-backed session records add stronger revocation semantics when Redis is healthy

When Redis is unavailable:

- session auth falls back to signed-cookie validation
- logout still clears the cookie
- Redis-backed invalidation guarantees are reduced until Redis recovers

## First-Run vs Existing-Volume Matrix

This is the practical summary.

### Safe on first run and later

- `BASE_URL`
- `TRUSTED_PUBLIC_ORIGINS`
- `TRUSTED_PROXY_CIDRS_ENABLED`
- `TRUSTED_PROXY_CIDRS`
- upload/quota/task settings
- session cookie settings

### Easy on first run, coordinated restart later

- `REDIS_PASSWORD`

### Easy on first run, explicit service-side rotation later

- `POSTGRES_PASSWORD`
- app-facing S3 credentials when the storage backend credentials are rotated too

### Best treated as bootstrap-oriented

- Garage cluster/bootstrap secrets
- Garage key import and bucket bootstrap values

## Recommended Operator Habit

When a setting changes, ask:

1. does this live only in the Python app, or also inside a persistent backing service
2. is the backing service state already initialized on disk
3. does the backing service need an explicit internal rotation step before env changes will work

If the answer to `2` or `3` is yes, do not assume “edit `.env` and restart” is sufficient.
