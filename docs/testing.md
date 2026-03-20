# Testing

## Test command

```bash
uv run pytest -q
```

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
- stale-session cleanup
- API keys and ShareX behavior
- Redis fallback behavior
- proxy/origin handling
- media processors
- storage backends
- ZIP streaming
- runtime status
- admin operations

## Important warning

Do not point tests at the live `imghost` database.

