# ShareX

`imghost` can generate ShareX uploader configuration files as `.sxcu` JSON.

## Endpoint

- `GET /api/v1/user/me/sharex-config`

## Generated config

The generated payload includes:

- upload URL pointing at `/api/v1/upload`
- `Authorization: Bearer ...`
- multipart form upload settings
- response field mappings for media URL, thumbnail URL, and delete URL

For authenticated ShareX uploads, `delete_url` is a capability URL that opens a confirmation flow:

- `GET /sharex/delete/{album_id}?token=...` validates the persisted delete capability and redirects
- `GET /sharex/delete/{album_id}/confirm` renders a confirmation page using a short-lived HTTP-only cookie
- `POST /sharex/delete/{album_id}/confirm` consumes the capability and deletes the album

Deletion does not happen on `GET`.

## Auth behavior

The route works with either:

- bearer API key auth
- browser session auth

### If called with an API key

- the presented raw key is embedded into the generated config

### If called from a browser session

- the app issues or rotates the API key
- the fresh raw key is embedded into the generated config

This is necessary because the app stores only the hash of the API key, not the raw value.

## Delete capability behavior

ShareX delete URLs are backed by persisted capability records in PostgreSQL.

- each authenticated ShareX upload creates a scoped delete capability for that album
- capabilities expire after 90 days
- the raw capability secret is returned only in the upload response
- the long-lived capability URL is exchanged for a short-lived confirmation cookie before deletion
- the confirmation cookie is short-lived and valid for 5 minutes
- repeated `GET` requests are allowed while the capability is still valid and the album has not been deleted
- expired, revoked, or already-consumed links return a generic invalid-link error
- capabilities are invalidated when the owning user is deleted
- capabilities are revoked when the owning user is suspended
- expired capabilities are pruned during the normal cleanup path
- revoked and consumed capabilities are retained briefly, then pruned during cleanup
- Redis is not required for correctness; Redis-backed deployments and Redis-free beginner deployments use the same ShareX delete flow

## Reverse proxy behavior

The generated upload URL uses the same public-origin resolution logic as the rest of the app, including:

- `TRUSTED_PUBLIC_ORIGINS`
- optional `TRUSTED_PROXY_CIDRS_ENABLED`
- `BASE_URL` fallback
