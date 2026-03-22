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

## Session model

The app intentionally prefers graceful availability:

- Redis-backed sessions when Redis is healthy
- signed-cookie fallback when Redis is down

This means a Redis outage degrades revocation semantics rather than hard-failing authentication.

Explicit posture:

- keep the app usable during Redis outages
- accept that Redis-backed session invalidation is temporarily weaker in that state
- rely on browser cookie clearing for logout in the current client while degraded

## Browser-session CSRF posture

- browser-session-authenticated `POST`, `PATCH`, and `DELETE` routes require a trusted `Origin` or `Referer`
- trusted origins are matched exactly against the configured public-origin allowlist/base URL model
- bearer API-key requests are not subject to the browser-session CSRF gate
- login and registration are intentionally exempt
- logout and other browser-session mutations are protected

Anonymous album manage-token flows are separate from browser-session auth, but if a session cookie is also present the browser-session CSRF gate still applies unless the request carries same-origin headers.

## URL generation and proxy trust

- exact trusted public-origin allowlist
- optional trusted proxy CIDR gate for forwarded headers
- fallback to `BASE_URL` when request-derived origin data is rejected

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

## Current limits

- no wildcard public-origin support
- no full proxy-chain trust model
- no richer metrics/telemetry backend yet
