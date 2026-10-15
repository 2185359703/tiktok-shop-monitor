"""Review-only entrypoint for the shared native asyncio backend."""

import argparse
import asyncio
import json
from dataclasses import replace
from uuid import UUID

from app.models import TaskType
from app.observability.logging import configure_logging
from app.observability.metrics import Metrics
from app.queue.async_redis_gate import AsyncRedisGate
from app.queue.contracts import TaskMessage
from app.services.product_reviews import ensure_review_task, reviews_available
from app.settings import Settings
from app.storage.async_database import AsyncDatabase, make_async_engine
from app.workers.async_handlers import async_handler_registry
from app.workers.async_runtime import AsyncWorker
from scripts.async_runtime import worker_loop
from scripts.runtime_support import log_failure, stop_signal


async def run_task(settings, database, task_id):
    if str(UUID(task_id)) != task_id:
        raise ValueError("任务UUID格式不正确。")
    gate = AsyncRedisGate(settings)
    try:
        worker = AsyncWorker(settings, database, async_handler_registry(), gate, "review-once",
                             concurrency=settings.review_concurrency)
        action = await worker.process(TaskMessage(task_id, TaskType.PRODUCT_REVIEWS))
        print(json.dumps({"task_id": task_id, "action": action}))
        return 0 if action == "ack" else 1
    finally:
        await gate.close()


async def run(settings, args):
    database = AsyncDatabase(make_async_engine(settings.database_url))
    try:
        if not await database.read(reviews_available):
            raise ValueError("需要先执行评论扩展表迁移。")
        if args.enqueue_product_id:
            def enqueue(db):
                result = []
                for product_id in dict.fromkeys(args.enqueue_product_id):
                    task, created = ensure_review_task(db, settings, product_id, max_pages=args.max_pages)
                    result.append({"product_id": str(product_id), "task_id": task.task_id, "created": created})
                return result
            print(json.dumps({"tasks": await database.transaction(enqueue)}))
            return 0
        if args.task_id:
            return await run_task(settings, database, args.task_id)
        config = replace(settings, enabled_task_types=(TaskType.PRODUCT_REVIEWS,),
                         worker_concurrency=settings.review_concurrency)
        return await worker_loop(config, database, async_handler_registry(), Metrics(), stop_signal(), once=args.once)
    finally:
        await database.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--once", action="store_true")
    modes.add_argument("--task-id")
    modes.add_argument("--enqueue-product-id", type=int, action="append", default=[])
    parser.add_argument("--max-pages", type=int)
    args = parser.parse_args(argv)
    configure_logging()
    settings = Settings.load()
    if args.check:
        print(json.dumps({"task_type": "product_reviews", "async_database": True, "async_http": True, "async_mq": True,
                          "concurrency": settings.review_concurrency, "max_pages": settings.review_max_pages,
                          "enabled": TaskType.PRODUCT_REVIEWS in settings.enabled_task_types, "database_connections": 0}))
        return 0
    if TaskType.PRODUCT_REVIEWS not in settings.enabled_task_types:
        raise ValueError("需要启用product_reviews。")
    return asyncio.run(run(settings, args))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        log_failure("review_worker_failed", error)
        raise SystemExit(1)
