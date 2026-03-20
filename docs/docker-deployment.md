# Docker Deployment

The Docker stack is defined in [`docker/docker-compose.yml`](/home/james/imghost/docker/docker-compose.yml).

## Services

- `app`
  Runs the FastAPI web application through [`docker/scripts/start-app.sh`](/home/james/imghost/docker/scripts/start-app.sh).
- `worker`
  Runs `python -m imghost run-worker` for Redis-backed background job consumption.
- `postgres`
  PostgreSQL state store.
- `redis`
  Redis for optional sessions, rate limits, and task queue operations.
- `garage`
  S3-compatible object storage.
- `garage-init`
  One-shot bootstrap container that configures the Garage layout, key import, and bucket permissions.

## Startup flow

The app and worker both depend on:

- healthy Postgres
- healthy Redis
- completed `garage-init`

The app process also runs the normal FastAPI lifespan startup, which:

- connects to Postgres
- checks Redis startup readiness when Redis is enabled
- starts the task queue backend
- re-enqueues pending thumbnails

Before uvicorn starts, the app container entrypoint script also runs:

- `python -m imghost init-storage` when `STORAGE_BACKEND=garage`

This behavior comes from [`docker/scripts/start-app.sh`](/home/james/imghost/docker/scripts/start-app.sh) and means web-container startup has a storage-bootstrap side effect in Garage mode.

## Volumes

- `../postgres-data`
  Persistent Postgres data directory
- `redis-data`
  Persistent Redis AOF data
- `garage-meta`
  Garage metadata
- `garage-data`
  Garage object data

## Compose env behavior

`app` and `worker` load `docker/.env`, then Compose injects additional derived values like `DATABASE_URL`.

Notably:

- `TASK_WORKER_ENABLED` is overridden per service in Compose
- `DATABASE_URL` is built from `POSTGRES_*` values
- Redis auth is handled by `REDIS_PASSWORD` plus `REDIS_URL`

## Current Redis behavior in Docker

The Redis service starts with:

- `redis-server --appendonly yes` when `REDIS_PASSWORD` is empty
- `redis-server --appendonly yes --requirepass "$REDIS_PASSWORD"` when `REDIS_PASSWORD` is set

In the current stack, `REDIS_PASSWORD` is set in [`docker/.env`](/home/james/imghost/docker/.env), so Redis authentication is enabled.

The Compose healthcheck follows the same auth mode:

- unauthenticated `redis-cli ping` when no password is configured
- authenticated `redis-cli -a "$REDIS_PASSWORD" ping` when a password is configured

## Current Garage bootstrap behavior

[`docker/scripts/garage-init.sh`](/home/james/imghost/docker/scripts/garage-init.sh) does the following:

- assigns the node to the configured zone/capacity if no role exists yet
- imports the configured S3 key if it does not exist
- creates the bucket if it does not exist
- grants read/write/owner access for the configured key to the bucket

This means the first-run bootstrap state is tied closely to the current env values and persisted Garage volumes.
