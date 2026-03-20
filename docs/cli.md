# CLI

The project exposes operational commands through:

```bash
python -m imghost <subcommand>
```

The current parser lives in [`src/imghost/__main__.py`](/home/james/imghost/src/imghost/__main__.py).

## Commands

### `prune`

Deletes expired albums and their media.

Examples:

```bash
python -m imghost prune
python -m imghost prune --dry-run
```

The command prints:

- deleted or candidate album count
- item count
- bytes freed
- affected album IDs when present

### `retry-thumbnails`

Re-enqueues pending, processing, and failed thumbnails, waits for the queue to drain, and prints how many thumbnails were re-enqueued.

Example:

```bash
python -m imghost retry-thumbnails
```

### `init-storage`

Initializes the configured storage backend.

Example:

```bash
python -m imghost init-storage
```

For the current S3-compatible backend this ensures the configured bucket exists.

### `run-worker`

Starts the background worker process used for Redis-backed task consumption.

Example:

```bash
python -m imghost run-worker
```

This is the command used by the Docker `worker` service.

### `create-user`

Creates a user directly in the database.

Example:

```bash
python -m imghost create-user --username alice --email alice@example.com
```

Optional flags:

- `--admin`
- `--quota-bytes <int>`

Important note:

- CLI-created users start with `password_hash=None`
- local password login is only possible after setting a password later through the app or admin flows

### `issue-api-key`

Issues or rotates a user API key and prints the raw new key.

Example:

```bash
python -m imghost issue-api-key --user-id <user-id>
```

## Docker startup interaction

When the app container starts with `STORAGE_BACKEND=garage`, [`docker/scripts/start-app.sh`](/home/james/imghost/docker/scripts/start-app.sh) runs:

```bash
python -m imghost init-storage
```

before starting uvicorn.

That means `init-storage` is both:

- a manual operator command
- part of normal Garage-backed web-container startup
