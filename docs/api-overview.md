# API Overview

This is a human-oriented map of the main routes. It is not meant to replace generated OpenAPI output.

## Public and browser pages

- `GET /`
- `GET /dashboard`
- `GET /settings`
- `GET /admin`
- `GET /album-tools`
- `GET /a/{album_id}`
- `GET /u/{username}`

## Auth

- `POST /api/v1/auth/login`
- `POST /api/v1/auth/register`
- `POST /api/v1/auth/logout`

## Upload and album access

- `POST /api/v1/upload`
- `GET /api/v1/album/{album_id}`
- `GET /api/v1/album/{album_id}/zip`
- `DELETE /api/v1/album/{album_id}`
- `GET /api/v1/album/{album_id}/delete`
- `PATCH /api/v1/album/{album_id}`
- `PATCH /api/v1/album/{album_id}/order`
- `DELETE /api/v1/media/{media_id}`

Anonymous/public mutation rules:

- anonymous albums receive a `delete_url` with `delete_token`
- `delete_token` can authorize album delete, album patch, album reorder, and media delete for anonymous/public albums
- authenticated owners and admins can perform the same mutations without `delete_token`

## Current-user APIs

- `GET /api/v1/user/me`
- `GET /api/v1/user/me/albums`
- `POST /api/v1/user/me/api-key`
- `PATCH /api/v1/user/me/password`
- `GET /api/v1/user/me/sharex-config`
- `DELETE /api/v1/user/me`

## Media serving

- `GET /i/{id}.{ext}`
- `GET /t/{id}.{ext}`

`GET /t/{id}.{ext}` currently returns:

- `200` when the thumbnail exists
- `202` while the thumbnail is pending or processing
- `404` when thumbnail generation failed or the media/album is unavailable

## Admin APIs

- `GET /api/v1/admin/users`
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
- `actor_id`
- `user_id`
- `correlation_id`
- `after`
- `before`
- `limit` default `100`, maximum `500`
- `offset`

## Health

- `GET /health/live`
- `GET /health/ready`

## Common error/status patterns

Some important API behaviors worth knowing:

- `POST /api/v1/upload` can return `400`, `413`, `415`, or `429` for invalid uploads, quota rejection, unsupported media, or rate limiting
- protected endpoints return `401` for missing or invalid authentication
- mutation endpoints return `403` for failed owner/admin/delete-token authorization
