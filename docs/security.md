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
- API keys stored hash-only
- browser sessions signed with `SECRET_KEY`
- cookie security controlled by `SESSION_COOKIE_SECURE`

## Session model

The app intentionally prefers graceful availability:

- Redis-backed sessions when Redis is healthy
- signed-cookie fallback when Redis is down

This means a Redis outage degrades revocation semantics rather than hard-failing authentication.

## URL generation and proxy trust

- exact trusted public-origin allowlist
- optional trusted proxy CIDR gate for forwarded headers
- fallback to `BASE_URL` when request-derived origin data is rejected

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

