"""Manual browser fixture server, restricted to a disposable local MySQL schema."""

import argparse
import os
import re

import uvicorn
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.main import create_app
from app.services.analytics_refresh import enabled
from app.services.analytics_runtime import AnalyticsRunner, backfill_batch, maintain
from app.settings import Settings
from app.storage.database import make_engine
from app.storage.schema import mysql_ddl
from test_product_detail import snapshot
from test_product_detail_storage import initial


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--port", type=int, default=18891)
    args = parser.parse_args()
    url = make_url(args.database_url)
    if url.host not in {"127.0.0.1", "localhost"} or not re.fullmatch(r"tiktok_shop_monitor_test_[a-zA-Z0-9_]+", url.database or ""):
        raise ValueError("Only a new disposable local test schema is permitted.")
    admin = make_engine(url, with_database=False)
    created = False
    engine = None
    db = None
    try:
        with admin.connect() as connection:
            if connection.scalar(text("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=:name"), {"name": url.database}):
                raise ValueError("Choose a new test schema name.")
            for statement in mysql_ddl(url.database).split(";"):
                statement = "\n".join(line for line in statement.splitlines() if not line.startswith("--")).strip()
                if statement:
                    connection.exec_driver_sql(statement)
                    if statement.startswith("CREATE DATABASE"):
                        created = True
        engine = make_engine(url)
        db = sessionmaker(engine)()
        from app.models import Product, Shop, CrawlTask, TaskType
        from app.services.product_detail import persist_product_detail
        from app.services.snapshot_service import persist_snapshot
        from app.services.change_detector import detect_snapshot_changes
        from sqlalchemy import select
        from decimal import Decimal
        product, task = initial(db)
        captured = snapshot()
        persist_product_detail(db, task, captured)
        persist_snapshot(db, task, captured, update_current=False)
        change = db.scalar(select(CrawlTask).where(CrawlTask.task_type == TaskType.CHANGE_DETECTION))
        detect_snapshot_changes(db, change)
        product.title = "TEST FIXTURE · Dress with observed SKUs"
        shop = db.get(Shop, product.shop_id)
        shop.canonical_name = "TEST FIXTURE · Alpha"
        shop.canonical_url = "https://shop.tiktok.com/"
        second = Shop(region="US", platform_shop_id="999900002", canonical_name="TEST FIXTURE · Beta",
                      canonical_url="https://shop.tiktok.com/")
        db.add(second)
        db.flush()
        for i in range(45):
            db.add(Product(region="US", shop_id=shop.id if i % 2 else second.id,
                platform_product_id=str(999910000+i), product_url=f"https://shop.tiktok.com/p/{999910000+i}",
                title=f"TEST FIXTURE · Sample {i:02d}", original_category="Dresses" if i % 2 else "Tops",
                price_min=Decimal(i+10), price_max=Decimal(i+10)))
        db.commit()
        os.environ["ANALYTICS_ENABLED"] = "1"
        config = Settings(database_url=args.database_url)
        factory = sessionmaker(engine, expire_on_commit=False)
        runner = AnalyticsRunner(factory, config)
        for _ in range(100):
            with factory.begin() as work:
                complete = backfill_batch(work, config)
            if not runner.run_once() and complete:
                break
        if not complete:
            raise RuntimeError("Fixture backfill failed.")
        with factory.begin() as work:
            maintain(work, config)
        for _ in range(32):
            if not runner.run_once():
                break
        print(f"Isolated UI fixture: http://127.0.0.1:{args.port}/", flush=True)
        # Keep the same production scheduler behavior while the browser changes filters.
        import threading
        stop = threading.Event()
        def background():
            while not stop.wait(0.3):
                with factory.begin() as work:
                    maintain(work, config)
                runner.run_once()
        worker = threading.Thread(target=background, daemon=True)
        worker.start()
        try:
            application = create_app(config)
            @application.middleware("http")
            async def public_prefix(request, call_next):
                # Mirror the deployed Nginx prefix stripping, including native API I/O.
                if request.scope["path"].startswith("/shopscope/"):
                    request.scope["path"] = request.scope["path"][len("/shopscope"):]
                    request.scope["raw_path"] = request.scope["path"].encode()
                return await call_next(request)
            uvicorn.run(application, host="127.0.0.1", port=args.port, access_log=False)
        finally:
            stop.set()
            worker.join(timeout=10)
    finally:
        if db:
            db.close()
        if engine:
            engine.dispose()
        if created:
            with admin.connect() as connection:
                connection.exec_driver_sql(f"DROP DATABASE `{url.database}`")
        admin.dispose()


if __name__ == "__main__":
    main()
