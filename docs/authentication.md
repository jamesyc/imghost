# Authentication

`imghost` supports browser sessions and bearer API keys.

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

## Browser sessions

Session implementation lives in [`src/imghost/sessions.py`](/home/james/imghost/src/imghost/sessions.py).

Behavior:

- cookies are signed with `SECRET_KEY`
- `remember_me=true` uses `SESSION_REMEMBER_DAYS`
- session cookies are `HttpOnly` and `SameSite=Lax`
- `Secure` follows `SESSION_COOKIE_SECURE`

Session storage model:

- when Redis is healthy and enabled, a Redis session record is written and the cookie is marked `store=redis`
- when Redis is unavailable, session creation and resolution fall back to signed-cookie behavior so browser auth keeps working

This is intentionally availability-first rather than purely server-side.

Operational consequence:

- the app keeps working through Redis outages
- logout still clears the browser cookie
- server-side revocation semantics are weaker during the outage because Redis session records cannot be consulted

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

## ShareX behavior

`GET /api/v1/user/me/sharex-config` works with either:

- bearer API-key auth
- browser-session auth

Behavior:

- if the request is API-key-authenticated, that raw presented key is embedded in the `.sxcu`
- if the request is browser-session-authenticated, the app auto-issues or rotates the API key and embeds the fresh raw key

That behavior exists because the app only stores the hash of the key, not the original raw value.
