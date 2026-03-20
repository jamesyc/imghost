# Current Status

## Summary

The prototype is now a working single-service FastAPI application with:

- PostgreSQL-backed state
- optional Redis-backed sessions, rate limits, and task dispatch
- pluggable storage
- background thumbnail processing
- authenticated and anonymous upload flows
- public album/media serving
- admin APIs
- a basic multi-page browser UI
- Docker support for separate app and worker containers

It is no longer just an anonymous upload proof-of-concept.

## Implemented

### Uploads And Albums

- Anonymous upload flow
- Authenticated upload flow
- Multi-file upload into a single album
- Authenticated append to an existing owned album
- Public album JSON endpoint
- Public album HTML page
- Public per-user album listing page
- Album ZIP download
- Album editing:
  - title updates
  - cover selection
  - item reorder
- Per-media deletion
- Album deletion via:
  - `DELETE`
  - ShareX-style `GET`

### Ownership And Authorization

- Anonymous albums use `delete_token`
- Authenticated owners can manage their own albums
- Admins can manage any album
- Album edit/reorder/delete-media/delete-album all use consistent owner/admin-or-token authorization

### Media Serving

- Raw media serving via `/i/{id}.{ext}`
- Thumbnail serving via `/t/{id}.{ext}`
- Range request support
- Long-lived cache headers
- Public URLs generated from trusted request origin when available, with `BASE_URL` fallback
- Trusted public-origin allowlist via `TRUSTED_PUBLIC_ORIGINS`

### Media Processing

- Processor registry
- JPEG, PNG, BMP
- GIF and animated WebP
- SVG sanitization and thumbnailing
- MP4, MOV, WebM
- ffmpeg/ffprobe-based video inspection and thumbnailing
- Thumbnail state machine:
  - `pending`
  - `processing`
  - `done`
  - `failed`

### Background Work

- Redis-backed task queue
- Dedicated worker process / container
- Async in-process fallback queue when Redis is unavailable
- Sync fallback task mode
- Startup thumbnail recovery
- Retry-thumbnails CLI command

### Authentication

- Local registration
- Local login by username or email
- Hybrid Redis-backed browser sessions with signed-cookie fallback
- API keys
- API key regeneration
- Current-user endpoint
- Password change
- Account deletion
- Admin login audit event
- bcrypt password hashing
- configurable session cookie `Secure` behavior via `BASE_URL` / `SESSION_COOKIE_SECURE`

### Browser UI

- `/`
  - login
  - registration
  - logout
  - anonymous upload
- `/dashboard`
  - session or API-key driven account tools
  - authenticated upload
  - owned album listing and mutation
  - ShareX download
- `/admin`
  - admin user and album operations
  - runtime config
  - audit log
- `/album-tools`
  - manual token-based anonymous album operations

### Admin And Runtime Config

- Admin user CRUD
- Admin password reset
- Admin album listing/update/delete
- Global storage stats
- Runtime config get/patch
- Audit log API with filters
- Env-lock-aware runtime config behavior

### Rate Limits And Quotas

- Redis-backed upload rate limiting
- In-process fallback upload rate limiting when Redis is unavailable
- Runtime-config-backed anonymous and authenticated upload limit settings
- Per-user rate-limit overrides
- Server-wide storage quota
- Per-user storage quota

### Storage And Infrastructure

- Filesystem storage backend
- Garage/S3-compatible storage backend
- Redis service support
- Storage initialization command
- Docker setup under [`docker/`](/home/james/imghost/docker)
- Compose project name `imghost`
- Local `docker/.env`-based Compose flow
- Dedicated worker container in Compose
- Optional remote Postgres/Garage endpoint overrides for the app container

### Database

- PostgreSQL schema under [`docker/db/init/001-init.sql`](/home/james/imghost/docker/db/init/001-init.sql)
- Triggers for `updated_at` on:
  - `users`
  - `albums`
  - `config`
- Tables for:
  - users
  - user_sso_links
  - api_keys
  - albums
  - media
  - config
  - audit_log
  - user_rate_limits

### Tests

- Full pytest suite covering:
  - auth
  - upload flow
  - media processing
  - storage backends
  - album mutation auth
  - browser-page UX regressions

## Partially Implemented Or Deliberately Simplified

- ShareX config export exists, but download still requires API-key-authenticated requests
- ZIP downloads work, but are buffered in memory rather than streamed
- Session auth uses Redis when available, but retains signed-cookie fallback semantics rather than becoming purely server-side
- Rate limiting and task dispatch degrade to process-local behavior when Redis is unavailable
- Public origin validation is exact-match allowlist based; there is no wildcard or trusted-proxy model yet
- Browser UI exists, but is intentionally basic and primarily for testing

## Not Implemented

### Auth / Identity

- OAuth / SSO flows

### Infra / Scaling

- Structured metrics/observability stack
- Full production reverse-proxy/CDN docs

### Product

- polished end-user UI
- private/password-protected albums
- email-based password reset
- richer admin UX beyond the current utility pages

## Immediate Next Work

If work continues from the current state, the highest-value next steps are:

1. Stream ZIP downloads instead of buffering them in memory.
2. Decide whether ShareX config should be downloadable from a browser session.
3. Add structured observability around degraded Redis mode, queue depth, worker health, and rejected public-origin candidates.
4. Add stronger reverse-proxy / trusted-proxy hardening around forwarded headers.
5. Replace the temporary browser UI with a real product UI.
