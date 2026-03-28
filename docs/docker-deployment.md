# Docker Deployment

There are two main Docker stacks plus one optional companion override:

- beginner: [`compose.beginner.yaml`](/home/james/imghost/compose.beginner.yaml)
- advanced pull-based: [`compose.yaml`](/home/james/imghost/compose.yaml)
- advanced local-build variant: [`compose.build.yaml`](/home/james/imghost/compose.build.yaml)
- optional nginx companion: [`compose.with-nginx.yaml`](/home/james/imghost/compose.with-nginx.yaml)

## Services

Beginner stack:

- `app`
  Runs the FastAPI web application through [`docker/entrypoints/start-app.sh`](/home/james/imghost/docker/entrypoints/start-app.sh).
- `postgres`
  PostgreSQL state store.
- `garage`
  S3-compatible object storage.
- `garage-init`
  One-shot bootstrap container that configures the Garage layout, key import, and bucket permissions.

Advanced stack adds:

- `worker-thumbnails`
  Runs `python -m imghost run-worker-thumbnails`.
- `worker-cleanup`
  Runs `python -m imghost run-worker-cleanup`.
- `worker-default`
  Runs `python -m imghost run-worker-default`.
- `scheduler`
  Runs `python -m imghost run-scheduler`.
- `pgbouncer`
  Transaction-pooling proxy in front of PostgreSQL for the app, workers, and scheduler.
- `redis`
  Redis for optional sessions, rate limits, task queues, and scheduler leases.

Optional nginx companion adds:

- `nginx`
  Reverse proxy container that terminates HTTPS and proxies to `app:8000` using [`docker/nginx/nginx-site.conf`](/home/james/imghost/docker/nginx/nginx-site.conf).

## Startup flow

In the advanced stack, the app, workers, and scheduler depend on:

- healthy Postgres
- healthy PgBouncer
- healthy Redis
- completed `garage-init`

In the beginner stack, the app depends on healthy Postgres and completed `garage-init`.

The app process also runs the normal FastAPI lifespan startup, which:

- connects to Postgres
- checks Redis startup readiness when Redis is enabled
- starts the task queue backend
- does not run thumbnail recovery unless the process is the thumbnail worker role

Before uvicorn starts, the app container entrypoint script also runs:

- `python -m imghost init-storage` when `STORAGE_BACKEND=garage`

This behavior comes from [`docker/entrypoints/start-app.sh`](/home/james/imghost/docker/entrypoints/start-app.sh) and means web-container startup has a storage-bootstrap side effect in Garage mode.

## Health probe guidance

For container orchestration and reverse proxies:

- use `GET /health/live` as the liveness probe
- use `GET /health/ready` as the readiness probe

Expected readiness behavior:

- `200` when database and storage are healthy and any required Redis dependency is reachable
- `503` when database is unhealthy
- `503` when storage is unhealthy
- `503` when `REDIS_MODE=required` and Redis is configured but unreachable
- `200` when Redis is unreachable but Redis is optional and the app is running in degraded fallback mode

This matches the application’s current fallback model rather than treating every Redis outage as a full traffic blocker.

## Volumes

- `../postgres-data`
  Persistent Postgres data directory
- `garage-meta`
  Garage metadata
- `garage-data`
  Garage object data

Advanced stack only:

- `redis-data`
  Persistent Redis AOF data

## Compose env behavior

The advanced stack uses [`.env.example`](/home/james/imghost/.env.example) copied to `.env`. The beginner stack uses [`.env.example.beginner`](/home/james/imghost/.env.example.beginner) copied to `.env.beginner`.

The advanced stack can either build locally or pull a published image, using:

- `APP_IMAGE` (defaults to `ghcr.io/jamesyc/imghost`)
- `APP_IMAGE_TAG` (defaults to `latest`)

The advanced stack injects additional derived values like `DATABASE_URL` into app, worker, and scheduler services.

Notably:

- `TASK_WORKER_ENABLED` is overridden per service in Compose
- `DATABASE_URL` points app services at `pgbouncer:5432`
- `DATABASE_USE_PGBOUNCER=true` is set for the advanced stack
- Redis auth is handled by `REDIS_PASSWORD` plus `REDIS_URL`

The beginner stack is pull-only and forces:

- `DATABASE_USE_PGBOUNCER=false`
- `REDIS_MODE=disabled`
- `TASK_QUEUE_MODE=async`
- no separate worker or scheduler container
- `APP_SCHEDULER_ENABLED=true` so the app process hosts recurring cleanup by default

## Pulling The Published Image

If you want to deploy without building locally, pull first and then start normally.

Beginner stack:

```bash
docker compose -f compose.beginner.yaml --env-file .env.beginner pull
docker compose -f compose.beginner.yaml --env-file .env.beginner up -d
```

Advanced stack:

```bash
docker compose -f compose.yaml --env-file .env pull
docker compose -f compose.yaml --env-file .env up -d
```

The image publish workflow is defined in [publish-image.yml](/home/james/imghost/.github/workflows/publish-image.yml) and pushes multi-arch images to `ghcr.io/jamesyc/imghost`.

## Advanced PgBouncer behavior

The advanced stack keeps PostgreSQL as the backing database, but application traffic goes through PgBouncer first:

- app, workers, and scheduler connect to `pgbouncer:5432`
- PgBouncer connects onward to `postgres:5432`
- PgBouncer runs in `transaction` pool mode
- the Python app disables asyncpg statement caching when `DATABASE_USE_PGBOUNCER=true`

## Optional Compose nginx

The nginx companion is an override file, not a standalone stack. Start it with:

```bash
docker compose \
  -f compose.yaml \
  -f compose.with-nginx.yaml \
  --env-file .env \
  up -d
```

It mounts:

- [`docker/nginx/nginx-site.conf`](/home/james/imghost/docker/nginx/nginx-site.conf)
  Container-facing nginx config with `proxy_pass http://app:8000;`
- `../certs`
  Expected to contain `fullchain.pem` and `privkey.pem`

For host-installed nginx instead, use [`docs/nginx-site.conf`](/home/james/imghost/docs/nginx-site.conf), which proxies to `127.0.0.1:8000` rather than the Compose service name.

## Advanced Redis behavior in Docker

The Redis service starts with:

- `redis-server --appendonly yes` when `REDIS_PASSWORD` is empty
- `redis-server --appendonly yes --requirepass "$REDIS_PASSWORD"` when `REDIS_PASSWORD` is set

In the current stack, `REDIS_PASSWORD` is set in [`.env`](/home/james/imghost/.env), so Redis authentication is enabled.

The Compose healthcheck follows the same auth mode:

- unauthenticated `redis-cli ping` when no password is configured
- authenticated `redis-cli -a "$REDIS_PASSWORD" ping` when a password is configured

## Current Garage bootstrap behavior

[`docker/garage/init.sh`](/home/james/imghost/docker/garage/init.sh) does the following:

- assigns the node to the configured zone/capacity if no role exists yet
- imports the configured S3 key if it does not exist
- creates the bucket if it does not exist
- grants read/write/owner access for the configured key to the bucket

This means the first-run bootstrap state is tied closely to the current env values and persisted Garage volumes.
