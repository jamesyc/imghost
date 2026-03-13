# Current Status

## Overview

The prototype is now well past the original anonymous upload proof-of-concept stage. It supports:

- Anonymous/public album flows
- Authenticated API-key and browser-session user flows
- Background thumbnail processing and recovery
- Image, SVG, animated image, and video processing pipelines
- Cleanup/pruning commands
- Authenticated registration, browser-aware auth/upload home page, admin user management, quota enforcement, runtime-config-backed upload rate limiting, album management, audit log API, and runtime config API

The codebase remains a FastAPI prototype backed by:

- PostgreSQL-backed repository, audit log, and runtime config storage
- Docker Compose PostgreSQL service with bind-mounted local data directory
- local filesystem storage
- in-process async task workers

It does **not** yet implement the full production architecture from `DESIGN.md` such as Redis, S3-compatible object storage, OAuth/SSO, or the full production Redis-backed runtime/session model.

## Implemented

### Public / Anonymous Flow

- Anonymous upload endpoint
- Multi-file batch upload into a single album
- Public album JSON endpoint
- Public album HTML page
- Public user album list page:
  - `GET /u/{username}`
  - sorted by most recently modified albums first
  - hides expired albums
- Media serving via `/i/{id}.{ext}`
- Thumbnail serving via `/t/{id}.{ext}`
- Range request support for media streaming
- Album ZIP download
- Anonymous album delete tokens
- Album deletion via `DELETE` and ShareX-style `GET`
- Album editing:
  - title changes
  - cover selection
  - item reorder
- Per-media deletion

### Background Tasks / Thumbnail Pipeline

- Task queue abstraction
- Sync fallback task queue
- Async in-process worker queue
- Startup recovery for `pending` and stuck `processing` thumbnails
- Re-enqueue of failed thumbnails
- `python -m imghost retry-thumbnails`
- Real thumbnail lifecycle through:
  - `pending`
  - `processing`
  - `done`
  - `failed`

### Media Processing

- Format-specific processor registry
- Static image processors:
  - JPEG/JPG
  - PNG
  - BMP
- Animated image processors:
  - GIF
  - WebP
- SVG processor:
  - sanitizes scripts/event handlers/external refs
  - stores sanitized SVG
  - rasterizes JPEG thumbnails
- Video processors:
  - MP4
  - MOV
  - WebM
- Video metadata extraction/remux/thumbnail hooks via `ffprobe`/`ffmpeg`
- Animated image thumbnail strategy:
  - small files can serve original as thumb
  - larger files can generate WebP and fall back if not worth it
- Video compatibility warnings:
  - HEVC warning
  - WebM/VP9 compatibility warning path

### Cleanup / Maintenance

- Expired album pruning
- Prune dry-run mode
- `python -m imghost prune`
- `python -m imghost prune --dry-run`

### Database / Persistence

- Docker Compose Postgres service:
  - `docker-compose.yml`
  - bind-mounted data directory at `./postgres-data`
- Schema bootstrap SQL loaded via `db/init/001-init.sql`
- Postgres-backed app adapters for:
  - repository/state
  - audit log
  - runtime config
- `DATABASE_URL` runtime configuration
- `asyncpg` added via `uv`
- Verified smoke path against live Postgres container:
  - schema initialization
  - CLI user creation
  - CLI API key issuance
- Current caveat:
  - this is an initial DB cut, not a full finished migration
  - legacy tests still assume the old JSON-state test harness and have not been rewritten yet

### Users / Authentication

- Persisted `User` model
- Persisted per-user upload limit override fields:
  - `rate_limit_rpm`
  - `rate_limit_bph`
- Persisted `ApiKey` model
- One active API key per user
- Bearer API-key auth
- API key `last_used_at` tracking
- Local password login by username or email
- Self-service registration with username, email, and password
- Signed cookie session auth
- Browser logout endpoint
- Remember-me session support
- Home page reflects session state and auth actions:
  - sign-in form for anonymous users
  - registration form when allowed
  - logout action for signed-in users
- CLI bootstrap:
  - `python -m imghost create-user`
  - `python -m imghost issue-api-key`
- Authenticated uploads:
  - one file per request
  - always new single-item album
  - no expiry by default
  - no delete token
  - user-owned storage path
- Current user endpoint:
  - `GET /api/v1/user/me`
- Local auth endpoints:
  - `POST /api/v1/auth/login`
  - `POST /api/v1/auth/register`
  - `POST /api/v1/auth/logout`
- API key regeneration:
  - `POST /api/v1/user/me/api-key`
- ShareX config download:
  - `GET /api/v1/user/me/sharex-config`
  - requires API-key auth even when browser session auth is available
- Self account deletion:
  - `DELETE /api/v1/user/me`
- Password change:
  - `PATCH /api/v1/user/me/password`

### Quotas

- Server-wide storage quota enforcement
- Per-user storage quota enforcement
- Effective per-user quota resolution:
  - explicit user quota if set
  - otherwise default configured quota
- Correct status codes:
  - `507` when server quota is exceeded
  - `413` when user quota is exceeded

### Rate Limiting

- In-process upload rate limiting for the prototype
- Runtime-config-backed limits for:
  - per-anonymous identity uploads per minute
  - per-anonymous identity bytes per hour
  - global anonymous uploads per minute
  - global anonymous bytes per hour
  - per-authenticated-user uploads per minute
  - per-authenticated-user bytes per hour
- Effective per-authenticated-user rate-limit resolution:
  - explicit user override if set
  - otherwise runtime-config default
- Anonymous identity derived from client IP plus user agent
- IP extraction precedence for proxied deployments:
  - `CF-Connecting-IP`
  - `X-Real-IP`
  - `X-Forwarded-For`
  - socket client host
- Current enforcement target:
  - upload endpoint

### Admin

- Admin access via existing bearer API-key auth and `is_admin`
- User management endpoints:
  - `GET /api/v1/admin/users`
  - `POST /api/v1/admin/users`
  - `PATCH /api/v1/admin/users/{userId}`
  - `DELETE /api/v1/admin/users/{userId}`
- Global storage stats:
  - `GET /api/v1/admin/stats`
- Album management endpoints:
  - `GET /api/v1/admin/albums`
  - `PATCH /api/v1/admin/albums/{albumId}`
  - `DELETE /api/v1/admin/albums/{albumId}`
- Admin album expiry set/clear
- Admin user suspend/quota/password updates
- Admin user create/patch/list payloads include per-user upload rate-limit overrides
- Dedicated admin password reset endpoint:
  - `POST /api/v1/admin/users/{userId}/reset-password`
  - generic admin user patch no longer mutates passwords
- Audit log API:
  - `GET /api/v1/admin/audit`
  - filterable by event type, actor, affected user, correlation ID, and date range
- Runtime config API:
  - `GET /api/v1/admin/config`
  - `PATCH /api/v1/admin/config`
  - env-lock-aware effective values returned to admin clients

### Audit

- Postgres-backed audit writer/reader abstraction
- Audit persistence triggered by event bus listeners
- Audit entries for current domain events:
  - `AlbumCreated`
  - `MediaUploaded`
  - `AlbumDeleted`
  - `MediaDeleted`
  - `AlbumTitleChanged`
  - `AlbumCoverSet`
  - `AlbumReordered`
  - `AlbumExpiryChanged`
  - `ConfigChanged`
  - `UserRegistered`
  - `UserDeleted`
  - `UserSuspended`
  - `UserPasswordReset`
  - `AdminLoggedIn`

### Runtime Config

- Postgres-backed runtime config writer/reader abstraction
- Effective value resolution with env lock support
- Persisted overrides for:
  - `allow_registration`
  - `anon_upload_enabled`
  - `anon_expiry_hours`
  - rate-limit config keys from the design
- Immediate registration enable/disable without restart via `allow_registration`
- Immediate anonymous upload behavior changes without restart for:
  - anonymous upload enable/disable
  - anonymous expiry hours
- Home page UI consumes runtime config for:
  - registration availability
  - anonymous upload availability
  - anonymous expiry messaging
- Upload endpoint consumes runtime config for:
  - anonymous and authenticated rate limits

### Domain Events Currently Present

- `AlbumCreated`
- `MediaUploaded`
- `AlbumDeleted`
- `MediaDeleted`
- `AlbumTitleChanged`
- `AlbumCoverSet`
- `AlbumReordered`
- `AlbumExpiryChanged`
- `ConfigChanged`
- `UserRegistered`
- `UserDeleted`
- `UserSuspended`
- `UserPasswordReset`
- `AdminLoggedIn`

These events exist and are emitted in the service layer. Thumbnail, audit, and config listeners are now implemented where relevant; metrics and other future listeners from the design are still pending.

## Not Yet Implemented

### Authentication / Sessions

- Redis-backed session management
- OAuth / SSO

### Audit / Config System

- Config change audit/history UI beyond raw audit log
- Runtime-config-backed rate limiting UI visibility beyond raw config JSON

### Storage / Infra from Final Design

- Redis-backed task queue / sessions / rate limits
- S3-compatible object storage backend
- multi-service worker deployment
- correlation-aware structured logging/metrics stack

### Additional User/Admin Features

- Admin album rescue/expiry UI beyond API
- Admin audit browsing UI beyond API
- Admin configuration panel

## MVP Assessment

### Anonymous/Public MVP

The anonymous/public media host path is already at a solid prototype-MVP level.

That now includes:

1. public album viewing
2. album ZIP download
3. public user album lists

### Logged-In User MVP

The backend now has a credible logged-in-user MVP:

1. local login/logout/session support is implemented
2. registration is implemented
3. current user uploads, password change, account deletion, and ShareX export are implemented

The remaining gaps are polish and scale-oriented concerns, not the core logged-in flow.

## Recommended Next Step

The next best implementation slice depends on the target:

### If the goal is backend completeness against `DESIGN.md`

Implement the remaining control-plane and auth semantics next:

- Redis-backed limiter behavior matching the final design
- explicit fail-open/fail-closed behavior around Redis availability

That is now the main remaining gap in the rate-limit/auth control-plane area.

## Test Status

Current automated status at the time of writing:

- focused slices passing:
  - `uv run pytest -q tests/test_app.py -k 'public_user_album_list or upload_album_and_media_serving or admin_album_management or api_key_upload'`
  - `uv run pytest -q tests/test_app.py -k 'index_page or album_patch or admin_album_management or admin_user_management or registration'`
  - `uv run pytest -q tests/test_app.py -k 'admin_password_reset or admin_user_management or admin_audit_log or user_can_change_password'`
  - `uv run pytest -q tests/test_app.py -k 'admin_audit_log or admin_login'`
  - `uv run pytest -q tests/test_app.py -k 'admin_user_management or admin_password_reset or local_login or registration_creates_user_session_and_audit_entry'`
- additional DB smoke checks passing:
  - `docker compose up -d`
  - `docker compose exec -T postgres psql -U imghost -d imghost -c "\\dt"`
  - `DATABASE_URL=postgresql://imghost:imghost@localhost:5432/imghost uv run python -m imghost create-user --username pgadmin --email pgadmin@example.com`
  - `DATABASE_URL=postgresql://imghost:imghost@localhost:5432/imghost uv run python -m imghost issue-api-key --user-id 72a00f56-54d2-4da5-aff7-8b8b095fbc53`
  - `uv run python -m compileall src`
