import asyncio
from types import SimpleNamespace

from app.models import TaskType
from app.queue.async_rabbitmq import AsyncReviewQueue
from app.queue.rabbitmq import RabbitMQ
from app.settings import Settings


def test_async_queue_uses_existing_generation_dlx_and_bounded_prefetch(monkeypatch):
    async def run():
        events = []
        class Queue:
            async def bind(self, exchange, routing_key):
                events.append(("bind", exchange.name, routing_key))
        class Channel:
            async def set_qos(self, **kwargs):
                events.append(("qos", kwargs))
            async def declare_exchange(self, name, kind, **kwargs):
                events.append(("exchange", name, kind, kwargs))
                return SimpleNamespace(name=name)
            async def declare_queue(self, name, **kwargs):
                events.append(("queue", name, kwargs))
                return Queue()
        class Connection:
            is_closed = False
            async def channel(self):
                return Channel()
            async def close(self):
                self.is_closed = True
        connection = Connection()
        async def connect(*args, **kwargs):
            return connection
        monkeypatch.setattr("app.queue.async_rabbitmq.aio_pika.connect", connect)
        settings = Settings(rabbitmq_url="amqp://localhost", broker_generation="review_test", review_concurrency=3)
        broker = await AsyncReviewQueue(settings).connect()
        reference = RabbitMQ(settings)
        assert ("qos", {"prefetch_count": 3}) in events
        assert ("queue", reference.queue_name(TaskType.PRODUCT_REVIEWS), {
            "durable": True, "arguments": {"x-dead-letter-exchange": reference.dead_letter_exchange}}) in events
        await broker.close()
        assert connection.is_closed
    asyncio.run(run())
