from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from imghost.config import Settings
from imghost.main import app
from imghost.models import User, utcnow
from imghost.telemetry.state import ObservabilityState
from imghost.rate_limits import InMemoryRateLimiter, RedisRateLimiter
from imghost.redis_support import RedisHandle
from imghost.sessions import RedisBackedSessionBackend
from imghost.sessions import SessionBackendUnavailable
from imghost.sessions import _decode_signed_token
from imghost.tasks import RedisTaskQueue, TaskContext

from .helpers import browser_session_headers, create_user_and_api_key, set_user_password


class FakeRedis:
    def __init__(self) -> None:
        self.fail = False
        self.values: dict[str, str] = {}
        self.hashes: dict[str, dict[str, int]] = {}
        self.lists: dict[str, list[str]] = {}

    def _check(self) -> None:
        if self.fail:
            raise OSError("redis down")

    async def ping(self) -> bool:
        self._check()
        return True

    async def get(self, key: str) -> str | None:
        self._check()
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self._check()
        self.values[key] = value
        return True

    async def delete(self, key: str) -> int:
        self._check()
        existed = key in self.values
        self.values.pop(key, None)
        self.hashes.pop(key, None)
        self.lists.pop(key, None)
        return 1 if existed else 0

    async def incr(self, key: str) -> int:
        self._check()
        value = int(self.values.get(key, "0")) + 1
        self.values[key] = str(value)
        return value

    async def expire(self, key: str, seconds: int) -> bool:
        self._check()
        return True

    async def hgetall(self, key: str) -> dict[str, str]:
        self._check()
        return {field: str(value) for field, value in self.hashes.get(key, {}).items()}

    async def hdel(self, key: str, *fields: str) -> int:
        self._check()
        bucket = self.hashes.get(key, {})
        deleted = 0
        for field in fields:
            if field in bucket:
                deleted += 1
                del bucket[field]
        return deleted

    async def hincrby(self, key: str, field: str, amount: int) -> int:
        self._check()
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = bucket.get(field, 0) + amount
        return bucket[field]

    async def rpush(self, key: str, value: str) -> int:
        self._check()
        bucket = self.lists.setdefault(key, [])
        bucket.append(value)
        return len(bucket)

    async def blpop(self, keys: list[str], timeout: int = 0):
        self._check()
        for key in keys:
            bucket = self.lists.get(key)
            if bucket:
                return key, bucket.pop(0)
        await asyncio.sleep(0)
        return None

    async def llen(self, key: str) -> int:
        self._check()
        return len(self.lists.get(key, []))

    async def aclose(self) -> None:
        return None


class DummyRuntimeConfig:
    def __init__(self, values: dict[str, int | bool]) -> None:
        self.values = values

    async def get_value(self, key: str) -> int | bool:
        return self.values[key]


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "base_url": "https://testserver",
        "public_origin_enabled": True,
        "trusted_public_origins": ("https://testserver",),
        "trusted_proxy_cidrs_enabled": False,
        "trusted_proxy_cidrs": (),
        "database_url": "postgresql://test",
        "data_dir": Path("/tmp/imghost-test"),
        "redis_url": None,
        "redis_password": None,
        "redis_mode": "auto",
        "redis_prefix": "imghost",
        "storage_backend": "filesystem",
        "s3_endpoint_url": None,
        "s3_access_key_id": None,
        "s3_secret_access_key": None,
        "s3_bucket": None,
        "s3_region": "garage",
        "secret_key": "secret",
        "session_cookie_name": "imghost_session",
        "session_cookie_secure": True,
        "session_redis_fail_closed": False,
        "session_remember_days": 30,
        "max_upload_bytes": 50 * 1024 * 1024,
        "anon_expiry_hours": 24,
        "max_pixel_megapixels": 50,
        "default_user_quota_bytes": 2 * 1024 * 1024 * 1024,
        "server_quota_bytes": 0,
        "video_thumb_frames": 10,
        "task_queue_mode": "async",
        "task_worker_enabled": True,
        "thumbnail_worker_count": 1,
    }
    values.update(overrides)
    return Settings(**values)


def make_user() -> User:
    now = utcnow()
    return User(
        id="user-1",
        username="redis-user",
        email="redis@example.com",
        password_hash=None,
        is_admin=False,
        suspended=False,
        quota_bytes=None,
        rate_limit_rpm=None,
        rate_limit_bph=None,
        created_at=now,
        updated_at=now,
    )


def test_redis_session_backend_falls_back_to_cookie_when_redis_is_down() -> None:
    fake = FakeRedis()
    fake.fail = True
    settings = make_settings(redis_url="redis://fake")
    backend = RedisBackedSessionBackend(
        settings,
        RedisHandle(settings, client_factory=lambda _: fake, cooldown_seconds=0),
        ObservabilityState(),
    )

    token, _ = asyncio.run(backend.create_session(make_user(), remember_me=True))

    fake.fail = False
    assert asyncio.run(backend.resolve_user(token)) == "user-1"


def test_redis_session_backend_uses_redis_when_available_and_gracefully_falls_back_during_outage() -> None:
    fake = FakeRedis()
    settings = make_settings(redis_url="redis://fake")
    backend = RedisBackedSessionBackend(
        settings,
        RedisHandle(settings, client_factory=lambda _: fake, cooldown_seconds=0),
        ObservabilityState(),
    )

    token, _ = asyncio.run(backend.create_session(make_user(), remember_me=True))
    assert any(key.endswith("session:") is False for key in fake.values)

    fake.fail = True
    assert asyncio.run(backend.resolve_user(token)) == "user-1"

    fake.fail = False
    session_key = next(key for key in fake.values if ":session:" in key)
    asyncio.run(fake.delete(session_key))
    assert asyncio.run(backend.resolve_user(token)) is None


def test_redis_session_backend_strict_mode_rejects_session_creation_when_redis_is_down() -> None:
    fake = FakeRedis()
    fake.fail = True
    settings = make_settings(redis_url="redis://fake", session_redis_fail_closed=True)
    backend = RedisBackedSessionBackend(
        settings,
        RedisHandle(settings, client_factory=lambda _: fake, cooldown_seconds=0),
        ObservabilityState(),
    )

    with pytest.raises(SessionBackendUnavailable, match="Redis-backed sessions"):
        asyncio.run(backend.create_session(make_user(), remember_me=True))


def test_redis_session_backend_strict_mode_fails_closed_when_redis_goes_down_after_login() -> None:
    fake = FakeRedis()
    settings = make_settings(redis_url="redis://fake", session_redis_fail_closed=True)
    backend = RedisBackedSessionBackend(
        settings,
        RedisHandle(settings, client_factory=lambda _: fake, cooldown_seconds=0),
        ObservabilityState(),
    )

    token, _ = asyncio.run(backend.create_session(make_user(), remember_me=True))
    fake.fail = True

    assert asyncio.run(backend.resolve_user(token)) is None


def test_redis_rate_limiter_enforces_limits_when_available() -> None:
    fake = FakeRedis()
    settings = make_settings(redis_url="redis://fake")
    runtime = DummyRuntimeConfig(
        {
            "rate_limit_user_rpm": 1,
            "rate_limit_user_bph": 100,
            "rate_limit_anon_rpm": 1,
            "rate_limit_anon_bph": 100,
            "rate_limit_global_anon_rpm": 1,
            "rate_limit_global_anon_bph": 100,
        }
    )
    limiter = RedisRateLimiter(
        runtime,
        RedisHandle(settings, client_factory=lambda _: fake, cooldown_seconds=0),
        InMemoryRateLimiter(runtime),
        ObservabilityState(),
    )
    user = make_user()

    asyncio.run(limiter.enforce_upload_limits(actor_key=user.id, byte_count=10, user=user))
    with pytest.raises(HTTPException) as denied:
        asyncio.run(limiter.enforce_upload_limits(actor_key=user.id, byte_count=10, user=user))
    assert denied.value.status_code == 429


def test_redis_rate_limiter_falls_back_to_in_memory_when_redis_is_down() -> None:
    fake = FakeRedis()
    fake.fail = True
    settings = make_settings(redis_url="redis://fake")
    runtime = DummyRuntimeConfig(
        {
            "rate_limit_user_rpm": 1,
            "rate_limit_user_bph": 100,
            "rate_limit_anon_rpm": 1,
            "rate_limit_anon_bph": 100,
            "rate_limit_global_anon_rpm": 1,
            "rate_limit_global_anon_bph": 100,
        }
    )
    limiter = RedisRateLimiter(
        runtime,
        RedisHandle(settings, client_factory=lambda _: fake, cooldown_seconds=0),
        InMemoryRateLimiter(runtime),
        ObservabilityState(),
    )
    user = make_user()

    asyncio.run(limiter.enforce_upload_limits(actor_key=user.id, byte_count=10, user=user))
    with pytest.raises(HTTPException) as denied:
        asyncio.run(limiter.enforce_upload_limits(actor_key=user.id, byte_count=10, user=user))
    assert denied.value.status_code == 429


def test_in_memory_rate_limiter_keeps_anonymous_actor_and_global_limits_distinct() -> None:
    runtime = DummyRuntimeConfig(
        {
            "rate_limit_user_rpm": 0,
            "rate_limit_user_bph": 0,
            "rate_limit_anon_rpm": 2,
            "rate_limit_anon_bph": 100,
            "rate_limit_global_anon_rpm": 3,
            "rate_limit_global_anon_bph": 1000,
        }
    )
    limiter = InMemoryRateLimiter(runtime)

    asyncio.run(limiter.enforce_upload_limits(actor_key="anon-a", byte_count=10, user=None))
    asyncio.run(limiter.enforce_upload_limits(actor_key="anon-b", byte_count=10, user=None))
    asyncio.run(limiter.enforce_upload_limits(actor_key="anon-a", byte_count=10, user=None))

    with pytest.raises(HTTPException) as actor_denied:
        asyncio.run(limiter.enforce_upload_limits(actor_key="anon-a", byte_count=10, user=None))
    assert actor_denied.value.status_code == 429
    assert actor_denied.value.detail == "Upload rate limit exceeded."

    with pytest.raises(HTTPException) as global_denied:
        asyncio.run(limiter.enforce_upload_limits(actor_key="anon-c", byte_count=10, user=None))
    assert global_denied.value.status_code == 429
    assert global_denied.value.detail == "Upload rate limit exceeded."


def test_redis_rate_limiter_recovers_after_redis_returns() -> None:
    fake = FakeRedis()
    settings = make_settings(redis_url="redis://fake")
    runtime = DummyRuntimeConfig(
        {
            "rate_limit_user_rpm": 10,
            "rate_limit_user_bph": 1000,
            "rate_limit_anon_rpm": 10,
            "rate_limit_anon_bph": 1000,
            "rate_limit_global_anon_rpm": 10,
            "rate_limit_global_anon_bph": 1000,
        }
    )
    observability = ObservabilityState()
    limiter = RedisRateLimiter(
        runtime,
        RedisHandle(settings, client_factory=lambda _: fake, cooldown_seconds=0),
        InMemoryRateLimiter(runtime),
        observability,
    )
    user = make_user()

    fake.fail = True
    asyncio.run(limiter.enforce_upload_limits(actor_key=user.id, byte_count=10, user=user))
    status = observability.subsystem_snapshot("rate_limits", configured=True, default_mode="redis")
    assert status["degraded"] is True

    fake.fail = False
    asyncio.run(limiter.enforce_upload_limits(actor_key=user.id, byte_count=10, user=user))
    status = observability.subsystem_snapshot("rate_limits", configured=True, default_mode="redis")
    assert status["degraded"] is False


def test_redis_rate_limiter_uses_per_user_overrides_instead_of_runtime_defaults() -> None:
    fake = FakeRedis()
    settings = make_settings(redis_url="redis://fake")
    runtime = DummyRuntimeConfig(
        {
            "rate_limit_user_rpm": 10,
            "rate_limit_user_bph": 1000,
            "rate_limit_anon_rpm": 10,
            "rate_limit_anon_bph": 1000,
            "rate_limit_global_anon_rpm": 10,
            "rate_limit_global_anon_bph": 1000,
        }
    )
    limiter = RedisRateLimiter(
        runtime,
        RedisHandle(settings, client_factory=lambda _: fake, cooldown_seconds=0),
        InMemoryRateLimiter(runtime),
        ObservabilityState(),
    )
    user = make_user()
    user.rate_limit_rpm = 1
    user.rate_limit_bph = 20

    asyncio.run(limiter.enforce_upload_limits(actor_key=user.id, byte_count=10, user=user))
    with pytest.raises(HTTPException) as denied:
        asyncio.run(limiter.enforce_upload_limits(actor_key=user.id, byte_count=10, user=user))
    assert denied.value.status_code == 429


async def _run_queue(queue: RedisTaskQueue, calls: list[str]) -> None:
    queue.register("demo", lambda value: _record_call(calls, value))
    await queue.start()
    await queue.enqueue("demo", queue="thumbnails", value="ok")
    await queue.join()
    await queue.stop()


async def _record_call(calls: list[str], value: str) -> None:
    calls.append(value)


def test_redis_task_queue_processes_jobs_with_worker_enabled() -> None:
    fake = FakeRedis()
    settings = make_settings(redis_url="redis://fake")
    handle = RedisHandle(settings, client_factory=lambda _: fake, cooldown_seconds=0)
    queue = RedisTaskQueue(
        handle,
        TaskContext(None, None, None),
        ObservabilityState(),
        worker_count=1,
        run_worker=True,
    )  # type: ignore[arg-type]
    calls: list[str] = []

    asyncio.run(_run_queue(queue, calls))

    assert calls == ["ok"]


def test_redis_task_queue_falls_back_to_local_async_queue_when_redis_is_down() -> None:
    fake = FakeRedis()
    fake.fail = True
    settings = make_settings(redis_url="redis://fake")
    handle = RedisHandle(settings, client_factory=lambda _: fake, cooldown_seconds=0)
    queue = RedisTaskQueue(
        handle,
        TaskContext(None, None, None),
        ObservabilityState(),
        worker_count=1,
        run_worker=False,
    )  # type: ignore[arg-type]
    calls: list[str] = []

    asyncio.run(_run_queue(queue, calls))

    assert calls == ["ok"]


def test_redis_task_queue_marks_tasks_subsystem_degraded_and_then_recovers() -> None:
    fake = FakeRedis()
    settings = make_settings(redis_url="redis://fake")
    observability = ObservabilityState()
    handle = RedisHandle(settings, client_factory=lambda _: fake, cooldown_seconds=0)
    queue = RedisTaskQueue(
        handle,
        TaskContext(None, None, None),
        observability,
        worker_count=1,
        run_worker=False,
    )  # type: ignore[arg-type]
    calls: list[str] = []
    queue.register("demo", lambda value: _record_call(calls, value))

    async def scenario() -> None:
        await queue.start()
        fake.fail = True
        await queue.enqueue("demo", queue="thumbnails", value="fallback")
        await queue.join()
        degraded = observability.subsystem_snapshot("tasks", configured=True, default_mode="redis")
        assert degraded["degraded"] is True
        assert calls == ["fallback"]

        fake.fail = False
        await queue.enqueue("demo", queue="thumbnails", value="redis")
        await queue.join()
        recovered = observability.subsystem_snapshot("tasks", configured=True, default_mode="redis")
        assert recovered["degraded"] is False
        assert calls == ["fallback"]
        assert fake.lists[handle.prefixed("queue:thumbnails")] == [
            '{"task_name":"demo","kwargs":{"value":"redis"}}'
        ]
        await queue.stop()

    asyncio.run(scenario())


def test_app_session_auth_gracefully_falls_back_when_redis_is_down(tmp_path, monkeypatch) -> None:
    fake = FakeRedis()
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setenv("REDIS_MODE", "auto")
    monkeypatch.setattr("imghost.redis_support.redis_async", SimpleNamespace(from_url=lambda *args, **kwargs: fake))

    with TestClient(app, base_url="https://testserver") as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "cookiefallback",
                "email": "cookiefallback@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200

        fake.fail = True
        me = client.get("/api/v1/user/me")
        assert me.status_code == 200
        assert me.json()["username"] == "cookiefallback"


def test_app_registration_still_creates_a_working_session_when_redis_is_down_from_start(tmp_path, monkeypatch) -> None:
    fake = FakeRedis()
    fake.fail = True
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setenv("REDIS_MODE", "auto")
    monkeypatch.setattr("imghost.redis_support.redis_async", SimpleNamespace(from_url=lambda *args, **kwargs: fake))

    with TestClient(app, base_url="https://testserver") as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "fallbackcreate",
                "email": "fallbackcreate@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200

        me = client.get("/api/v1/user/me")
        assert me.status_code == 200
        assert me.json()["username"] == "fallbackcreate"


def test_app_login_without_redis_configured_uses_cookie_session_token(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-secret")

    user_id, _ = create_user_and_api_key(capsys, username="cookielogin", email="cookielogin@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, user_id, "secret-pass")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "cookielogin@example.com", "password": "secret-pass"},
        )
        assert login.status_code == 200
        cookie_token = client.cookies.get("imghost_session")
        assert cookie_token is not None
        payload = _decode_signed_token(client.app.state.imghost.settings, cookie_token)
        assert payload is not None
        assert payload.store == "cookie"


def test_app_login_uses_redis_backed_session_token_when_redis_is_available(tmp_path, monkeypatch, capsys) -> None:
    fake = FakeRedis()
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setenv("REDIS_MODE", "auto")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setattr("imghost.redis_support.redis_async", SimpleNamespace(from_url=lambda *args, **kwargs: fake))

    user_id, _ = create_user_and_api_key(capsys, username="redislogin", email="redislogin@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, user_id, "secret-pass")
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "redislogin@example.com", "password": "secret-pass"},
        )
        assert login.status_code == 200
        cookie_token = client.cookies.get("imghost_session")
        assert cookie_token is not None
        payload = _decode_signed_token(client.app.state.imghost.settings, cookie_token)
        assert payload is not None
        assert payload.store == "redis"


def test_app_registration_uses_redis_backed_session_token_when_redis_is_available(tmp_path, monkeypatch) -> None:
    fake = FakeRedis()
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setenv("REDIS_MODE", "auto")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setattr("imghost.redis_support.redis_async", SimpleNamespace(from_url=lambda *args, **kwargs: fake))

    with TestClient(app, base_url="https://testserver") as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "redisregister",
                "email": "redisregister@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200
        cookie_token = client.cookies.get("imghost_session")
        assert cookie_token is not None
        payload = _decode_signed_token(client.app.state.imghost.settings, cookie_token)
        assert payload is not None
        assert payload.store == "redis"


def test_app_login_returns_503_when_redis_sessions_fail_closed_and_redis_is_down(tmp_path, monkeypatch, capsys) -> None:
    fake = FakeRedis()
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setenv("REDIS_MODE", "auto")
    monkeypatch.setenv("SESSION_REDIS_FAIL_CLOSED", "true")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setattr("imghost.redis_support.redis_async", SimpleNamespace(from_url=lambda *args, **kwargs: fake))

    user_id, _ = create_user_and_api_key(capsys, username="strictloginseed", email="strictloginseed@example.com")

    with TestClient(app, base_url="https://testserver") as client:
        set_user_password(client, user_id, "secret-pass")
        fake.fail = True
        login = client.post(
            "/api/v1/auth/login",
            json={"login": "strictloginseed@example.com", "password": "secret-pass"},
        )
        assert login.status_code == 503
        assert login.json()["detail"] == "Redis-backed sessions are currently unavailable."


def test_app_registration_returns_503_when_redis_sessions_fail_closed_and_redis_is_down(tmp_path, monkeypatch) -> None:
    fake = FakeRedis()
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setenv("REDIS_MODE", "auto")
    monkeypatch.setenv("SESSION_REDIS_FAIL_CLOSED", "true")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setattr("imghost.redis_support.redis_async", SimpleNamespace(from_url=lambda *args, **kwargs: fake))

    with TestClient(app, base_url="https://testserver") as client:
        fake.fail = True
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "strictregister",
                "email": "strictregister@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 503
        assert registered.json()["detail"] == "Redis-backed sessions are currently unavailable."


def test_app_existing_browser_session_fails_closed_when_redis_sessions_are_strict_and_redis_goes_down(tmp_path, monkeypatch) -> None:
    fake = FakeRedis()
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setenv("REDIS_MODE", "auto")
    monkeypatch.setenv("SESSION_REDIS_FAIL_CLOSED", "true")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setattr("imghost.redis_support.redis_async", SimpleNamespace(from_url=lambda *args, **kwargs: fake))

    with TestClient(app, base_url="https://testserver") as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "strictresolve",
                "email": "strictresolve@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200

        fake.fail = True
        me = client.get("/api/v1/user/me")
        assert me.status_code == 401


def test_app_existing_browser_session_still_works_when_redis_sessions_fail_open_and_redis_goes_down(tmp_path, monkeypatch) -> None:
    fake = FakeRedis()
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setenv("REDIS_MODE", "auto")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setattr("imghost.redis_support.redis_async", SimpleNamespace(from_url=lambda *args, **kwargs: fake))

    with TestClient(app, base_url="https://testserver") as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "failopenresolve",
                "email": "failopenresolve@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200

        fake.fail = True
        me = client.get("/api/v1/user/me")
        assert me.status_code == 200
        assert me.json()["username"] == "failopenresolve"


def test_dashboard_page_still_works_when_redis_sessions_fail_open_and_redis_goes_down(tmp_path, monkeypatch) -> None:
    fake = FakeRedis()
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setenv("REDIS_MODE", "auto")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setattr("imghost.redis_support.redis_async", SimpleNamespace(from_url=lambda *args, **kwargs: fake))

    with TestClient(app, base_url="https://testserver") as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "dashboardfallback",
                "email": "dashboardfallback@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200

        fake.fail = True
        dashboard = client.get("/dashboard")
        assert dashboard.status_code == 200
        assert "Dashboard" in dashboard.text


def test_dashboard_page_redirects_to_login_when_redis_sessions_fail_closed_and_redis_goes_down(tmp_path, monkeypatch) -> None:
    fake = FakeRedis()
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setenv("REDIS_MODE", "auto")
    monkeypatch.setenv("SESSION_REDIS_FAIL_CLOSED", "true")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setattr("imghost.redis_support.redis_async", SimpleNamespace(from_url=lambda *args, **kwargs: fake))

    with TestClient(app, base_url="https://testserver") as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "dashboardstrict",
                "email": "dashboardstrict@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200

        fake.fail = True
        dashboard = client.get("/dashboard", follow_redirects=False)
        assert dashboard.status_code == 303
        assert dashboard.headers["location"] == "/login?next=%2Fdashboard"


def test_admin_page_redirects_to_login_when_redis_sessions_fail_closed_and_redis_goes_down(tmp_path, monkeypatch, capsys) -> None:
    fake = FakeRedis()
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setenv("REDIS_MODE", "auto")
    monkeypatch.setenv("SESSION_REDIS_FAIL_CLOSED", "true")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setattr("imghost.redis_support.redis_async", SimpleNamespace(from_url=lambda *args, **kwargs: fake))

    with TestClient(app, base_url="https://testserver") as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "pageadminredis",
                "email": "pageadminredis@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200
        state = client.app.state.imghost
        user = client.portal.call(state.repository.get_user_by_email, "pageadminredis@example.com")
        assert user is not None
        user.is_admin = True
        user.updated_at = utcnow()
        client.portal.call(state.repository.update_user, user)

        fake.fail = True
        admin_page = client.get("/admin", follow_redirects=False)
        assert admin_page.status_code == 303
        assert admin_page.headers["location"] == "/login?next=%2Fadmin"


def test_app_logout_still_clears_cookie_when_redis_sessions_fail_closed_and_redis_is_down(tmp_path, monkeypatch) -> None:
    fake = FakeRedis()
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setenv("REDIS_MODE", "auto")
    monkeypatch.setenv("SESSION_REDIS_FAIL_CLOSED", "true")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setattr("imghost.redis_support.redis_async", SimpleNamespace(from_url=lambda *args, **kwargs: fake))

    with TestClient(app, base_url="https://testserver") as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "strictlogout",
                "email": "strictlogout@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200

        fake.fail = True
        logout = client.post("/api/v1/auth/logout", headers=browser_session_headers("https://testserver", "/"))
        assert logout.status_code == 200
        assert "imghost_session=" in logout.headers["set-cookie"]

        after_logout = client.get("/api/v1/user/me")
        assert after_logout.status_code == 401


def test_app_logout_still_clears_cookie_when_redis_delete_fails(tmp_path, monkeypatch) -> None:
    fake = FakeRedis()
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setenv("REDIS_MODE", "auto")
    monkeypatch.setattr("imghost.redis_support.redis_async", SimpleNamespace(from_url=lambda *args, **kwargs: fake))

    with TestClient(app, base_url="https://testserver") as client:
        registered = client.post(
            "/api/v1/auth/register",
            json={
                "username": "logoutfallback",
                "email": "logoutfallback@example.com",
                "password": "secret-pass",
            },
        )
        assert registered.status_code == 200

        fake.fail = True
        logout = client.post("/api/v1/auth/logout", headers=browser_session_headers("https://testserver", "/"))
        assert logout.status_code == 200
        assert "imghost_session=" in logout.headers["set-cookie"]

        after_logout = client.get("/api/v1/user/me")
        assert after_logout.status_code == 401


def test_health_ready_reports_detailed_status_when_redis_is_unavailable(tmp_path, monkeypatch) -> None:
    fake = FakeRedis()
    monkeypatch.setenv("IMGHOST_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BASE_URL", "https://testserver")
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setenv("REDIS_MODE", "auto")
    monkeypatch.setenv("TRUSTED_PUBLIC_ORIGINS", "https://testserver")
    monkeypatch.setattr("imghost.redis_support.redis_async", SimpleNamespace(from_url=lambda *args, **kwargs: fake))

    with TestClient(app, base_url="https://testserver") as client:
        fake.fail = True
        response = client.get("/health/ready")
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["redis"]["configured"] is True
        assert payload["redis"]["reachable"] is False
        assert payload["tasks"]["mode"] in {"async", "redis"}
