# Run Without Docker

This guide covers running `imghost` directly on a machine without Docker.

It is aimed at local development, debugging, and operators who already have Python, PostgreSQL, and optional Redis available outside containers.

## What this guide assumes

- Python 3.14
- `uv` installed
- PostgreSQL available
- Redis available if you want Redis-backed sessions, rate limits, workers, or scheduler leases
- an S3-compatible storage backend available if you are not using a local storage configuration

## 1. Clone the repo

```bash
git clone https://github.com/jamesyc/imghost.git
cd imghost
```

## 2. Install dependencies

```bash
uv sync
```

## 3. Create an env file

For the full configuration template:

```bash
cp .env.example .env
```

For a smaller beginner-oriented starting point:

```bash
cp .env.example.beginner .env
```

Then edit [`.env`](/home/james/imghost/.env) with your actual values.

At minimum, review:

- `BASE_URL`
- `SECRET_KEY`
- `DATABASE_URL`
- `REDIS_URL` if using Redis
- storage settings such as `STORAGE_BACKEND`, `S3_ENDPOINT_URL`, `S3_BUCKET`, `S3_ACCESS_KEY_ID`, and `S3_SECRET_ACCESS_KEY`

## 4. Create and initialize the database

Create a dedicated PostgreSQL database and user, then point `DATABASE_URL` at it.

Example:

```env
DATABASE_URL=postgresql://imghost:change-me@localhost:5432/imghost
```

Load the schema:

```bash
psql "$DATABASE_URL" -f db/init/001-init.sql
```

## 5. Initialize storage

If your configured storage backend needs bootstrap setup, run:

```bash
uv run python -m imghost init-storage
```

For the current S3-compatible storage path, this ensures the configured bucket exists.

## 6. Start the web app

Run the ASGI app with uvicorn:

```bash
uv run uvicorn imghost.main:app --host 0.0.0.0 --port 8000
```

The app will then be available on your configured `BASE_URL`, typically `http://localhost:8000` for local use.

## 7. Start background services when needed

If you use background jobs, start workers and the scheduler in separate processes.

Examples:

```bash
uv run python -m imghost run-worker-thumbnails
uv run python -m imghost run-worker-cleanup
uv run python -m imghost run-worker-default
uv run python -m imghost run-scheduler
```

If you prefer one generic worker process instead of split workers:

```bash
uv run python -m imghost run-worker
```

The advanced deployment shape typically uses the split worker commands.

## 8. Create an initial admin user if needed

Example:

```bash
uv run python -m imghost create-user --username admin --email admin@example.com --password 'change-me-now' --admin
```

You can then log in through the web UI or issue an API key with:

```bash
uv run python -m imghost issue-api-key --user-id <user-id>
```

## 9. Production notes

- Run the app behind a reverse proxy such as nginx, Caddy, or Traefik for real deployments
- Use HTTPS and set `BASE_URL` accordingly
- Use a process manager such as `systemd` to keep the app, workers, and scheduler running
- If Redis is disabled, some runtime behavior falls back to in-process or degraded modes depending on your configuration

## Related docs

- [configuration.md](/home/james/imghost/docs/configuration.md)
- [cli.md](/home/james/imghost/docs/cli.md)
- [background-jobs.md](/home/james/imghost/docs/background-jobs.md)
- [reverse-proxy.md](/home/james/imghost/docs/reverse-proxy.md)
