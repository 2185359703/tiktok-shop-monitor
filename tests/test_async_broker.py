import asyncio
from types import SimpleNamespace
from uuid import uuid4

from app.models import TaskType
from app.queue.async_broker import AsyncRabbitMQ
from app.queue.contracts import TaskMessage
from app.settings import Settings


def test_native_broker_confirms_all_routes_and_isolates_report_prefetch(monkeypatch):
    async def run():
        calls, sent = [], []
        class Exchange:
            def __init__(self, name): self.name = name
            async def publish(self, message, **kwargs): sent.append((message, kwargs))
        class Queue:
            declaration_result = SimpleNamespace(message_count=7)
            async def bind(self, exchange, **kwargs): calls.append(('bind', exchange.name, kwargs))
        class Channel:
            async def set_qos(self, **kwargs): calls.append(('qos', kwargs))
            async def declare_exchange(self, name, kind, **kwargs): return Exchange(name)
            async def declare_queue(self, name, **kwargs):
                calls.append(('queue', name, kwargs))
                return Queue()
        class Connection:
            is_closed = False
            async def channel(self, **kwargs):
                calls.append(('channel', kwargs))
                return Channel()
            async def close(self): self.is_closed = True
        connection = Connection()
        async def connect(*args, **kwargs): return connection
        monkeypatch.setattr('app.queue.async_broker.aio_pika.connect', connect)
        config = Settings(rabbitmq_url='amqp://localhost', broker_generation='async_test')
        broker = await AsyncRabbitMQ(config).connect()
        assert set(broker.queues) == set(TaskType)
        assert ('qos', {'prefetch_count': 8}) in calls
        assert ('qos', {'prefetch_count': 1}) in calls
        assert ('channel', {'publisher_confirms': True, 'on_return_raises': True}) in calls
        message = TaskMessage(str(uuid4()), TaskType.PRODUCT_DETAIL)
        await broker.publish(message)
        assert sent[0][0].message_id == message.task_id and sent[0][0].body == message.to_bytes()
        assert sent[0][1] == {'routing_key': 'product_detail', 'mandatory': True}
        assert await broker.ready_count(TaskType.PRODUCT_DETAIL) == 7
        assert ('queue', 'tiktok_shop.async_test.report_generation', {'passive': True}) in calls
        await broker.close()
        assert not broker.connected
    asyncio.run(run())
