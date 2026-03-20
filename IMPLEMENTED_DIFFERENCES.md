# Implemented But Still Different From `DESIGN.md`

This file lists material differences that still exist between the current codebase and the original [`DESIGN.md`](/home/james/imghost/DESIGN.md).

It intentionally excludes differences that have already been closed, such as:

- bcrypt password hashing
- owner/admin album mutation auth
- authenticated multi-file and append-to-owned-album uploads
- request-origin-based absolute URL generation

## 1. Sessions Are Hybrid, Not Purely Server-Side

The app now uses Redis-backed sessions when Redis is healthy, but keeps signed-cookie fallback semantics.

What the original design expected:

- Redis-backed session storage
- more production-oriented session invalidation semantics

Current state:

- browser sessions are Redis-backed when Redis is available
- signed-cookie validation still works as the graceful fallback path
- cookie `Secure` behavior is configurable and defaults from `BASE_URL`
- logout still clears the browser cookie even if Redis is unavailable

## 2. ShareX Config Download From Browser Sessions Rotates The API Key

The design treats ShareX config as a settings action for a signed-in user.

Current state:

- `GET /api/v1/user/me/sharex-config` now works for ordinary browser-session-authenticated users
- if the request is authenticated by API key, that presented raw key is embedded as-is
- if the request is authenticated by browser session, the app auto-issues or rotates the API key and embeds the fresh raw key in the exported `.sxcu`
- this behavior exists because only the API-key hash is stored, so an existing raw key cannot be recovered and shown later

## 3. Upload Size Policy Is Still Simplified

The original design separated image and video size limits.

Current state:

- one global `MAX_UPLOAD_BYTES` cap applies to all uploads
- there is no distinct image-vs-video size policy yet

## 4. Admin Bootstrap CLI Is Simpler Than Designed

The original design suggested a richer admin bootstrap flow.

Current state:

- CLI commands are:
  - `create-user`
  - `issue-api-key`
  - `prune`
  - `retry-thumbnails`
  - `init-storage`
  - `run-worker`
- `create-user --admin` exists, but there is no dedicated interactive `create-admin` command
- CLI-created users still start with `password_hash=None` unless a password is set later through the app/admin flow

## 5. Media Responses Still Do Not Explicitly Set `Content-Length`

The design expected explicit forwarding of media response length.

Current state:

- range handling and `Content-Range` work
- the storage layer computes stream length
- the response path does not explicitly set `Content-Length`

## 6. The Browser UI Is Utility-Focused, Not Product-Focused

The current UI is intentionally basic and mostly exists to exercise the backend.

This differs from the original design, which assumed a more fully designed end-user and admin interface.

## 7. Public-Origin Validation Is Simpler Than A Full Trusted-Proxy Model

The app now validates request-derived public origins against an explicit allowlist and can optionally gate forwarded-header trust behind a CIDR-based trusted-proxy check.

What the design still does not fully cover:

- there is no wildcard public-origin support
- trusted-proxy enforcement is opt-in rather than on by default
- exact-match origin validation is intentionally strict and config-driven

## 8. Observability Is Status-Oriented, Not A Full Metrics Stack

The app now exposes low-noise health and runtime-status snapshots and logs subsystem degraded/recovered transitions.

What it still does not provide:

- a full metrics/telemetry backend
- deep per-task success logging by default
- broad request-level structured event capture
