import json
import logging
from dataclasses import replace
from threading import Event
from uuid import uuid4

import httpx
import pytest

from app.crawlers.public_shop import PublicShopClient
from app.models import TaskType
from app.observability.logging import JsonFormatter
from app.queue.contracts import TaskMessage
from app.queue.rabbitmq import RabbitMQ
from app.queue.redis_gate import RedisGate
from app.queue.retry_policy import retry_delay
from app.settings import Settings
from app.workers.handlers import handler_registry, validate_enabled


def test_settings_defaults_disable_crawling_and_hide_secrets():
    config = Settings(rabbitmq_url="amqp://name:do-not-print@localhost/%2F",
                      redis_url="redis://:another-secret@localhost/0", api_token="not-a-real-token-1234567890")
    assert not config.enabled_task_types
    assert "do-not-print" not in repr(config) and "another-secret" not in repr(config)
    assert "not-a-real-token" not in repr(config)
    with pytest.raises(ValueError):
        replace(config, minimum_interval_seconds=0.1)
    with pytest.raises(ValueError):
        replace(config, lease_seconds=30, heartbeat_seconds=15)
    with pytest.raises(ValueError):
        replace(config, api_token="short")


def test_config_load_env_and_capabilities(monkeypatch, tmp_path):
    config = tmp_path / "settings.toml"
    config.write_text('[runtime]\nenabled_task_types = ["shop_discovery"]\n', encoding="utf-8")
    monkeypatch.setenv("ENABLED_TASK_TYPES", "")
    assert Settings.load(config).enabled_task_types == ()
    monkeypatch.setenv("ENABLED_TASK_TYPES", "shop_discovery")
    assert Settings.load(config).enabled_task_types == (TaskType.SHOP_DISCOVERY,)
    with pytest.raises(ValueError):
        validate_enabled(Settings(enabled_task_types=(TaskType.REPORT_GENERATION,)), {TaskType.SHOP_DISCOVERY: handler_registry()[TaskType.SHOP_DISCOVERY]})


@pytest.mark.parametrize("body", [b"[]", b"{}", b"x" * 1025, b'{"version":true}',
                                  json.dumps({"version": 1, "task_id": str(uuid4()), "task_type": "product_detail", "cookie": "secret"}).encode()])
def test_message_contract_rejects_payloads_and_invalid_versions(body):
    with pytest.raises(ValueError):
        TaskMessage.from_bytes(body)


def test_message_and_retry_policy():
    message = TaskMessage(str(uuid4()), TaskType.SHOP_DISCOVERY)
    assert TaskMessage.from_bytes(message.to_bytes()) == message
    assert 10 <= retry_delay(message.task_id, 1, 10, 300) <= 12
    assert retry_delay(message.task_id, 10, 10, 300) == 300
    assert retry_delay(message.task_id, 2, 10, 300) == retry_delay(message.task_id, 2, 10, 300)


def test_logs_do_not_serialize_exception_or_extra_secret_fields():
    record = logging.LogRecord("test", logging.ERROR, __file__, 1, "task_failed", (),
                               (ValueError, ValueError("password=private"), None))
    record.task_id, record.password, record.payload = "task", "secret", {"token": "secret"}
    output = JsonFormatter().format(record)
    assert "private" not in output and "secret" not in output
    assert json.loads(output)["exception_type"] == "ValueError"


def test_shared_gate_applies_to_redirect_requests():
    calls = []
    def response(request):
        return httpx.Response(302, headers={"Location": "/us/k/clothing"}) if request.url.path == "/us/k/women" else httpx.Response(403)
    with PublicShopClient(interval=0, max_attempts=1, before_request=lambda: calls.append(1),
                          transport=httpx.MockTransport(response)) as client:
        with pytest.raises(Exception):
            client.fetch("https://shop.tiktok.com/us/k/women")
    assert len(calls) == 2


def test_rabbitmq_notifications_use_confirm_and_manual_ack(monkeypatch):
    calls = []
    class Channel:
        def __getattr__(self, name):
            def operation(**kwargs):
                calls.append((name, kwargs))
            return operation
    class Connection:
        is_open = True
        def channel(self):
            return Channel()
        def close(self):
            self.is_open = False
    monkeypatch.setattr("app.queue.rabbitmq.pika.BlockingConnection", lambda parameters: Connection())
    config = Settings(rabbitmq_url="amqp://localhost/%2F")
    broker = RabbitMQ(config).connect()
    broker.publish(TaskMessage(str(uuid4()), TaskType.SHOP_DISCOVERY))
    broker.ack(12)
    broker.reject(13)
    assert any(name == "confirm_delivery" for name, args in calls)
    args = next(args for name, args in calls if name == "basic_publish")
    assert args["mandatory"] is True and args["properties"].delivery_mode == 2
    assert any(name == "basic_nack" and args["requeue"] is False for name, args in calls)
    broker.close()
