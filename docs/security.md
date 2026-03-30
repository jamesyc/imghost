# Security

This document summarizes the security-relevant behavior currently implemented.

## Upload validation

- empty uploads rejected
- maximum upload size enforced
- processor-based validation for supported image/video formats
- max pixel count enforced through processor setup
- SVG sanitization supported

## Auth

- passwords hashed with bcrypt
- local passwords must be at least 8 characters when created or changed through the app APIs
- API keys stored hash-only
- browser sessions signed with `SECRET_KEY`
- cookie security controlled by `SESSION_COOKIE_SECURE`
- optional Google OAuth login/link flows with PKCE

## Auth rate limiting

- password login is rate limited by client IP and by repeated failures against the same normalized login identifier
- registration is rate limited by client IP
- repeated invalid bearer API-key attempts are rate limited by client IP
- repeated admin-access denials are rate limited by client IP
- throttled auth routes return `429 Too Many Requests` with a generic error instead of exposing account validity details
- successful password login clears the narrow account-scoped login failure state
- Redis improves shared enforcement across multiple processes, but auth throttling still works when Redis is disabled or temporarily unavailable
- without Redis, auth throttles are enforced with process-local in-memory counters, which is acceptable for the beginner stack but weaker across multiple app instances

## Session model

The app supports two Redis session outage postures:

- graceful mode (`SESSION_REDIS_FAIL_CLOSED=false`)
  - Redis-backed sessions when Redis is healthy
  - signed-cookie fallback when Redis is down
- fail-closed mode (`SESSION_REDIS_FAIL_CLOSED=true`)
  - Redis-backed sessions when Redis is healthy
  - browser sessions stop authenticating if Redis-backed session state cannot be consulted

Tradeoff:

- graceful mode favors availability and accepts weaker revocation during the outage
- fail-closed mode favors stronger session invalidation semantics and accepts browser-auth downtime during the outage

## Browser-session CSRF posture

- browser-session-authenticated `POST`, `PATCH`, and `DELETE` routes require a trusted `Origin` or `Referer`
- trusted origins are matched exactly against the configured public-origin allowlist/base URL model
- bearer API-key requests are not subject to the browser-session CSRF gate
- login and registration are intentionally exempt
- logout and other browser-session mutations are protected

Anonymous album manage-token flows are separate from browser-session auth, but if a session cookie is also present the browser-session CSRF gate still applies unless the request carries same-origin headers.

## ShareX delete posture

- authenticated ShareX uploads return a dedicated `delete_url`
- the URL is a capability entry point, not a destructive `GET`
- capability records are stored in PostgreSQL, scoped to one album and one owner, and expire automatically after 90 days
- `GET /sharex/delete/{album_id}?token=...` validates the capability and exchanges it for a short-lived HTTP-only confirmation cookie
- `POST /sharex/delete/{album_id}/confirm` consumes the capability and deletes the album
- the confirmation cookie expires after 5 minutes
- repeated `GET` requests are allowed while the capability is still valid
- expired, revoked, or consumed links return a generic invalid-link response
- capabilities are invalidated when the owning user is deleted and revoked when the owning user is suspended
- expired, revoked, and old consumed capability rows are pruned during cleanup
- Redis is optional acceleration only; ShareX delete correctness does not depend on Redis being available

## URL generation and proxy trust

- exact trusted public-origin allowlist
- optional trusted proxy CIDR gate for forwarded headers
- fallback to `BASE_URL` when request-derived origin data is rejected
- the same forwarded-header trust model also affects client-IP identity for auth throttling and anonymous upload throttling

## Baseline browser security headers

The app now sets these headers broadly on HTML, JSON, media, and error responses:

- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `X-Frame-Options: DENY`
- `Content-Security-Policy: frame-ancestors 'none'`

`Strict-Transport-Security` is added only when the request is effectively HTTPS:

- direct `https://` requests
- or trusted forwarded `X-Forwarded-Proto: https` behind a trusted proxy

The app does not currently ship a stricter full script/style CSP.

## Admin protection

Admin pages and APIs require admin auth.

Important admin endpoints include:

- user management
- runtime config
- audit log
- runtime status

Repeated failed or forbidden admin access attempts are also subject to auth throttling.

## Current limits

- no wildcard public-origin support
- no full proxy-chain trust model
- no external metrics or telemetry service yet; metrics are currently exposed from the app process and audit data is stored in PostgreSQL
- auth throttling without Redis is process-local, so multi-instance deployments get weaker shared enforcement unless Redis is available
