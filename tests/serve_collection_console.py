"""Real local API for browser checks; synthetic data, no broker or source requests."""

import argparse
import asyncio
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import uvicorn
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.main import create_app
from app.models import CrawlAttempt, CrawlTask, Product, ResourceType, Shop, TaskStatus, TaskType
from app.services.analytics_runtime import AnalyticsRunner, backfill_batch, maintain
from app.services.collection_rollups import activate, advance_rollups
from app.services.collection_monitor import read_monitor
from app.services.collection_alerts import evaluate_snapshot
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
                          product_url="https://shop.tiktok.com/us/pdp/7490000000000000002",
                          title="TEST FIXTURE · Cotton shirt", last_success_at=now,
                          last_seen_at=now, price_min=19, price_max=24)
        db.add(product)
        db.flush()
        for index in range(1, 32):
            shop_id, product_id = str(7490000000000001000 + index), str(7490000000000002000 + index)
            extra = Shop(region="US", platform_shop_id=shop_id, canonical_name=f"TEST FIXTURE · 资源选择店铺 {index:02}",
                canonical_url=f"https://shop.tiktok.com/us/store/fixture-{index}/{shop_id}")
            db.add(extra)
            db.flush()
            db.add(Product(region="US", shop_id=extra.id, platform_product_id=product_id,
                product_url=f"https://shop.tiktok.com/us/pdp/{product_id}", title=f"TEST FIXTURE · Shirt {index:02}",
                last_success_at=now, last_seen_at=now, price_min=10 + index))
        for index in range(36):
            failed = index % 3 == 0
            ended = now - timedelta(minutes=35-index)
            task = CrawlTask(region="US", task_type=TaskType.SHOP_PROFILE,
                             resource_type=ResourceType.SHOP, resource_id=str(shop.id),
                             payload={"shop_id": str(shop.id), "source_url": shop.canonical_url,
                                      "shop_url": shop.canonical_url, "platform_shop_id": shop.platform_shop_id},
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
                         payload={"shop_id": str(shop.id), "source_url": shop.canonical_url,
                                  "shop_url": shop.canonical_url, "platform_shop_id": shop.platform_shop_id},
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
                          enabled_task_types=tuple(TaskType))
        seed(factory)
        runner = AnalyticsRunner(factory, config)
        for _ in range(256):
            with factory.begin() as db:
                complete = backfill_batch(db, config)
            if not runner.run_once() and complete:
                break
        with factory.begin() as db:
            maintain(db, config)
        for _ in range(32):
            if not runner.run_once():
                break
        with factory.begin() as db:
            activate(db, config)
            advance_rollups(db, config, force_publish=True)
        application = create_app(config)
        original_lifespan = application.router.lifespan_context

        def refresh_derived():
            with factory.begin() as db:
                backfill_batch(db, config)
                maintain(db, config)
                advance_rollups(db, config)
            for _ in range(8):
                if not runner.run_once():
                    break
            with factory.begin() as db:
                snapshot = read_monitor(db, config)
                snapshot["runtime"] = {"status": "not_configured", "components": [],
                    "observed_at": snapshot["generated_at"], "source_cooldown_seconds": None}
                evaluate_snapshot(db, config, snapshot)

        @asynccontextmanager
        async def fixture_lifespan(app):
            async with original_lifespan(app):
                async def update():
                    while True:
                        await asyncio.to_thread(refresh_derived)
                        await asyncio.sleep(10)
                background = asyncio.create_task(update())
                try:
                    yield
                finally:
                    background.cancel()
                    await asyncio.gather(background, return_exceptions=True)
        application.router.lifespan_context = fixture_lifespan

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
