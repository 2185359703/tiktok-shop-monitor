import os
from dataclasses import replace
from threading import Event
from uuid import uuid4
from urllib.parse import urlsplit

import pytest
from redis import Redis

from app.queue.redis_gate import RedisGate
from app.queue.retry_policy import TaskFailure
from app.settings import Settings


@pytest.fixture
def redis_gate():
    url = os.environ.get("REDIS_TEST_URL")
    if not url:
        pytest.skip("Set REDIS_TEST_URL for a dedicated disposable local Redis.")
    parsed = urlsplit(url)
    if parsed.hostname != "127.0.0.1" or parsed.port != 36387:
        raise ValueError("Redis tests require the dedicated localhost:36387 instance.")
    config = Settings(redis_url=url, queue_prefix=f"test_{uuid4().hex}")
    gate = RedisGate(config)
    assert gate.client.ping()
    yield gate
    # Delete only this test's unique key prefix, never FLUSHDB/FLUSHALL.
    keys = list(gate.client.scan_iter(f"{gate.prefix}:*"))
    if keys:
        gate.client.delete(*keys)
    gate.close()


def test_real_redis_pause_publish_dedup_and_token_checked_release(redis_gate):
    gate = redis_gate
    token = gate.reserve_publish("task")
    assert token is not None and gate.reserve_publish("task") is None
    gate.release_publish("task", "wrong-owner")
    assert gate.reserve_publish("task") is None
    gate.release_publish("task", token)
    assert gate.reserve_publish("task") is not None
    gate.pause()
    assert 0 < gate.pause_remaining() <= gate.settings.access_pause_seconds
    with pytest.raises(TaskFailure) as error:
        gate.wait_turn(Event())
    assert error.value.failure.code == "access_limited"


def test_real_redis_request_spacing_is_shared_between_workers(redis_gate):
    from time import monotonic
    second = RedisGate(redis_gate.settings)
    try:
        redis_gate.wait_turn(Event())
        began = monotonic()
        second.wait_turn(Event())
        assert monotonic() - began >= redis_gate.settings.minimum_interval_seconds - 0.1
    finally:
        second.close()
