# Background Jobs

Thumbnail generation is the primary background job system today.

## Queue backends

Implemented in [`src/imghost/tasks.py`](/home/james/imghost/src/imghost/tasks.py).

Supported modes:

- `sync`
- `async`
- `redis`

## Backend behavior

### `sync`

- jobs run immediately in the request/process context

### `async`

- jobs run in an in-process asyncio queue
- no cross-process durability

### `redis`

- jobs are pushed into Redis lists
- a worker process can consume them
- queue names currently include `default` and `thumbnails`

## Redis fallback behavior

If Redis-backed tasks are configured but Redis becomes unavailable:

- enqueue falls back to the in-process async queue
- dequeue workers log degraded mode and retry later
- overall behavior becomes process-local until Redis recovers

## Worker process

CLI command:

- `python -m imghost run-worker`

Docker service:

- `worker`

The worker process starts the task queue and then sleeps, allowing the queue backend to run the actual consumers.

## Recovery behavior

On app startup:

- pending thumbnails are re-enqueued

CLI:

- `retry-thumbnails` re-enqueues failed thumbnails as well

## Observability

Low-noise events include:

- worker started/stopped
- task subsystem degraded/recovered
- thumbnail recovery summary
- task failures

