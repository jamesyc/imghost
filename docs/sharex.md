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

## Reverse proxy behavior

The generated upload URL uses the same public-origin resolution logic as the rest of the app, including:

- `TRUSTED_PUBLIC_ORIGINS`
- optional `TRUSTED_PROXY_CIDRS_ENABLED`
- `BASE_URL` fallback

