import argparse
import asyncio
import json
import logging

from app.observability.logging import configure_logging
from app.observability.metrics import Metrics
from app.queue.async_broker import AsyncRabbitMQ
from app.queue.async_redis_gate import AsyncRedisGate
from app.services.async_scheduler import AsyncScheduler
from app.services.scheduler import task_counts
from app.settings import Settings
from app.storage.async_database import AsyncDatabase, make_async_engine
from app.workers.async_handlers import async_handler_registry
from app.workers.handlers import validate_enabled
from scripts.async_runtime import pause_until_stop
from scripts.runtime_support import log_failure, start_metrics, stop_signal


async def run(settings, args):
    database = AsyncDatabase(make_async_engine(settings.database_url))
    broker = gate = server = None
    metrics = Metrics()
    try:
        if args.dry_run:
            counts = await database.read(task_counts, settings)
            print(json.dumps({"enabled_task_types": list(settings.enabled_task_types),
                "counts": [{"task_type": k, "status": s, "count": n} for k, s, n in counts]}))
            return 0
        settings.require_infrastructure()
        broker, gate = AsyncRabbitMQ(settings), AsyncRedisGate(settings)
        scheduler = AsyncScheduler(settings, database, broker, gate, metrics)
        stop = stop_signal()
        if not args.once:
            server, _ = start_metrics(metrics, settings.scheduler_metrics_port, settings.host)
        while not stop.is_set():
            try:
                if not broker.connected:
                    await broker.connect()
                result = await scheduler.dispatch_once()
                logging.getLogger(__name__).info("scheduler_tick", extra={"count": result["published"]})
                if args.once:
                    print(json.dumps(result))
                    return 0
                await pause_until_stop(stop, settings.poll_seconds)
            except Exception as error:
                log_failure("scheduler_retry_connection", error)
                await broker.close()
                if args.once:
                    return 1
                await pause_until_stop(stop, settings.poll_seconds)
        return 0
    finally:
        if server:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
        if broker:
            await broker.close()
        if gate:
            await gate.close()
        await database.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="异步任务调度、通知补发与租约恢复")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="只读数据库，不连接MQ/Redis或发布消息")
    args = parser.parse_args(argv)
    configure_logging()
    settings = Settings.load()
    validate_enabled(settings, async_handler_registry())
    return asyncio.run(run(settings, args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        log_failure("invalid_runtime_configuration", error)
        raise SystemExit(1)
