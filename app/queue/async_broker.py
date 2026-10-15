"""Native AMQP for all task types, with confirms and generation-safe deliveries."""

import aio_pika

from app.models import TaskType


class AsyncRabbitMQ:
    def __init__(self, settings):
        self.settings = settings
        self.connection = None
        self.queues = {}

    def generated_name(self, value):
        return f"{value}.{self.settings.broker_generation}" if self.settings.broker_generation else value

    def queue_name(self, kind):
        return f"{self.generated_name(self.settings.queue_prefix)}.{kind.value}"

    @property
    def connected(self):
        return self.connection is not None and not self.connection.is_closed

    async def connect(self):
        if not self.settings.rabbitmq_url:
            raise ValueError("缺少RABBITMQ_URL。")
        self.connection = await aio_pika.connect(self.settings.rabbitmq_url, timeout=10, heartbeat=60)
        try:
            self.channel = await self.connection.channel(publisher_confirms=True, on_return_raises=True)
            # Per-consumer QoS works on RabbitMQ 4.x without deprecated global
            # QoS. AsyncWorker's semaphore bounds total executing tasks across
            # queues; deliveries waiting for a slot do not acquire SQL leases.
            await self.channel.set_qos(prefetch_count=self.settings.worker_concurrency)
            self.exchange = await self.channel.declare_exchange(self.generated_name(self.settings.exchange), "direct", durable=True)
            dead = await self.channel.declare_exchange(self.generated_name(self.settings.dead_letter_exchange), "direct", durable=True)
            dlq = await self.channel.declare_queue(self.generated_name(self.settings.dead_letter_queue), durable=True)
            for kind in TaskType:
                queue = await self.channel.declare_queue(self.queue_name(kind), durable=True,
                    arguments={"x-dead-letter-exchange": dead.name})
                await queue.bind(self.exchange, routing_key=kind.value)
                await dlq.bind(dead, routing_key=kind.value)
                self.queues[kind] = queue
            # A backlog of reports must not occupy every public-task delivery
            # slot while a single large report is being materialized.
            self.report_channel = await self.connection.channel()
            await self.report_channel.set_qos(prefetch_count=1)
            self.queues[TaskType.REPORT_GENERATION] = await self.report_channel.declare_queue(
                self.queue_name(TaskType.REPORT_GENERATION), passive=True)
            return self
        except BaseException:
            await self.close()
            raise

    async def publish(self, message):
        await self.exchange.publish(aio_pika.Message(body=message.to_bytes(), content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT, message_id=message.task_id),
            routing_key=message.task_type.value, mandatory=True)

    async def ready_count(self, kind):
        queue = await self.channel.declare_queue(self.queue_name(kind), passive=True)
        return queue.declaration_result.message_count

    async def get(self, kind):
        return await self.queues[kind].get(fail=False, timeout=5)

    async def close(self):
        if self.connected:
            await self.connection.close()
