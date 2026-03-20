# Operations

## Health endpoints

- `GET /health/live`
  Returns plain `ok` if the process is alive.
- `GET /health/ready`
  Returns runtime status plus an `ok` field and responds `503` when the app is not ready.

Readiness currently depends on:

- database health
- storage health
- Redis reachability only when `REDIS_MODE=required` and Redis is configured

## Admin runtime status

- `GET /api/v1/admin/runtime-status`

Requires admin auth and returns:

- database status
- storage status
- Redis configured/reachable status
- Redis subsystem snapshots for sessions, rate limits, and tasks
- worker state
- task queue status and queue depth
- trusted public origins
- forwarded-header policy
- trusted proxy CIDRs

## Logging model

Observability is intentionally low-noise.

Expected logs include:

- Redis subsystem degraded/recovered transitions
- stale session cookie clearing
- rate-limit denials
- thumbnail recovery summaries
- task failures
- worker lifecycle transitions
- suppressed warnings for repeated untrusted-origin cases

## Redis degraded mode

When Redis is down and `REDIS_MODE=auto`:

- sessions fall back to signed-cookie validation
- rate limits fall back to in-memory counters
- Redis task queue falls back to in-process async enqueue

## Current Docker operational caveats

- Postgres data persists under `../postgres-data`
- Redis AOF data persists in the `redis-data` volume
- Garage data persists in `garage-meta` and `garage-data`
- bootstrap-related secret changes may not fully apply by editing env alone when volumes already contain initialized state

