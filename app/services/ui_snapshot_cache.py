"""Small, optional Redis read-through cache; MySQL remains authoritative.

The namespace has at most 32 entries of at most 200 KiB for at most 3 seconds.
No source observations, control-plane keys or Redis server settings are changed.
"""

import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.services.analytics_snapshot import ttl


PUT = """
local clock = redis.call('TIME')
local now = tonumber(clock[1])
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
if not redis.call('ZSCORE', KEYS[2], KEYS[1]) and redis.call('ZCARD', KEYS[2]) >= 32 then
    return 0
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
redis.call('ZADD', KEYS[2], now + tonumber(ARGV[2]), KEYS[1])
redis.call('EXPIRE', KEYS[2], 10)
return 1
"""


class UISnapshotCache:
    def __init__(self, config, *, client=None):
        namespace = hashlib.sha256(str(config.database_url or "injected").encode()).hexdigest()[:20]
        self.prefix = f"shopscope:ui:snapshots:v1:{namespace}:"
        self.client = client if client is not None else (Redis.from_url(config.redis_url, socket_connect_timeout=.08,
            socket_timeout=.08, retry_on_timeout=False, max_connections=4) if config.redis_url else None)
        self.pending, self.unavailable_until = {}, 0

    async def redis_call(self, method, *args):
        if self.client is None or time.monotonic() < self.unavailable_until:
            return None
        try:
            async with asyncio.timeout(.1):
                return await getattr(self.client, method)(*args)
        except (RedisError, OSError, TimeoutError):
            self.unavailable_until = time.monotonic() + 10
            return None

    async def get_or_build(self, key, kind, builder):
        redis_key = self.prefix + key
        raw = await self.redis_call("get", redis_key)
        if raw:
            try:
                value = json.loads(raw)
                if value[0] == 200 and isinstance(value[1], dict) and "_meta" in value[1]:
                    return value
            except (ValueError, KeyError, TypeError, IndexError):
                pass
        if key not in self.pending:
            if len(self.pending) >= 32:
                return await builder()
            async def build():
                value = await builder()
                if value[0] == 200:
                    meta = value[1].get("_meta", {})
                    seconds = 3
                    if meta.get("status") == "fresh":
                        stamp = datetime.fromisoformat(meta["snapshot_at"])
                        seconds = min(seconds, int(ttl(kind) - (datetime.now(timezone.utc) - stamp).total_seconds()))
                    if kind not in {"overview", "workbench"} and meta.get("generated_at"):
                        generated = datetime.fromisoformat(meta["generated_at"])
                        retention = 60 if kind == "task_counts" else 900
                        seconds = min(seconds, int(retention - (datetime.now(timezone.utc) - generated).total_seconds()))
                    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
                    if seconds > 0 and len(encoded) <= 200 * 1024:
                        await self.redis_call("eval", PUT, 2, redis_key, self.prefix + "keys", encoded, seconds)
                return value
            task = asyncio.create_task(build())
            self.pending[key] = task
            def finished(done):
                self.pending.pop(key, None)
                if not done.cancelled():
                    done.exception()  # consume failure even if every HTTP waiter disconnected
            task.add_done_callback(finished)
        return await asyncio.shield(self.pending[key])

    async def close(self):
        tasks = list(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.client is not None:
            await self.client.aclose()
