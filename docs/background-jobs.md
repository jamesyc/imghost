# Background Jobs

Thumbnail generation is the primary background job system today.

Current formal tasks also include cleanup and thumbnail recovery.

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
- this is the mode used by the beginner Docker stack

### `redis`

- jobs are pushed into Redis lists
- queue-scoped worker processes can consume them
- queue names currently include `default`, `thumbnails`, and `cleanup`

## Redis fallback behavior

If Redis-backed tasks are configured but Redis becomes unavailable:

- enqueue falls back to the in-process async queue
- dequeue workers log degraded mode and retry later
- overall behavior becomes process-local until Redis recovers

## Worker and scheduler processes

Preferred worker CLI commands:

- `python -m imghost run-worker-thumbnails`
- `python -m imghost run-worker-cleanup`
- `python -m imghost run-worker-default`

Scheduler CLI command:

- `python -m imghost run-scheduler`

Compatibility CLI command:

- `python -m imghost run-worker`

Advanced Docker services:

- `worker-thumbnails`
- `worker-cleanup`
- `worker-default`
- `scheduler`

The worker processes start the task queue and then sleep, allowing the queue backend to run the actual consumers. The scheduler only enqueues recurring jobs and does not execute cleanup inline.

## Recovery behavior

On thumbnail worker startup:

- pending thumbnails are re-enqueued

CLI:

- `retry-thumbnails` re-enqueues failed thumbnails as well

## Observability

Low-noise events include:

- worker started/stopped
- scheduler state in admin runtime status
- task subsystem degraded/recovered
- thumbnail recovery summary
- task failures
