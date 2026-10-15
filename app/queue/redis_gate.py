"""A shared request gate and pause switch; Redis failures are fail-closed."""

import time
from threading import Event
from uuid import uuid4

from redis import Redis

from app.queue.retry_policy import Failure, TaskFailure
from app.services.task_runtime import LeaseLost
from app.settings import Settings


RELEASE = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end"
PAUSE = "if redis.call('PTTL', KEYS[1]) < tonumber(ARGV[1]) then redis.call('SET', KEYS[1], 'access_limited', 'PX', ARGV[1]) end return 1"


class RedisGate:
    def __init__(self, settings: Settings, client=None):
        if client is None and not settings.redis_url:
            raise ValueError("缺少 REDIS_URL。")
        self.settings = settings
        self.client = client if client is not None else Redis.from_url(
            settings.redis_url, socket_connect_timeout=3, socket_timeout=3, decode_responses=True)
        self.prefix = f"{settings.queue_prefix}:{settings.platform}:{settings.region}"

    def pause_remaining(self) -> float:
        return max(0, self.client.pttl(f"{self.prefix}:pause") / 1000)

    def pause(self) -> None:
        self.client.eval(PAUSE, 1, f"{self.prefix}:pause", self.settings.access_pause_seconds * 1000)

    def wait_turn(self, stop: Event) -> None:
        key = f"{self.prefix}:request_gate"
        while not stop.is_set():
            paused = self.pause_remaining()
            if paused:
                raise TaskFailure(Failure("access_limited", "市场共享闸门已暂停，等待人工检查或冷却。", True, paused))
            if self.client.set(key, "reserved", nx=True, px=round(self.settings.minimum_interval_seconds * 1000)):
                return
            stop.wait(min(max(self.client.pttl(key) / 1000, 0.05), 0.25))
        raise LeaseLost("执行已停止，禁止继续请求。")

    def reserve_publish(self, key: str) -> str | None:
        token = str(uuid4())
        return token if self.client.set(f"{self.prefix}:publish:{key}", token, nx=True,
                                        ex=self.settings.publish_dedup_seconds) else None

    def release_publish(self, key: str, token: str) -> None:
        self.client.eval(RELEASE, 1, f"{self.prefix}:publish:{key}", token)

    def close(self):
        self.client.close()
