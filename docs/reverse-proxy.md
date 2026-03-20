# Reverse Proxy

`imghost` can sit behind a reverse proxy such as nginx or Caddy. Public URL generation is controlled by two layers:

- trusted public origins
- optional trusted proxy CIDRs for forwarded-header trust

## Public origin handling

The app resolves public URLs from:

1. forwarded origin (`X-Forwarded-Proto` + `X-Forwarded-Host`) if allowed
2. direct request origin
3. `BASE_URL` fallback

The candidate origin must match:

- `TRUSTED_PUBLIC_ORIGINS`
- or normalized `BASE_URL`

If the origin is malformed or untrusted, the app falls back to `BASE_URL`.

## Trusted proxy gating

By default:

- `TRUSTED_PROXY_CIDRS_ENABLED=false`
- forwarded-header handling is permissive

If you enable the gate:

- only immediate peers inside `TRUSTED_PROXY_CIDRS` may influence `X-Forwarded-*` processing
- untrusted peers can still hit the app, but their forwarded headers are ignored

This is implemented in [`src/imghost/public_origin.py`](/home/james/imghost/src/imghost/public_origin.py).

## Recommended nginx headers

Typical forwarded headers:

```nginx
proxy_set_header Host $host;
proxy_set_header X-Forwarded-Host $host;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
```

## Recommended deployment settings

For a deployment serving multiple domains:

```env
BASE_URL=https://imghost.example.com
TRUSTED_PUBLIC_ORIGINS=https://imghost.example.com,https://imghost.example2.com
TRUSTED_PROXY_CIDRS_ENABLED=true
TRUSTED_PROXY_CIDRS=127.0.0.1/32,172.16.0.0/12
```

## Current limitations

- exact origin matching only
- no wildcard origin support
- no deeper proxy-chain trust model beyond the immediate peer CIDR check

