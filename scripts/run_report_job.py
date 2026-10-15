"""Execute only one real report task through a privately scoped RabbitMQ queue."""

import argparse
import json
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values
from sqlalchemy import select

from app.models import CrawlTask, TaskStatus, TaskType
from app.observability.logging import configure_logging
from app.observability.metrics import Metrics
from app.queue.contracts import TaskMessage
from app.queue.rabbitmq import RabbitMQ
from app.queue.redis_gate import RedisGate
from app.services.report_service import ensure_report_task
from app.services.snapshot_service import utc_window
from app.services.task_runtime import database_now
from app.settings import Settings
from app.storage.database import make_engine, make_session_factory, session_scope
from app.workers.handlers import handler_registry
from app.workers.runtime import Worker


def main():
    parser = argparse.ArgumentParser(description="真实 RabbitMQ 报告任务实跑，不领取网站采集任务")
    parser.add_argument("--broker-config", type=Path, help="隔离联调的受保护 env 文件；缺省用项目 RABBITMQ_URL")
    args = parser.parse_args()
    configure_logging()
    config = Settings.load()
    broker_url = dotenv_values(args.broker_config).get("RABBITMQ_TEST_URL") if args.broker_config else config.rabbitmq_url
    identifier = uuid4().hex[:12]
    settings = replace(config, rabbitmq_url=broker_url, enabled_task_types=(TaskType.REPORT_GENERATION,),
                       periodic_snapshots_enabled=False, monitored_product_ids=(), queue_prefix=f"report_pilot_{identifier}",
                       exchange=f"report_pilot_{identifier}.tasks", dead_letter_exchange=f"report_pilot_{identifier}.dead",
                       dead_letter_queue=f"report_pilot_{identifier}.dead_letter")
    settings.require_infrastructure()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    broker, gate = RabbitMQ(settings), RedisGate(settings)
    try:
        with session_scope(factory) as db:
            task, created = ensure_report_task(db, settings, utc_window(database_now(db), 3600))
            task_id, completed = task.task_id, task.status == TaskStatus.SUCCEEDED
        if not completed:
            broker.connect()
            broker.publish(TaskMessage(task_id, TaskType.REPORT_GENERATION))
            deadline, delivery = time.monotonic() + 20, None
            while delivery is None and time.monotonic() < deadline:
                delivery = broker.get(TaskType.REPORT_GENERATION)
                if delivery is None: broker.poll(0.1)
            if delivery is None: raise RuntimeError("报告消息未在预期时间内送达。")
            tag, body = delivery
            message = TaskMessage.from_bytes(body)
            if message.task_id != task_id: raise RuntimeError("隔离队列收到非目标报告任务。")
            worker = Worker(settings, factory, handler_registry(), gate, Metrics(), f"report-pilot:{identifier}")
            action = worker.process(message, broker.poll)
            (broker.ack if action == "ack" else broker.reject)(tag)
            broker.poll(0.1)
        with factory() as db:
            task = db.scalar(select(CrawlTask).where(CrawlTask.task_id == task_id))
            if task.status != TaskStatus.SUCCEEDED: raise RuntimeError("报告业务处理未成功。")
            print(json.dumps({"task_id": task_id, "created": created, "reused_completed_task": completed,
                              "status": task.status, **(task.checkpoint or {})}, ensure_ascii=False, indent=2))
        return 0
    finally:
        try: broker.close()
        except Exception: pass
        gate.close(); engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "message": "真实报告队列执行未完成。"}, ensure_ascii=False))
        raise SystemExit(1)
