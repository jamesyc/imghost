# Authentication

`imghost` supports browser sessions, bearer API keys, and optional Google OAuth.

## Local auth flows

Routes:

- `POST /api/v1/auth/login`
- `POST /api/v1/auth/register`
- `POST /api/v1/auth/logout`

Local login supports:

- username
- email

Registration creates:

- a user
- a browser session
- a registration audit/event flow

Password-setting policy:

- local passwords must be at least 8 characters
- the backend enforces this for registration, admin user creation, admin reset, and current-user password change

Auth throttling:

- `POST /api/v1/auth/login` can return `429` after repeated attempts from one client IP
- repeated failed login attempts against the same normalized login identifier can also trigger a temporary lock
- `POST /api/v1/auth/register` can return `429` after repeated attempts from one client IP
- repeated invalid bearer API-key attempts can trigger `429` on protected API routes
- repeated admin-access denials can trigger `429` on admin routes
- throttled responses stay generic and do not reveal whether an account or API key is valid

## OAuth flows

Routes:

- `GET /auth/google/start`
- `GET /auth/google/callback`
- `GET /auth/{provider}/start`
- `GET /auth/{provider}/callback`
- `POST /api/v1/user/me/oauth/google/disconnect`

Current shipped provider support:

- Google OAuth with PKCE

Current behavior:

- OAuth sign-in is optional and controlled by `GOOGLE_OAUTH_ENABLED`, `GOOGLE_CLIENT_ID`, and `GOOGLE_CLIENT_SECRET`
- OAuth can sign an existing linked user in, create a new account when registration is allowed, or link a provider to an already signed-in account
- OAuth login still creates the normal browser session cookie after the provider callback completes
- `/settings` supports Google disconnect and OAuth-based re-authentication for account deletion
- disconnect is blocked if it would leave the account without any remaining sign-in method

## Browser sessions

Session implementation lives in [`src/imghost/sessions.py`](/home/james/imghost/src/imghost/sessions.py).

Behavior:

- cookies are signed with `SECRET_KEY`
- `remember_me=true` uses `SESSION_REMEMBER_DAYS`
- session cookies are `HttpOnly` and `SameSite=Lax`
- `Secure` follows `SESSION_COOKIE_SECURE`

Session storage model:

- when Redis is healthy and enabled, a Redis session record is written and the cookie is marked `store=redis`
- when `SESSION_REDIS_FAIL_CLOSED=false`, Redis outages fall back to signed-cookie behavior so browser auth keeps working
- when `SESSION_REDIS_FAIL_CLOSED=true`, Redis-backed browser sessions fail closed if Redis is unavailable

This is configurable because local/self-hosted convenience and production-oriented revocation semantics pull in different directions.

Operational consequence:

- graceful mode:
  - the app keeps working through Redis outages
  - logout still clears the browser cookie
  - server-side revocation semantics are weaker during the outage because Redis session records cannot be consulted
  - auth rate limiting falls back to process-local counters if Redis-backed counters cannot be used
- fail-closed mode:
  - browser login and registration return `503` while Redis-backed sessions are unavailable
  - existing browser sessions stop authenticating until Redis recovers
  - revocation semantics stay closer to server-tracked session behavior

Auth throttling is independent of the browser-session fail-closed setting. In the beginner stack and other Redis-free deployments, login, registration, API-key, and admin throttles still run with in-memory counters.

## Browser-session mutation protection

Session-authenticated browser mutations are protected with trusted-origin checks.

Current policy:

- session-authenticated `POST`, `PATCH`, and `DELETE` requests require a trusted `Origin` or `Referer`
- the trusted set comes from the same public-origin configuration used for generated URLs
- when `PUBLIC_ORIGIN_ENABLED=false`, the app trusts the direct browser-visible host instead of requiring an explicit origin allowlist
- bearer API-key requests are exempt from this browser-session check
- `POST /api/v1/auth/login` and `POST /api/v1/auth/register` are intentionally exempt
- `POST /api/v1/auth/logout` is protected

Failure mode:

- blocked requests return `403` with `CSRF protection blocked the request.`

Operational implication:

- if the app is served from more than one browser-visible origin, every valid origin must be configured in the trusted public-origin allowlist or browser-session mutations will fail

## Stale session handling

If a page request arrives with an invalid or stale session cookie:

- the app clears the cookie
- the request is treated as anonymous for normal page rendering

This avoids leaking JSON `Invalid session` errors into the HTML experience.

## API keys

API keys are:

- one active key per user
- stored hash-only
- rotated by issuing a new key and invalidating the old one

Current user API key route:

- `POST /api/v1/user/me/api-key`

Current user summary payloads also expose:

- `has_password`
- `has_api_key`
- API key timestamps
- linked `sso_providers`

Bearer API-key authentication can be rate limited after repeated invalid attempts from one client IP. Normal successful API use is not itself rate limited by these auth counters.

## Delete-token authorization

Anonymous albums do not have an owning user, so their mutation model is based on `delete_token`.

A valid `delete_token` can authorize:

- album deletion
- album metadata updates
- album reorder
- media deletion

Owned albums can instead be managed by:

- the owning authenticated user
- an admin

Important interaction with browser sessions:

- token-authenticated mutations work without CSRF headers when no browser session is present
- if a browser session cookie is present on the same request, the browser-session CSRF gate still applies
- same-origin browser requests with a valid token continue to work
- the `/manage/{album_id}` workspace still accepts the `?token=` entry URL, but the browser scrubs the visible token from the address bar after load
- anonymous manage access is retained on the device with bounded local persistence and a path-scoped manage cookie so the workspace can reload after URL scrubbing

## ShareX behavior

`GET /api/v1/user/me/sharex-config` works with either:

- bearer API-key auth
- browser-session auth

Behavior:

- if the request is API-key-authenticated, that raw presented key is embedded in the `.sxcu`
- if the request is browser-session-authenticated, the app auto-issues or rotates the API key and embeds the fresh raw key

That behavior exists because the app only stores the hash of the key, not the original raw value.

Authenticated ShareX uploads also return a dedicated `delete_url`:

- the URL is scoped to a single album and owner
- the URL is backed by a persisted capability record in PostgreSQL
- the capability expires after 90 days
- `GET /sharex/delete/{album_id}?token=...` validates the capability and redirects to a confirmation page
- `POST /sharex/delete/{album_id}/confirm` consumes the capability and deletes the album
- the confirmation cookie is valid for 5 minutes
- repeated `GET` requests are allowed while the capability is still valid and unconsumed
- expired, revoked, or consumed links return a generic invalid-link response
- capabilities are invalidated when the owning user is deleted and revoked when the owning user is suspended
- the flow works the same with and without Redis

## Account deletion confirmation

`DELETE /api/v1/user/me` requires an explicit confirmation payload.

Supported confirmation modes:

- `password`
- `oauth_reauth`

`oauth_reauth` is currently used with the Google OAuth delete-account re-auth flow from `/settings`.
