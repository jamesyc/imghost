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
- Optional trusted-proxy CIDR gate for `X-Forwarded-*` handling via `TRUSTED_PROXY_CIDRS_ENABLED` and `TRUSTED_PROXY_CIDRS`

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
- Browser-session ShareX config download with auto-issue / rotation semantics
- Current-user endpoint
- Password change
- Account deletion
- Admin login audit event
- bcrypt password hashing
- configurable session cookie `Secure` behavior via `BASE_URL` / `SESSION_COOKIE_SECURE`

### Browser UI

- `/`
  - template-backed landing page
  - logout
  - anonymous upload
  - signed-in upload entry
- `/login`
  - dedicated sign-in page
- `/register`
  - dedicated registration page
- `/dashboard`
  - intended signed-in home
  - should contain the primary authenticated upload surface
  - should link to `/albums` and `/settings`
- `/albums`
  - owned album listing and mutation
  - usage/quota summary
  - no primary upload box
- `/settings`
  - account summary
  - API key rotation/reveal
  - ShareX download
  - password change
  - account deletion
  - inline action-local feedback instead of page-top flash for settings actions
- `/admin`
  - admin user and album operations
  - runtime config
  - audit log
- `/manage/{id}`
  - token-backed anonymous album workspace reusing the owner editor
- shared browser UI infrastructure:
  - Jinja templates
  - shared base layout
  - static CSS and JS assets
  - auth-aware nav
  - shared flash surface
  - user/admin page login redirects

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
- `/health/live` liveness endpoint
- `/health/ready` readiness snapshot endpoint
- `/api/v1/admin/runtime-status` admin operational status endpoint

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
  - template/static asset rendering
  - login/register page behavior
  - user/admin page redirect behavior
  - split browser-page test module for UI coverage

## Partially Implemented Or Deliberately Simplified

- Browser-session ShareX config download rotates or auto-issues the API key because existing raw key material cannot be recovered from hash-only storage
- Session auth uses Redis when available, but retains signed-cookie fallback semantics rather than becoming purely server-side
- Rate limiting and task dispatch degrade to process-local behavior when Redis is unavailable
- Public origin validation is exact-match allowlist based; there is no wildcard support and the trusted-proxy gate is still opt-in
- Trusted-proxy enforcement exists but is opt-in and CIDR-list based, so the default behavior remains permissive unless explicitly enabled
- Observability is intentionally low-noise and status-endpoint-oriented rather than a full metrics stack
- Settings and admin pages are still utility-heavy compared to the now-redesigned signed-in and public album surfaces

## Not Implemented

### Auth / Identity

- OAuth / SSO flows

### Infra / Scaling

- Structured metrics/observability stack
- Full production reverse-proxy/CDN docs

### Product

- polished end-user UI for `/dashboard`, `/albums`, `/albums/{id}`, `/a/{id}`, `/u/{username}`, and the split admin pages
- private/password-protected albums
- email-based password reset
- richer admin UX beyond the current utility page

## Immediate Next Work

If work continues from the current state, the highest-value next steps are:

1. Add richer deployment and operations docs for the new runtime-status and proxy/degraded-mode behavior.
2. Rebuild `/a/{id}` on the same visual system as the owner album workspace.
3. Strengthen automated coverage for browser-side dashboard, album-list, and owner-workspace interactions beyond page-shell checks.
4. Split the admin surface into `/admin`, `/admin/users`, `/admin/albums`, `/admin/config`, and `/admin/ops`.
5. Add richer user settings and identity features such as OAuth-linked account management.
6. Decide whether trusted-proxy enforcement should eventually become the default instead of opt-in.
