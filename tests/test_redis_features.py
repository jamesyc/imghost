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
from imghost.observability import ObservabilityState
from imghost.rate_limits import InMemoryRateLimiter, RedisRateLimiter
from imghost.redis_support import RedisHandle
from imghost.sessions import RedisBackedSessionBackend
from imghost.tasks import RedisTaskQueue, TaskContext


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
        "trusted_public_origins": ("https://testserver",),
        "database_url": "postgresql://test",
        "data_dir": Path("/tmp/imghost-test"),
        "redis_url": None,
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
        logout = client.post("/api/v1/auth/logout")
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
