"""Real local API for browser checks; synthetic data, no broker or source requests."""

import argparse
import os
import re
from datetime import datetime, timedelta, timezone

import uvicorn
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.main import create_app
from app.models import CrawlAttempt, CrawlTask, Product, ResourceType, Shop, TaskStatus, TaskType
from app.services.analytics_runtime import AnalyticsRunner, backfill_batch, maintain
from app.settings import Settings
from app.storage.database import make_engine
from app.storage.schema import mysql_ddl


def seed(factory):
    now = datetime.now(timezone.utc)
    with factory.begin() as db:
        shop = Shop(region="US", platform_shop_id="7490000000000000001",
                    canonical_name="TEST FIXTURE · 采集管理验证店铺",
                    canonical_url="https://shop.tiktok.com/us/store/fixture/7490000000000000001")
        db.add(shop)
        db.flush()
        product = Product(region="US", shop_id=shop.id, platform_product_id="7490000000000000002",
                          product_url="https://shop.tiktok.com/view/product/7490000000000000002",
                          title="TEST FIXTURE · Cotton shirt", last_success_at=now,
                          last_seen_at=now, price_min=19, price_max=24)
        db.add(product)
        db.flush()
        for index in range(36):
            failed = index % 6 == 0
            ended = now - timedelta(minutes=35-index)
            task = CrawlTask(region="US", task_type=TaskType.SHOP_PROFILE,
                             resource_type=ResourceType.SHOP, resource_id=str(shop.id),
                             payload={"shop_id": str(shop.id), "source_url": shop.canonical_url},
                             status=TaskStatus.DEAD if failed else TaskStatus.SUCCEEDED,
                             attempt_count=1, max_attempts=1, scheduled_for=ended,
                             available_at=ended, started_at=ended-timedelta(seconds=2),
                             finished_at=ended, worker_id="fixture-worker")
            db.add(task)
            db.flush()
            db.add(CrawlAttempt(task_id=task.id, attempt_no=1, worker_id="fixture-worker",
                                started_at=task.started_at, finished_at=ended,
                                succeeded=not failed, latency_ms=800+index*30,
                                response_status=503 if failed else 200,
                                error_type="source_unavailable" if failed else None))
        db.add(CrawlTask(region="US", task_type=TaskType.SHOP_PROFILE,
                         resource_type=ResourceType.SHOP, resource_id=str(shop.id),
                         payload={"shop_id": str(shop.id), "source_url": shop.canonical_url},
                         status=TaskStatus.PENDING, scheduled_for=now, available_at=now))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--port", type=int, default=18892)
    args = parser.parse_args()
    url = make_url(args.database_url)
    if url.host not in {"127.0.0.1", "localhost"} or not re.fullmatch(
        r"tiktok_shop_monitor_test_[a-zA-Z0-9_]+", url.database or ""
    ):
        raise ValueError("A new local tiktok_shop_monitor_test_* database is required.")
    admin = make_engine(url, with_database=False)
    created = False
    engine = None
    try:
        with admin.connect() as connection:
            if connection.scalar(text("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=:name"), {"name": url.database}):
                raise ValueError("Choose a new disposable test schema.")
            for statement in mysql_ddl(url.database).split(";"):
                statement = "\n".join(line for line in statement.splitlines() if not line.startswith("--")).strip()
                if statement:
                    connection.exec_driver_sql(statement)
                    if statement.startswith("CREATE DATABASE"):
                        created = True
        engine = make_engine(url)
        factory = sessionmaker(engine, expire_on_commit=False)
        os.environ["ANALYTICS_ENABLED"] = "1"
        config = Settings(database_url=args.database_url, api_token="local-fixture-collection-token",
                          enabled_task_types=(TaskType.SHOP_PROFILE, TaskType.PRODUCT_SNAPSHOT,
                                              TaskType.PRODUCT_DETAIL, TaskType.PRODUCT_DISCOVERY))
        seed(factory)
        runner = AnalyticsRunner(factory, config)
        for _ in range(32):
            with factory.begin() as db:
                complete = backfill_batch(db, config)
            if not runner.run_once() and complete:
                break
        with factory.begin() as db:
            maintain(db, config)
        for _ in range(32):
            if not runner.run_once():
                break
        application = create_app(config)

        @application.middleware("http")
        async def prefix(request, call_next):
            if request.scope["path"].startswith("/shopscope/"):
                request.scope["path"] = request.scope["path"][len("/shopscope"):]
                request.scope["raw_path"] = request.scope["path"].encode()
            return await call_next(request)

        print(f"Synthetic local console: http://127.0.0.1:{args.port}/#collections", flush=True)
        uvicorn.run(application, host="127.0.0.1", port=args.port, access_log=False)
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            with admin.connect() as connection:
                connection.exec_driver_sql(f"DROP DATABASE `{url.database}`")
        admin.dispose()


if __name__ == "__main__":
    main()
