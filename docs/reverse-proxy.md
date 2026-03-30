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

## Local mode vs deployed mode

For self-hosted users, the simplest rule is:

- if you open `imghost` directly on your machine or LAN, permissive forwarded-header trust is acceptable
- if you put `imghost` behind nginx, Caddy, Traefik, Cloudflare Tunnel, or another reverse proxy, switch to explicit proxy trust

Local mode:

- `PUBLIC_ORIGIN_ENABLED=false`
- `TRUSTED_PROXY_CIDRS_ENABLED=false`
- low setup friction
- appropriate when there is no separate proxy in front of the app

Deployed mode:

- `PUBLIC_ORIGIN_ENABLED=true`
- `TRUSTED_PROXY_CIDRS_ENABLED=true`
- `TRUSTED_PROXY_CIDRS` set to the proxy/container-network CIDRs that actually connect to the app
- `TRUSTED_PUBLIC_ORIGINS` includes every public hostname users will visit

Why this matters:

- forwarded headers tell the app what public host and protocol to reflect into generated links
- forwarded client-IP headers also influence auth throttling and anonymous upload rate limiting
- if you trust forwarded headers from arbitrary clients in a real deployment, a direct client can try to influence that public host/protocol view
- if you trust forwarded client-IP headers from arbitrary clients in a real deployment, a direct client can also spoof the client identity used for auth and anonymous upload throttling
- the app still falls back safely, but explicit proxy trust is the right long-term deployment posture

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
PUBLIC_ORIGIN_ENABLED=true
TRUSTED_PUBLIC_ORIGINS=https://imghost.example.com,https://imghost.example2.com
TRUSTED_PROXY_CIDRS_ENABLED=true
TRUSTED_PROXY_CIDRS=127.0.0.1/32,172.16.0.0/12
```

The checked-in [`.env.example`](/home/james/imghost/.env.example) now follows this hardened deployment posture by default.

If you serve `https://photos.example.com` and `https://uploads.example.com`, both must be listed in `TRUSTED_PUBLIC_ORIGINS`. Unknown hosts are intentionally ignored and the app falls back to `BASE_URL`.

## Current limitations

- exact origin matching only
- no wildcard origin support
- no deeper proxy-chain trust model beyond the immediate peer CIDR check

## HTTPS and HSTS

The app emits `Strict-Transport-Security` only when a request is effectively HTTPS.

That means:

- direct HTTPS requests get HSTS
- trusted forwarded `X-Forwarded-Proto: https` requests get HSTS
- plain HTTP local/direct-access requests do not

For reverse-proxy deployments, make sure the proxy sends:

- `X-Forwarded-Proto`
- `X-Forwarded-Host`

and that proxy trust is configured correctly, or the app will intentionally avoid trusting the forwarded HTTPS signal.
