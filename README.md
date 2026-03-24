# imghost

`imghost` is a self-hosted image and video hosting prototype built with FastAPI, PostgreSQL, Redis, and either local filesystem storage or Garage/S3-compatible object storage.

## What It Currently Does

- Anonymous uploads into public albums
- Authenticated users with:
  - browser session login
  - API keys
  - registration
  - password change
  - account deletion
- Public album pages and JSON payloads
- Public per-user album listing
- Album editing:
  - title
  - cover image
  - item reorder
  - per-media deletion
  - album deletion
- Admin APIs and a basic browser admin UI
- Async thumbnail generation and recovery
- Image, animated image, SVG, and video processing
- Storage backends:
  - `filesystem`
  - `garage`

## Basic Browser UI

The current UI is template-backed and centered on the upload, album, and public-sharing flows:

- `/`:
  - sign in
  - register
  - anonymous upload
- `/dashboard`:
  - authenticated upload
  - recent albums and quick resume
- `/albums`:
  - owned album list
  - ZIP, public-link, and delete actions
- `/albums/{id}`:
  - owner album workspace
  - inline title editing
  - append upload
  - reorder
  - lightbox preview
- `/a/{id}`:
  - public album presentation
  - ZIP download
  - split-link media actions
- `/manage/{id}`:
  - token-backed anonymous album workspace
  - same editing surface as the owner album page
- `/u/{username}`:
  - public user gallery
- `/settings`:
  - account summary
  - password change
  - API key reveal/rotation
  - ShareX export
  - account deletion
- `/admin`:
  - user management
  - album management
  - runtime config
  - audit log

## Docker Setup

The Docker setup lives under [`docker/`](/home/james/imghost/docker).

Main files:

- [`docker/docker-compose.beginner.yml`](/home/james/imghost/docker/docker-compose.beginner.yml)
- [`docker/docker-compose.yml`](/home/james/imghost/docker/docker-compose.yml)
- [`docker/docker-compose.with-nginx.yml`](/home/james/imghost/docker/docker-compose.with-nginx.yml)
- [`docker/.env.example.beginner`](/home/james/imghost/docker/.env.example.beginner)
- [`docker/.env.example`](/home/james/imghost/docker/.env.example)
- [`docker/.env`](/home/james/imghost/docker/.env)

For beginners or simple LAN installs, use the beginner stack:

```bash
cp docker/.env.example.beginner docker/.env.beginner
docker compose -f docker/docker-compose.beginner.yml --env-file docker/.env.beginner up --build -d
```

That stack is intentionally simple:

- no Redis
- no PgBouncer
- no separate worker container
- no separate scheduler container
- background jobs run in-process inside the app container

The beginner Compose project name is `imghost-beginner`, so containers come up as:

- `imghost-beginner-app-1`
- `imghost-beginner-postgres-1`
- `imghost-beginner-garage-1`
- `imghost-beginner-garage-init-1`

For the full split-worker deployment, use the main stack:

```bash
cp docker/.env.example docker/.env
docker compose -f docker/docker-compose.yml --env-file docker/.env up --build -d
```

If you want nginx inside the Compose stack instead of on the host, add the optional companion file:

```bash
docker compose \
  -f docker/docker-compose.yml \
  -f docker/docker-compose.with-nginx.yml \
  --env-file docker/.env \
  up --build -d
```

That nginx service uses [`docker/nginx-site.conf`](/home/james/imghost/docker/nginx-site.conf), proxies to `app:8000`, and expects TLS certs at `./certs/fullchain.pem` and `./certs/privkey.pem`.

The main Compose project name is `imghost`, so containers come up as:

- `imghost-app-1`
- `imghost-worker-thumbnails-1`
- `imghost-worker-cleanup-1`
- `imghost-worker-default-1`
- `imghost-scheduler-1`
- `imghost-pgbouncer-1`
- `imghost-postgres-1`
- `imghost-redis-1`
- `imghost-garage-1`
- `imghost-garage-init-1`

Current default public base URL in the Docker env file:

```env
BASE_URL=https://imghost.jamesyc.com
```

Trusted public origins are configured separately, for example:

```env
TRUSTED_PUBLIC_ORIGINS=https://imghost.jamesyc.com,https://imghost.002015.xyz
```

Trusted proxy CIDRs are configured separately and only enforced when explicitly enabled:

```env
TRUSTED_PROXY_CIDRS_ENABLED=false
TRUSTED_PROXY_CIDRS=127.0.0.1/32,172.16.0.0/12
```

## Public Origin Modes

There are two supported deployment modes:

- Direct-request mode:
  - `PUBLIC_ORIGIN_ENABLED=false`
  - imghost reflects the host and scheme the browser used
  - best for localhost, direct LAN access, and simple home setups
- Strict public-origin mode:
  - `PUBLIC_ORIGIN_ENABLED=true`
  - imghost only reflects origins from `TRUSTED_PUBLIC_ORIGINS`
  - best when running behind nginx, Caddy, Traefik, Cloudflare, or another reverse proxy

In strict mode, the allowlist affects:

- generated public links
- OAuth callback URLs
- ShareX config URLs
- browser-session CSRF/origin checks

If request-derived origin data is missing, malformed, or untrusted, the app falls back to `BASE_URL`.

### Reverse Proxy Hardening

If you run imghost behind a real reverse proxy, use all of these together:

```env
PUBLIC_ORIGIN_ENABLED=true
TRUSTED_PUBLIC_ORIGINS=https://your-public-host.example
TRUSTED_PROXY_CIDRS_ENABLED=true
TRUSTED_PROXY_CIDRS=127.0.0.1/32,172.16.0.0/12
```

Guidance:

- `TRUSTED_PUBLIC_ORIGINS` should list the exact public hosts users visit
- `TRUSTED_PROXY_CIDRS` should list the immediate proxy peers that connect to imghost, not end-user IPs
- if `TRUSTED_PROXY_CIDRS_ENABLED=false`, forwarded headers stay permissive by design for local/self-hosted ease of use
- if `TRUSTED_PROXY_CIDRS_ENABLED=true`, only peers inside `TRUSTED_PROXY_CIDRS` may influence forwarded host/proto handling

## One-Machine vs Multi-Machine Docker

The same Compose file supports both:

- single-machine defaults:
  - `POSTGRES_HOST=postgres`
  - `REDIS_URL=redis://redis:6379/0`
  - `S3_ENDPOINT_URL=http://garage:3900`
- remote service overrides:
  - set `POSTGRES_HOST` to a reachable hostname/IP
  - set `REDIS_URL` to a reachable Redis instance
  - set `S3_ENDPOINT_URL` to the remote Garage/S3 endpoint

This makes the app container portable, but `garage-init` still assumes Garage is part of the local Compose stack. If Garage lives elsewhere, initialize/manage it separately.

## Environment Files

Application/runtime defaults:

- [`.env.example`](/home/james/imghost/.env.example)

Docker/infra defaults:

- [`docker/.env.example.beginner`](/home/james/imghost/docker/.env.example.beginner)
- [`docker/.env.example`](/home/james/imghost/docker/.env.example)

`docker/.env` is ignored by git and is the file Compose should use locally.

## Auth And Session Notes

- Passwords are hashed with bcrypt.
- Session cookies are `HttpOnly` and `SameSite=Lax`.
- `Secure` is enabled automatically when `BASE_URL` is HTTPS, and can be overridden with `SESSION_COOKIE_SECURE=true|false`.
- Session auth is hybrid:
  - Redis-backed when Redis is healthy
  - signed-cookie fallback when Redis is unavailable
- Logout still clears the browser cookie if Redis is down, but Redis-backed revocation semantics only apply while Redis is healthy.
- ShareX config download now works from either a browser session or bearer API key auth.
- Because API keys are stored hash-only, browser-session ShareX download rotates or auto-issues the user API key before embedding it into the exported `.sxcu` file.

## Google OAuth Setup

Google sign-in is optional and stays disabled unless all three settings are configured:

```env
GOOGLE_OAUTH_ENABLED=true
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
```

When creating the OAuth client in Google Cloud Console:

- application type: `Web application`
- `Authorized JavaScript origins`: leave blank
- `Authorized redirect URIs`: add your public callback URL exactly:

```text
https://your-domain.example/auth/google/callback
```

Important behavior:

- `BASE_URL` must match the same public origin users actually visit
- the callback URL must match Google Console exactly, including scheme and hostname
- in strict public-origin mode, the request or forwarded origin must also resolve to a trusted public origin or the callback URL falls back to `BASE_URL`
- when `ALLOW_REGISTRATION=false`, existing linked Google users may still sign in, but Google cannot create new accounts
- if a Google account email already belongs to an existing local account, imghost does not auto-merge it; the user must sign in locally first and then connect Google from `/settings`
- Google is just an extra sign-in method; users can set a local password later from `/settings`

## Queue And Rate-Limit Notes

- The beginner Docker stack uses `docker/docker-compose.beginner.yml` with `TASK_QUEUE_MODE=async` and `REDIS_MODE=disabled`, so background jobs run in-process inside the app container.
- The beginner Docker stack connects directly to Postgres.
- The advanced Docker stack routes app, worker, and scheduler DB traffic through PgBouncer.
- The beginner no-Redis stack does not run a separate worker or scheduler service.
- `TASK_QUEUE_MODE=redis` enables Redis-backed task dispatch.
- `TASK_WORKER_ENABLED=false` is intended for the web app container.
- `TASK_WORKER_ENABLED=true` is intended for dedicated worker containers.
- The preferred split worker commands are:
  - `python -m imghost run-worker-thumbnails`
  - `python -m imghost run-worker-cleanup`
  - `python -m imghost run-worker-default`
- The scheduler command is `python -m imghost run-scheduler`.
- `python -m imghost run-worker` remains available as a generic worker command and uses `TASK_WORKER_QUEUES`.
- The web app process runs as the `app` role and does not perform startup thumbnail recovery.
- Startup thumbnail recovery belongs to the thumbnail worker role.
- Scheduled cleanup enqueueing belongs to the scheduler role.
- If Redis is unavailable at runtime:
  - sessions degrade to signed-cookie validation
  - upload rate limiting falls back to in-process memory
  - queued jobs fall back to in-process async execution

## Health And Runtime Status

- `/health/live` returns a simple liveness response for process-level checks.
- `/health/ready` returns a low-noise readiness snapshot covering database, storage, Redis reachability, subsystem modes, worker state, and task queue status.
- `/metrics` returns Prometheus text-format telemetry metrics.
- `/api/v1/admin/runtime-status` returns a richer admin-only operational snapshot including process role, worker and scheduler state, trusted public origins, queue status, and Redis subsystem degradation status.
- Redis observability is transition-oriented rather than per-operation noisy, so degraded and recovered states are logged while repeated fallback behavior is suppressed.

### Metrics

Metrics now live inside the `telemetry/` package as a dedicated sibling subsystem to the audit/log event pipeline.

Current `/metrics` coverage includes:

- HTTP request count and duration
- upload results and uploaded bytes
- thumbnail job results and duration
- auth and OAuth event counters
- Redis-backed subsystem degraded/recovered state
- worker-running and task-enqueue counters

Operational guidance:

- expose `/metrics` only to Prometheus or another trusted scraper
- prefer reverse-proxy restriction rather than app-layer auth
- `/metrics` is intentionally excluded from its own HTTP request counters to avoid scrape noise

## URL Generation

Absolute URLs prefer the current request origin in this order:

1. trusted forwarded proto/host
2. direct request scheme/host
3. `BASE_URL`

In strict public-origin mode, request-derived origins are only used if they match:

- `TRUSTED_PUBLIC_ORIGINS`
- normalized `BASE_URL` is also trusted as the default fallback origin

This lets one hardened deployment serve multiple public domains correctly while still falling back safely when the request origin is missing, malformed, or hostile.

## Development

Run tests with:

```bash
uv run pytest -q
```

By default, the test suite now forces `DATABASE_URL` to a dedicated local test database:

```env
TEST_DATABASE_URL=postgresql://imghost:imghost@localhost:5432/imghost_test
```

The test harness will refuse to run against database names that do not look like test databases. Do not point it at the live `imghost` database.

Current full suite status at the time these docs were updated:

- `482 passed`

The test suite uses PostgreSQL and truncates tables between tests. Run it only against a dedicated test database.

## Related Docs

- [STATUS.md](/home/james/imghost/STATUS.md)
- [DB.md](/home/james/imghost/DB.md)
- [COULDIMPROVE.md](/home/james/imghost/COULDIMPROVE.md)
- [DESIGN.md](/home/james/imghost/plan/DESIGN.md)
