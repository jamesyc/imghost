import asyncio
import os
from uuid import uuid4

import pytest

from imghost.config import load_settings
from imghost.redis_support import RedisHandle


@pytest.mark.skipif(not os.getenv("TEST_REDIS_URL"), reason="Set TEST_REDIS_URL for real Redis integration coverage")
def test_redis_resp3_preserves_sessions_queues_and_lease_operations(monkeypatch) -> None:
    monkeypatch.setenv("REDIS_URL", os.environ["TEST_REDIS_URL"])
    monkeypatch.setenv("REDIS_MODE", "required")
    monkeypatch.setenv("REDIS_PREFIX", f"upgrade-test-{uuid4().hex}")

    async def exercise():
        handle = RedisHandle(load_settings())
        keys = [handle.prefixed(name) for name in ["session", "queue", "lease"]]
        try:
            client = await handle.get_client()
            assert client.connection_pool.connection_kwargs["retry"].get_retries() == 0
            assert await handle.ping()
            assert await client.set(keys[0], "session-data", ex=60)
            assert await client.get(keys[0]) == "session-data"
            assert 0 < await client.ttl(keys[0]) <= 60
            await client.rpush(keys[1], "first", "second")
            assert list(await client.blpop(keys[1], timeout=1)) == [keys[1], "first"]
            assert await client.lpop(keys[1]) == "second"
            assert await client.set(keys[2], "owner", nx=True, ex=60)
            assert not await client.set(keys[2], "other", nx=True, ex=60)
            assert await client.eval(
                'if redis.call("GET", KEYS[1]) == ARGV[1] then return redis.call("DEL", KEYS[1]) else return 0 end',
                1, keys[2], "owner",
            ) == 1
        finally:
            if handle._client is not None:
                await handle._client.delete(*keys)
            await handle.close()

    asyncio.run(exercise())
