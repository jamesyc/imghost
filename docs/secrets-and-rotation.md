# Secrets And Rotation

This document describes which secrets are easy to rotate in place and which are effectively bootstrap-time values unless you take explicit migration steps.

## Application `SECRET_KEY`

Used for:

- signing browser-session cookies

Changing it:

- invalidates all existing signed browser-session cookies immediately

Operationally:

- update the env value
- restart the app

## Redis password

Used for:

- authenticating app and worker connections to Redis
- authenticating Redis clients if the Docker Redis service is running with `--requirepass`

Changing it:

- update `REDIS_PASSWORD`
- restart Redis and any clients using it

In this project:

- Docker Redis is configured to use `requirepass` when `REDIS_PASSWORD` is present
- the app resolves passworded Redis URLs automatically when `REDIS_URL` itself does not already embed credentials

## Postgres password

Changing `POSTGRES_PASSWORD` in env is not enough once Postgres has already initialized its persistent data directory.

For an existing database:

1. change the password inside Postgres with SQL
2. update `POSTGRES_PASSWORD` in env
3. restart the dependent services

For a fresh volume:

- setting `POSTGRES_PASSWORD` before first startup is enough

## Garage secrets and tokens

Relevant values:

- `GARAGE_RPC_SECRET`
- `GARAGE_ADMIN_TOKEN`
- `GARAGE_METRICS_TOKEN`
- S3 key material imported by `garage-init`

These are more bootstrap-oriented than normal app env vars because Garage state persists in its own volumes.

For a fresh Garage deployment:

- setting them in env before first bootstrap is enough

For an existing Garage deployment:

- changing them may require explicit Garage-side administrative steps
- changing env alone is not guaranteed to rotate the live cluster state safely

## API keys

User API keys are stored hash-only in PostgreSQL.

Implications:

- existing raw API keys cannot be retrieved later
- browser-session ShareX export and browser-session key reveal work by issuing or rotating a fresh key

## Session model caveat

Sessions are hybrid:

- signed cookies always carry enough data to identify the user
- Redis-backed session records add stronger revocation semantics when Redis is healthy

When Redis is unavailable:

- session auth falls back to signed-cookie validation
- logout still clears the cookie, but server-side revocation guarantees are reduced

