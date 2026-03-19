# Current Status

## Summary

The prototype is now a working single-service FastAPI application with:

- PostgreSQL-backed state
- pluggable storage
- background thumbnail processing
- authenticated and anonymous upload flows
- public album/media serving
- admin APIs
- a basic multi-page browser UI

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
- Public URLs generated from request origin when available, with `BASE_URL` fallback

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

- Async in-process task queue
- Sync fallback task mode
- Startup thumbnail recovery
- Retry-thumbnails CLI command

### Authentication

- Local registration
- Local login by username or email
- Signed browser session cookies
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

- In-process upload rate limiting
- Runtime-config-backed anonymous and authenticated upload limit settings
- Per-user rate-limit overrides
- Server-wide storage quota
- Per-user storage quota

### Storage And Infrastructure

- Filesystem storage backend
- Garage/S3-compatible storage backend
- Storage initialization command
- Docker setup under [`docker/`](/home/james/imghost/docker)
- Compose project name `imghost`
- Local `docker/.env`-based Compose flow
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
- Rate limiting works, but is in-process instead of Redis-backed
- Session auth works, but is signed-cookie-based rather than Redis-backed/server-stored
- Browser UI exists, but is intentionally basic and primarily for testing

## Not Implemented

### Auth / Identity

- OAuth / SSO flows
- Redis-backed session storage

### Infra / Scaling

- Redis-backed rate limiting
- Redis-backed task queue
- Multi-service worker deployment
- Structured metrics/observability stack
- Full production reverse-proxy/CDN docs

### Product

- polished end-user UI
- private/password-protected albums
- email-based password reset
- richer admin UX beyond the current utility pages

## Immediate Next Work

If work continues from the current state, the highest-value next steps are:

1. Move sessions, rate limits, and task dispatch off in-process memory.
2. Decide whether ShareX config should be downloadable from a browser session.
3. Stream ZIP downloads instead of buffering them.
4. Add trusted-host/origin validation around forwarded-host URL generation if the app will sit behind arbitrary proxies.
5. Replace the temporary browser UI with a real product UI.
