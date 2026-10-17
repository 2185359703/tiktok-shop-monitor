"""All collection and local task types run in the bounded asyncio worker."""

import argparse
import asyncio
import json

from app.observability.logging import configure_logging
from app.observability.metrics import Metrics
from app.services.collection_heartbeat import RuntimeHeartbeat, instance_id
from app.settings import Settings
from app.storage.async_database import AsyncDatabase, make_async_engine
from app.workers.async_handlers import async_handler_registry
from app.workers.handlers import validate_enabled
from scripts.async_runtime import worker_loop
from scripts.runtime_support import log_failure, start_metrics, stop_signal


async def run(config, registry, *, once=False):
    database = AsyncDatabase(make_async_engine(config.database_url))
    metrics, server = Metrics(), None
    identifier = instance_id()
    runtime_status = {"state": "starting", "concurrency": config.worker_concurrency}
    def telemetry():
        return {**runtime_status, "active_tasks": int(metrics.active.collect()[0].samples[0].value)}
    heartbeat = RuntimeHeartbeat(config, "worker", identifier, telemetry)
    try:
        if not once:
            server, _ = start_metrics(metrics, config.worker_metrics_port, config.host)
            heartbeat.start()
        return await worker_loop(config, database, registry, metrics, stop_signal(), once=once,
                                 worker_id=identifier, runtime_status=runtime_status)
    finally:
        await heartbeat.close()
        if server:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
        await database.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只显示异步处理器和配置，不连接基础设施")
    parser.add_argument("--once", action="store_true", help="最多处理一条通知")
    args = parser.parse_args(argv)
    configure_logging()
    config, registry = Settings.load(), async_handler_registry()
    validate_enabled(config, registry)
    if args.check:
        print(json.dumps({"implemented_task_types": sorted(registry), "enabled_task_types": list(config.enabled_task_types),
                          "async_database": True, "async_http": True, "async_mq": True,
                          "concurrency": config.worker_concurrency}, ensure_ascii=False))
        return 0
    return asyncio.run(run(config, registry, once=args.once))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        log_failure("invalid_runtime_configuration", error)
        raise SystemExit(1)
