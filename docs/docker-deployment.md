# Docker Deployment

There are two Docker stacks:

- beginner: [`docker/docker-compose-beginner.yml`](/home/james/imghost/docker/docker-compose-beginner.yml)
- advanced: [`docker/docker-compose.yml`](/home/james/imghost/docker/docker-compose.yml)

## Services

Beginner stack:

- `app`
  Runs the FastAPI web application through [`docker/scripts/start-app.sh`](/home/james/imghost/docker/scripts/start-app.sh).
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
- `redis`
  Redis for optional sessions, rate limits, task queues, and scheduler leases.

## Startup flow

In the advanced stack, the app, workers, and scheduler depend on:

- healthy Postgres
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

This behavior comes from [`docker/scripts/start-app.sh`](/home/james/imghost/docker/scripts/start-app.sh) and means web-container startup has a storage-bootstrap side effect in Garage mode.

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

The advanced stack uses [`docker/.env.example`](/home/james/imghost/docker/.env.example). The beginner stack uses [`docker/.env.example.beginner`](/home/james/imghost/docker/.env.example.beginner).

The advanced stack injects additional derived values like `DATABASE_URL` into app, worker, and scheduler services.

Notably:

- `TASK_WORKER_ENABLED` is overridden per service in Compose
- `DATABASE_URL` is built from `POSTGRES_*` values
- Redis auth is handled by `REDIS_PASSWORD` plus `REDIS_URL`

The beginner stack forces:

- `REDIS_MODE=disabled`
- `TASK_QUEUE_MODE=async`
- no separate worker or scheduler container

## Advanced Redis behavior in Docker

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
