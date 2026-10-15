from dataclasses import replace

import pytest

from app.models import TaskType
from app.queue.rabbitmq import RabbitMQ
from app.queue.redis_gate import RedisGate
from app.settings import Settings


def broker_config():
    return Settings(rabbitmq_url="amqp://fixture:fixture@127.0.0.1:5672/%2F")


def test_default_broker_names_remain_compatible():
    config = broker_config()
    broker = RabbitMQ(config)
    assert broker.queue_name(TaskType.PRODUCT_DETAIL) == "tiktok_shop.product_detail"
    assert broker.exchange == config.exchange and broker.dead_letter_queue == config.dead_letter_queue


def test_generation_changes_all_broker_names_but_preserves_redis_request_pause_and_publish_scope():
    original = broker_config()
    generated = replace(original, broker_generation="g20261015")
    broker = RabbitMQ(generated)
    assert broker.queue_name(TaskType.PRODUCT_DETAIL) == "tiktok_shop.g20261015.product_detail"
    assert broker.exchange == "tiktok_shop.tasks.g20261015"
    assert broker.dead_letter_exchange == "tiktok_shop.dead.g20261015"
    assert broker.dead_letter_queue == "tiktok_shop.dead_letter.g20261015"
    assert RedisGate(original, client=object()).prefix == RedisGate(generated, client=object()).prefix
    assert original.queue_prefix == generated.queue_prefix


@pytest.mark.parametrize("generation", ["bad/name", "bad.name", "x" * 33, None])
def test_unsafe_generation_names_are_rejected(generation):
    with pytest.raises(ValueError):
        replace(broker_config(), broker_generation=generation)
