# API Overview

This is a human-oriented map of the main routes. It is not meant to replace generated OpenAPI output.

## Public and browser pages

- `GET /`
- `GET /login`
- `GET /register`
- `GET /dashboard`
- `GET /albums`
- `GET /albums/{album_id}`
- `GET /settings`
- `GET /admin`
- `GET /admin/users`
- `GET /admin/users/new`
- `GET /admin/users/{user_id}`
- `GET /admin/albums`
- `GET /admin/config`
- `GET /admin/ops`
- `GET /a/{album_id}`
- `GET /u/{username}`
- `GET /manage/{album_id}`
- `GET /manifest.webmanifest`
- `GET /service-worker.js`

## Auth

- `POST /api/v1/auth/login`
- `POST /api/v1/auth/register`
- `POST /api/v1/auth/logout`
- `GET /auth/google/start`
- `GET /auth/{provider}/start`
- `GET /auth/google/callback`
- `GET /auth/{provider}/callback`

## Upload and album access

- `POST /api/v1/upload`
- `GET /sharex/delete/{album_id}`
- `GET /sharex/delete/{album_id}/confirm`
- `POST /sharex/delete/{album_id}/confirm`
- `GET /api/v1/album/{album_id}`
- `GET /api/v1/album/{album_id}/zip`
- `DELETE /api/v1/album/{album_id}`
- `PATCH /api/v1/album/{album_id}`
- `PATCH /api/v1/album/{album_id}/order`
- `DELETE /api/v1/media/{media_id}`

Anonymous/public mutation rules:

- anonymous albums receive a `manage_url` with `token`
- the manage workspace exposes both a public link and a copyable private manage link
- that token can authorize album delete, album patch, album reorder, and media delete for anonymous/public albums
- authenticated owners and admins can perform the same mutations without `delete_token`
- authenticated ShareX uploads additionally return a `delete_url` that goes through a confirmation page and consumes a persisted delete capability on `POST`

## Current-user APIs

- `GET /api/v1/user/me`
- `GET /api/v1/user/me/albums`
- `POST /api/v1/user/me/api-key`
- `PATCH /api/v1/user/me/password`
- `GET /api/v1/user/me/sharex-config`
- `DELETE /api/v1/user/me`
- `POST /api/v1/user/me/oauth/google/disconnect`

`DELETE /api/v1/user/me` supports two confirmation modes:

- `{"method":"password","password":"..."}`
- `{"method":"oauth_reauth"}`

For `oauth_reauth`, the short-lived confirmation proof is set by the OAuth callback in an `HttpOnly` cookie and is read server-side during deletion.

`GET /api/v1/user/me/albums` returns a paginated envelope:

- `items`
- `total`
- `limit`
- `offset`
- `has_more`

## Media serving

- `GET /i/{id}.{ext}`
- `GET /t/{id}.{ext}`

`GET /t/{id}.{ext}` currently returns:

- `200` when the thumbnail exists
- `202` while the thumbnail is pending or processing
- `404` when thumbnail generation failed or the media/album is unavailable

## Admin APIs

- `GET /api/v1/admin/users`
- `GET /api/v1/admin/users/{user_id}`
- `GET /api/v1/admin/users/{user_id}/stats`
- `GET /api/v1/admin/users/{user_id}/albums`
- `POST /api/v1/admin/users`
- `PATCH /api/v1/admin/users/{user_id}`
- `POST /api/v1/admin/users/{user_id}/reset-password`
- `DELETE /api/v1/admin/users/{user_id}`
- `GET /api/v1/admin/albums`
- `PATCH /api/v1/admin/albums/{album_id}`
- `DELETE /api/v1/admin/albums/{album_id}`
- `GET /api/v1/admin/audit`
- `GET /api/v1/admin/config`
- `PATCH /api/v1/admin/config`
- `GET /api/v1/admin/stats`
- `GET /api/v1/admin/runtime-status`

`GET /api/v1/admin/audit` supports filters and pagination:

- `event_type`
- `action`
- `result`
- `source`
- `actor_id`
- `user_id`
- `correlation_id`
- `request_id`
- `after`
- `before`
- `limit` default `100`, maximum `500`
- `offset`

`GET /api/v1/admin/users/{user_id}/albums` returns a paginated envelope:

- `items`
- `total`
- `limit`
- `offset`
- `has_more`

## Health

- `GET /health/live`
- `GET /health/ready`
- `GET /metrics`

## Common error/status patterns

Some important API behaviors worth knowing:

- `POST /api/v1/upload` can return `400`, `413`, `415`, or `429` for invalid uploads, quota rejection, unsupported media, or rate limiting
- `POST /api/v1/auth/login` and `POST /api/v1/auth/register` can return `429` after repeated auth attempts
- protected bearer-authenticated endpoints can return `429` after repeated invalid API-key attempts
- admin routes can return `429` after repeated failed or forbidden admin access attempts
- `GET /api/v1/user/me/albums` validates `limit` as `1..200` and requires non-negative `offset`
- protected endpoints return `401` for missing or invalid authentication
- mutation endpoints return `403` for failed owner/admin/delete-token authorization
