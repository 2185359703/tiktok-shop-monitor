"""Shared asynchronous request pacing, cooldown and dispatch deduplication."""

import asyncio
from uuid import uuid4

from redis.asyncio import Redis

from app.queue.redis_gate import PAUSE, RELEASE
from app.queue.retry_policy import Failure, TaskFailure


class AsyncRedisGate:
    def __init__(self, settings, client=None):
        self.settings = settings
        if client is None and not settings.redis_url:
            raise ValueError("缺少REDIS_URL。")
        self.client = client if client is not None else Redis.from_url(
            settings.redis_url, socket_connect_timeout=3, socket_timeout=3, decode_responses=True)
        self.prefix = f"{settings.queue_prefix}:{settings.platform}:{settings.region}"

    async def pause_remaining(self):
        return max(0, await self.client.pttl(f"{self.prefix}:pause") / 1000)

    async def pause(self):
        await self.client.eval(PAUSE, 1, f"{self.prefix}:pause", self.settings.access_pause_seconds * 1000)

    async def wait_turn(self):
        key = f"{self.prefix}:request_gate"
        while True:
            paused = await self.pause_remaining()
            if paused:
                raise TaskFailure(Failure("access_limited", "市场共享请求已暂停。", True, paused))
            if await self.client.set(key, "reserved", nx=True, px=round(self.settings.minimum_interval_seconds * 1000)):
                return
            await asyncio.sleep(min(max(await self.client.pttl(key) / 1000, .05), .25))

    async def reserve_publish(self, key):
        token = str(uuid4())
        return token if await self.client.set(f"{self.prefix}:publish:{key}", token, nx=True,
                                              ex=self.settings.publish_dedup_seconds) else None

    async def release_publish(self, key, token):
        await self.client.eval(RELEASE, 1, f"{self.prefix}:publish:{key}", token)

    async def close(self):
        await self.client.aclose()
