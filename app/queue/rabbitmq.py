"""Persistent notifications, publisher confirms, manual ACK, dead letters."""

import pika

from app.models import TaskType
from app.queue.contracts import TaskMessage
from app.settings import Settings


class RabbitMQ:
    def __init__(self, settings: Settings):
        if not settings.rabbitmq_url:
            raise ValueError("缺少 RABBITMQ_URL。")
        self.settings = settings
        self.connection = None
        self.channel = None
        self.exchange = self.generated_name(settings.exchange)
        self.dead_letter_exchange = self.generated_name(settings.dead_letter_exchange)
        self.dead_letter_queue = self.generated_name(settings.dead_letter_queue)

    def generated_name(self, name):
        generation = self.settings.broker_generation
        return f"{name}.{generation}" if generation else name

    def queue_name(self, task_type: TaskType) -> str:
        return f"{self.generated_name(self.settings.queue_prefix)}.{task_type.value}"

    def connect(self):
        parameters = pika.URLParameters(self.settings.rabbitmq_url)
        parameters.heartbeat = 60
        parameters.blocked_connection_timeout = 30
        parameters.socket_timeout = 5
        parameters.stack_timeout = 10
        parameters.connection_attempts = 1
        self.connection = pika.BlockingConnection(parameters)
        self.channel = self.connection.channel()
        self.channel.exchange_declare(exchange=self.exchange, exchange_type="direct", durable=True)
        self.channel.exchange_declare(exchange=self.dead_letter_exchange, exchange_type="direct", durable=True)
        self.channel.queue_declare(queue=self.dead_letter_queue, durable=True)
        for kind in TaskType:
            queue = self.queue_name(kind)
            self.channel.queue_declare(queue=queue, durable=True, arguments={
                "x-dead-letter-exchange": self.dead_letter_exchange,
            })
            self.channel.queue_bind(queue=queue, exchange=self.exchange, routing_key=kind.value)
            self.channel.queue_bind(queue=self.dead_letter_queue,
                                    exchange=self.dead_letter_exchange, routing_key=kind.value)
        self.channel.basic_qos(prefetch_count=self.settings.prefetch)
        self.channel.confirm_delivery()
        return self

    def publish(self, message: TaskMessage):
        self.channel.basic_publish(exchange=self.exchange, routing_key=message.task_type.value,
                                   body=message.to_bytes(), mandatory=True,
                                   properties=pika.BasicProperties(content_type="application/json", delivery_mode=2,
                                                                   message_id=message.task_id))

    def get(self, task_type: TaskType):
        # One in-flight delivery per worker; no unbounded consumer buffering.
        method, properties, body = self.channel.basic_get(queue=self.queue_name(task_type), auto_ack=False)
        return (method.delivery_tag, body) if method else None

    def ready_count(self, task_type: TaskType) -> int:
        # Passive inspection never creates/replaces a queue or drops messages.
        return self.channel.queue_declare(queue=self.queue_name(task_type), passive=True).method.message_count

    def ack(self, tag: int):
        self.channel.basic_ack(delivery_tag=tag)

    def reject(self, tag: int):
        self.channel.basic_nack(delivery_tag=tag, requeue=False)

    def poll(self, seconds: float = 0.2):
        self.connection.process_data_events(time_limit=seconds)

    def close(self):
        if self.connection is not None and self.connection.is_open:
            self.connection.close()
