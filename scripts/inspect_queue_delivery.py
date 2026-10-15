"""Read current and retained RabbitMQ counts without consuming notifications."""

import json
from datetime import datetime, timezone

from app.models import TaskType
from app.queue.rabbitmq import RabbitMQ
from app.queue.redis_gate import RedisGate
from app.settings import Settings


def main():
    config = Settings.load()
    broker, gate = RabbitMQ(config), RedisGate(config)
    try:
        broker.connect()
        active = {kind.value: broker.ready_count(kind) for kind in TaskType}
        legacy = None
        if config.broker_generation:
            name = config.queue_prefix + "." + TaskType.PRODUCT_DETAIL.value
            legacy = broker.channel.queue_declare(queue=name, passive=True).method.message_count
        print(json.dumps({"checked_at": datetime.now(timezone.utc).isoformat(), "read_only": True,
                          "broker_generation": config.broker_generation, "active_ready": active,
                          "retained_legacy_detail_ready": legacy,
                          "redis_coordination_prefix": gate.prefix,
                          "market_pause_remaining_seconds": round(gate.pause_remaining(), 1),
                          "messages_consumed": 0, "queues_purged": 0}, ensure_ascii=False, indent=2))
    finally:
        broker.close()
        gate.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "message": "只读队列核验未完成，未输出连接信息。"}, ensure_ascii=False))
        raise SystemExit(1)
