import asyncio
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

from app.models import TaskType
from app.queue.contracts import TaskMessage
from app.settings import Settings
from scripts.async_runtime import consume


def test_shutdown_finishes_current_delivery_and_stops_intake_without_cancelling_it():
    async def run():
        stop, events = Event(), []
        config = Settings(enabled_task_types=(TaskType.SHOP_DISCOVERY,))
        class Delivery:
            body = TaskMessage(str(uuid4()), TaskType.SHOP_DISCOVERY).to_bytes()
            async def ack(self):
                events.append("ack")
            async def reject(self, **kwargs):
                raise AssertionError("Successful work must ACK")
        class Queue:
            async def consume(self, callback):
                await callback(Delivery())
                events.append("consume")
                return "consumer"
            async def cancel(self, tag):
                events.append("intake_stopped")
        class Worker:
            async def process(self, message):
                stop.set()
                await asyncio.sleep(.15)
                events.append("committed")
                return "ack"
        broker = SimpleNamespace(queues={TaskType.SHOP_DISCOVERY: Queue()}, connected=True)
        assert await consume(config, Worker(), broker, stop)
        assert events.index("intake_stopped") < events.index("committed") < events.index("ack")
    asyncio.run(run())


def test_infrastructure_failure_is_not_acknowledged():
    async def run():
        config = Settings(enabled_task_types=(TaskType.SHOP_DISCOVERY,))
        class Delivery:
            body = TaskMessage(str(uuid4()), TaskType.SHOP_DISCOVERY).to_bytes()
            async def ack(self):
                raise AssertionError("Uncommitted work cannot ACK")
            async def reject(self, **kwargs):
                raise AssertionError("Infrastructure failure must requeue on close")
        class Broker:
            async def get(self, kind):
                return Delivery()
        class Worker:
            async def process(self, message):
                raise OSError("fixture infrastructure failure")
        assert not await consume(config, Worker(), Broker(), Event(), once=True)
    asyncio.run(run())
