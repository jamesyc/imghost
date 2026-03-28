# Testing

## Test command

```bash
uv run pytest -q
```

Current full-suite baseline:

- 647 collected tests

## Test database safety

The test harness forces the process to use a dedicated test database.

Current default:

```env
TEST_DATABASE_URL=postgresql://imghost:imghost@localhost:5432/imghost_test
```

The safety guard rejects database names that do not look like test databases.

## Required local dependencies

The full suite expects:

- PostgreSQL reachable on localhost

Many tests do not require a live Redis instance because Redis-heavy paths are covered with focused unit tests and fakes.

## Test coverage areas

Current tests cover:

- uploads and album behavior
- auth and sessions
- Google OAuth login, linking, disconnect, and delete-account re-auth
- password minimum-length enforcement on registration, admin create/reset, and current-user change flows
- browser-session CSRF enforcement and exemptions
- anonymous manage-token mutation coverage
- stale-session cleanup
- auth redirect normalization
- API keys and ShareX behavior
- Redis fallback behavior
- Redis session creation and logout behavior during outages
- strict Redis session fail-closed behavior during outages
- proxy/origin handling
- baseline browser security headers and conditional HSTS
- page bootstrap/view-model helpers
- media processors
- storage backends
- ZIP streaming
- liveness and readiness health endpoints
- Prometheus metrics endpoint and metrics middleware
- runtime status
- scheduler behavior and cleanup enqueueing
- bootstrap admin promotion
- admin operations

## Important warning

Do not point tests at the live `imghost` database.
