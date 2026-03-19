# Implemented But Still Different From `DESIGN.md`

This file lists material differences that still exist between the current codebase and the original [`DESIGN.md`](/home/james/imghost/DESIGN.md).

It intentionally excludes differences that have already been closed, such as:

- bcrypt password hashing
- owner/admin album mutation auth
- authenticated multi-file and append-to-owned-album uploads
- request-origin-based absolute URL generation

## 1. Sessions Are Still Signed Cookies, Not Redis-Backed

The app uses signed cookie payloads for browser sessions.

What the original design expected:

- Redis-backed session storage
- more production-oriented session invalidation semantics

Current state:

- browser sessions are stateless signed cookies
- cookie `Secure` behavior is configurable and defaults from `BASE_URL`
- no Redis dependency exists yet

## 2. Rate Limiting Is In-Process

The original design expected Redis-backed rate limiting, including behavior differences when Redis is unavailable.

Current state:

- upload rate limiting is always in-process memory
- limits are still runtime-config-backed and per-user overrides exist
- this is fine for a prototype or single-process deployment, but not a horizontally scaled one

## 3. ZIP Downloads Are Buffered In Memory

The design calls for streaming ZIP generation.

Current state:

- album ZIP downloads work
- the full ZIP archive is assembled in memory before the response is returned

## 4. ShareX Config Download Still Requires API-Key Authentication

The design treats ShareX config as a settings action for a signed-in user.

Current state:

- `GET /api/v1/user/me/sharex-config` rejects ordinary session-authenticated requests
- the request itself must be authenticated with the API key being embedded

## 5. Upload Size Policy Is Still Simplified

The original design separated image and video size limits.

Current state:

- one global `MAX_UPLOAD_BYTES` cap applies to all uploads
- there is no distinct image-vs-video size policy yet

## 6. Admin Bootstrap CLI Is Simpler Than Designed

The original design suggested a richer admin bootstrap flow.

Current state:

- CLI commands are:
  - `create-user`
  - `issue-api-key`
  - `prune`
  - `retry-thumbnails`
  - `init-storage`
- `create-user --admin` exists, but there is no dedicated interactive `create-admin` command
- CLI-created users still start with `password_hash=None` unless a password is set later through the app/admin flow

## 7. Media Responses Still Do Not Explicitly Set `Content-Length`

The design expected explicit forwarding of media response length.

Current state:

- range handling and `Content-Range` work
- the storage layer computes stream length
- the response path does not explicitly set `Content-Length`

## 8. The Browser UI Is Utility-Focused, Not Product-Focused

The current UI is intentionally basic and mostly exists to exercise the backend.

This differs from the original design, which assumed a more fully designed end-user and admin interface.

## 9. There Is Still No Trusted Public-Origin Allowlist

The app now generates public URLs from request origin first, with `BASE_URL` fallback.

That closes the single-domain limitation, but the production hardening piece is still missing:

- no `ALLOWED_HOSTS` / trusted public origin validation exists yet
- deployments should rely on a trusted reverse proxy in front of the app
