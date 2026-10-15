"""Async AMQP consumer. Queue topology matches the existing scheduler exactly."""

import aio_pika

from app.models import TaskType


class AsyncReviewQueue:
    def __init__(self, settings):
        self.settings = settings
        self.connection = None

    def generated_name(self, name):
        return f"{name}.{self.settings.broker_generation}" if self.settings.broker_generation else name

    async def connect(self):
        settings = self.settings
        # A transport failure ends this connection generation. The caller drains
        # its in-flight work before reconnecting; deliveries are never ACKed on
        # a replacement channel with reused delivery tags.
        self.connection = await aio_pika.connect(settings.rabbitmq_url, timeout=10, heartbeat=60)
        try:
            self.channel = await self.connection.channel()
            await self.channel.set_qos(prefetch_count=settings.review_concurrency)
            exchange = await self.channel.declare_exchange(self.generated_name(settings.exchange), "direct", durable=True)
            dead = await self.channel.declare_exchange(self.generated_name(settings.dead_letter_exchange), "direct", durable=True)
            dlq = await self.channel.declare_queue(self.generated_name(settings.dead_letter_queue), durable=True)
            kind = TaskType.PRODUCT_REVIEWS.value
            self.queue = await self.channel.declare_queue(f"{self.generated_name(settings.queue_prefix)}.{kind}",
                durable=True, arguments={"x-dead-letter-exchange": dead.name})
            await self.queue.bind(exchange, routing_key=kind)
            await dlq.bind(dead, routing_key=kind)
            return self
        except BaseException:
            await self.close()
            raise

    async def close(self):
        if self.connection is not None and not self.connection.is_closed:
            await self.connection.close()
