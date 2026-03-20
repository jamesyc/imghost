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

## Browser sessions

Session implementation lives in [`src/imghost/sessions.py`](/home/james/imghost/src/imghost/sessions.py).

Behavior:

- cookies are signed with `SECRET_KEY`
- `remember_me=true` uses `SESSION_REMEMBER_DAYS`
- session cookies are `HttpOnly` and `SameSite=Lax`
- `Secure` follows `SESSION_COOKIE_SECURE`

Session storage model:

- when Redis is healthy and enabled, a Redis session record is written and the cookie is marked `store=redis`
- when Redis is unavailable, the signed cookie still authenticates the user

This is intentionally availability-first rather than purely server-side.

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

## ShareX behavior

`GET /api/v1/user/me/sharex-config` works with either:

- bearer API-key auth
- browser-session auth

Behavior:

- if the request is API-key-authenticated, that raw presented key is embedded in the `.sxcu`
- if the request is browser-session-authenticated, the app auto-issues or rotates the API key and embeds the fresh raw key

That behavior exists because the app only stores the hash of the key, not the original raw value.
