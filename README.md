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

The current UI is intentionally simple and exists mainly to exercise the backend:

- `/`:
  - sign in
  - register
  - anonymous upload
- `/dashboard`:
  - account summary
  - password change
  - API key generation
  - ShareX export
  - authenticated upload
  - owned album management
- `/admin`:
  - user management
  - album management
  - runtime config
  - audit log
- `/album-tools`:
  - token-based management for anonymous/public albums

## Docker Setup

The Docker setup lives under [`docker/`](/home/james/imghost/docker).

Main files:

- [`docker/docker-compose.yml`](/home/james/imghost/docker/docker-compose.yml)
- [`docker/.env.example`](/home/james/imghost/docker/.env.example)
- [`docker/.env`](/home/james/imghost/docker/.env)

The Compose project name is `imghost`, so containers come up as:

- `imghost-app-1`
- `imghost-worker-1`
- `imghost-postgres-1`
- `imghost-redis-1`
- `imghost-garage-1`
- `imghost-garage-init-1`

Start the stack with:

```bash
docker compose -f docker/docker-compose.yml --env-file docker/.env up --build -d
```

Current default public base URL in the Docker env file:

```env
BASE_URL=https://imghost.jamesyc.com
```

Trusted public origins are configured separately, for example:

```env
TRUSTED_PUBLIC_ORIGINS=https://imghost.jamesyc.com,https://imghost.002015.xyz
```

## One-Machine vs Multi-Machine Docker

The same Compose file supports both:

- single-machine defaults:
  - `POSTGRES_HOST=postgres`
  - `POSTGRES_CONNECT_PORT=5432`
  - `REDIS_URL=redis://redis:6379/0`
  - `S3_ENDPOINT_URL=http://garage:3900`
- remote service overrides:
  - set `POSTGRES_HOST` to a reachable hostname/IP
  - set `POSTGRES_CONNECT_PORT` if needed
  - set `REDIS_URL` to a reachable Redis instance
  - set `S3_ENDPOINT_URL` to the remote Garage/S3 endpoint

This makes the app container portable, but `garage-init` still assumes Garage is part of the local Compose stack. If Garage lives elsewhere, initialize/manage it separately.

## Environment Files

Application/runtime defaults:

- [`.env.example`](/home/james/imghost/.env.example)

Docker/infra defaults:

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
- ShareX config download still requires API-key-authenticated requests, even if the user has a valid browser session.

## Queue And Rate-Limit Notes

- `TASK_QUEUE_MODE=redis` enables Redis-backed thumbnail dispatch.
- `TASK_WORKER_ENABLED=false` is intended for the web app container.
- `TASK_WORKER_ENABLED=true` is intended for the dedicated worker container.
- If Redis is unavailable at runtime:
  - sessions degrade to signed-cookie validation
  - upload rate limiting falls back to in-process memory
  - thumbnail jobs fall back to in-process async execution

## Health And Runtime Status

- `/health/live` returns a simple liveness response for process-level checks.
- `/health/ready` returns a low-noise readiness snapshot covering database, storage, Redis reachability, subsystem modes, worker state, and task queue status.
- `/api/v1/admin/runtime-status` returns a richer admin-only operational snapshot including trusted public origins, worker/task state, and Redis subsystem degradation status.
- Redis observability is transition-oriented rather than per-operation noisy, so degraded and recovered states are logged while repeated fallback behavior is suppressed.

## URL Generation

Absolute URLs now prefer the request's public origin:

- `X-Forwarded-Proto`
- `X-Forwarded-Host`
- request host/scheme

That origin is only used if it matches the configured trusted allowlist:

- `TRUSTED_PUBLIC_ORIGINS`
- normalized `BASE_URL` is also trusted as the default fallback origin

If the forwarded or direct request origin is missing, malformed, or untrusted, the app falls back to `BASE_URL`.

This means one deployment can serve multiple public domains correctly for normal request-driven responses.

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

- `81 passed`

The test suite uses PostgreSQL and truncates tables between tests. Run it only against a dedicated test database.

## Related Docs

- [STATUS.md](/home/james/imghost/STATUS.md)
- [IMPLEMENTED_DIFFERENCES.md](/home/james/imghost/IMPLEMENTED_DIFFERENCES.md)
- [DB.md](/home/james/imghost/DB.md)
- [COULDIMPROVE.md](/home/james/imghost/COULDIMPROVE.md)
- [DESIGN.md](/home/james/imghost/DESIGN.md)
- [DESIGN2.md](/home/james/imghost/DESIGN2.md)
