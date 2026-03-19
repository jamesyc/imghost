# imghost

Self-hosted image and video hosting prototype built with FastAPI and PostgreSQL.

## Storage Modes

The app supports two storage backends selected with `STORAGE_BACKEND`:

- `filesystem`: store originals and thumbnails under `IMGHOST_DATA_DIR`
- `garage`: store originals and thumbnails in a Garage S3-compatible bucket

`filesystem` is the simplest choice for local development. `garage` is the intended Docker Compose deployment path.

## First-Run Setup

1. Copy `.env.example` to `.env`
2. Copy `docker/.env.example` to `docker/.env`
3. Replace all placeholder secrets in both files
4. Start the stack:

```bash
docker compose -f docker/docker-compose.yml --env-file docker/.env up --build
```

On the default Garage-backed setup:

- `postgres` initializes from `db/init/001-init.sql`
- `garage` starts with config generated from environment variables
- `garage-init` performs one-time idempotent bootstrap:
  - assigns single-node layout if needed
  - imports the S3 key from the app env file
  - creates the bucket if missing
  - grants the key access to the bucket
- `app` waits for `garage-init`, runs `python -m imghost init-storage`, then starts

Environment file split:

- `.env`: application/runtime settings passed into the Python app
- `docker/.env`: Docker/Compose infrastructure settings such as Postgres ports/passwords and Garage cluster bootstrap tokens

## Switching to Filesystem Storage

To run without Garage:

```env
STORAGE_BACKEND=filesystem
IMGHOST_DATA_DIR=./data
```

You can then start only the app and Postgres services, or leave Garage in the Compose file unused.

## Development

Run tests with:

```bash
uv run pytest -q
```

The test suite uses PostgreSQL and truncates tables between tests.

## Notes

- PostgreSQL is the source of truth for application state.
- Redis is not currently part of the prototype Compose file.
- Garage bootstrap is intended to be idempotent, but the Docker/Garage startup path should be validated in a real container run before production use.
