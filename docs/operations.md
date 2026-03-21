# Operations

## Health endpoints

- `GET /health/live`
  Returns plain `ok` if the process is alive.
- `GET /health/ready`
  Returns runtime status plus an `ok` field and responds `503` when the app is not ready.

Liveness is intentionally shallow:

- it reports whether the web process is up
- it does not depend on database, storage, Redis, or worker health
- it is the right probe for “should the process be restarted”

Readiness currently depends on:

- database health
- storage health
- Redis reachability only when `REDIS_MODE=required` and Redis is configured

Readiness does not fail just because Redis is unavailable in optional mode. In `REDIS_MODE=auto`, the app can still be ready while sessions, rate limits, and tasks fall back to degraded but functional behavior.

Practical probe meaning:

- use `/health/live` for container/process liveness checks
- use `/health/ready` for traffic readiness checks
- expect `/health/ready` to return `503` for hard blockers only

## Readiness response contract

`GET /health/ready` returns the runtime-status payload with a top-level `ok` field added by the endpoint.

Stable fields to rely on:

- `ok`
- `database.ok`
- `storage.ok`
- `redis.configured`
- `redis.reachable`
- `tasks.mode`

The payload also includes worker state, Redis subsystem snapshots, trusted-origin/proxy settings, and task queue details for debugging.

## Current tested health scenarios

The test suite now covers:

- `/health/live` returns plain text `ok`
- `/health/live` does not depend on runtime-status checks
- `/health/ready` healthy path
- `/health/ready` database failure
- `/health/ready` storage failure
- `/health/ready` optional Redis outage
- `/health/ready` required Redis outage
- `/health/ready` response payload remains useful when not ready

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

That degraded state should still be reflected as ready by `/health/ready` unless Redis is configured as required.

## Current Docker operational caveats

- Postgres data persists under `../postgres-data`
- Redis AOF data persists in the `redis-data` volume
- Garage data persists in `garage-meta` and `garage-data`
- bootstrap-related secret changes may not fully apply by editing env alone when volumes already contain initialized state
