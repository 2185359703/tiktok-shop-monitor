import asyncio
from datetime import datetime, timezone

from redis.exceptions import ConnectionError

from app.services.ui_snapshot_cache import UISnapshotCache
from app.settings import Settings


class Redis:
    def __init__(self):
        self.values, self.writes = {}, []

    async def get(self, key):
        return self.values.get(key)

    async def eval(self, script, count, key, index, value, seconds):
        self.writes.append((key, value, seconds))
        self.values[key] = value

    async def aclose(self):
        pass


def payload():
    return 200, {"total": 4, "_meta": {"status": "fresh", "snapshot_at": datetime.now(timezone.utc).isoformat()}}


def test_cache_reuses_result_and_disconnected_waiter_does_not_cancel_build():
    async def scenario():
        redis, started, release, builds = Redis(), asyncio.Event(), asyncio.Event(), []
        cache = UISnapshotCache(Settings(), client=redis)
        async def build():
            builds.append(1)
            started.set()
            await release.wait()
            return payload()
        first = asyncio.create_task(cache.get_or_build("one", "catalog_stats", build))
        await started.wait()
        second = asyncio.create_task(cache.get_or_build("one", "catalog_stats", build))
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        release.set()
        value = await second
        cached = await cache.get_or_build("one", "catalog_stats", build)
        assert value[1] == cached[1] and builds == [1]
        assert len(redis.writes) == 1 and redis.writes[0][2] == 3
        await cache.close()
    asyncio.run(scenario())


def test_cache_failure_falls_back_and_pending_or_large_responses_are_not_cached():
    async def scenario():
        class Offline(Redis):
            async def get(self, key):
                raise ConnectionError("offline")
        cache = UISnapshotCache(Settings(), client=Offline())
        assert (await cache.get_or_build("one", "catalog_stats", lambda: asyncio.sleep(0, result=payload())))[0] == 200
        assert cache.unavailable_until > 0
        await cache.close()
        redis = Redis()
        cache = UISnapshotCache(Settings(), client=redis)
        for key, value in (("pending", (202, {"_meta": {"status": "pending"}})),
                           ("large", (200, {"body": "x" * 210000, "_meta": {"status": "stale"}}))):
            assert await cache.get_or_build(key, "catalog_stats", lambda: asyncio.sleep(0, result=value)) == value
        assert not redis.writes
        await cache.close()
    asyncio.run(scenario())
