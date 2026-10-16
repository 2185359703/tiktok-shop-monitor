"""Exercise MySQL-specific projection, JSON, session and lease behavior."""

from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from app.models.analytics_read import AnalyticsSnapshot
from app.services.analytics_projection import build_product, publish_product
from app.services.analytics_queries import list_products, parameters
from app.services.analytics_refresh import claim, complete, enqueue, now
from app.services.analytics_runtime import source_reader
from app.services.analytics_snapshot import build_snapshot, publish_snapshot, read_snapshot
from app.services.product_detail import persist_product_detail
from app.services.snapshot_service import persist_snapshot
from app.settings import Settings
from test_product_detail import snapshot
from test_product_detail_storage import initial


def test_native_async_api_can_read_and_schedule_snapshots(mysql_engine, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy import delete
    from app.main import create_app
    from app.models.analytics_read import AnalyticsRefreshRequest
    from app.services.analytics_refresh import job_row, scope
    factory = sessionmaker(mysql_engine)
    config = Settings(database_url=mysql_engine.url.render_as_string(hide_password=False))
    monkeypatch.setenv("ANALYTICS_ENABLED", "1")
    try:
        with factory.begin() as db:
            row = job_row(db, "control", {**scope(config), "name": "backfill"})
            row.parameters = {**row.parameters, "ready": True}
        with TestClient(create_app(config)) as client:
            assert client.get("/api/v2/ui/bootstrap").json()["ready"]
            assert client.get("/api/v2/catalog/products").status_code == 200
            response = client.get("/api/v2/catalog/stats")
            assert response.status_code == 202
            assert response.json()["_meta"]["status"] == "pending"
    finally:
        with factory.begin() as db:
            db.execute(delete(AnalyticsSnapshot))
            db.execute(delete(AnalyticsRefreshRequest))


def test_mysql_projection_and_snapshots_preserve_real_snapshot_fields(session, monkeypatch):
    from app.services import analytics_projection
    product, task = initial(session)
    page = snapshot()
    persist_product_detail(session, task, page)
    task.payload = {**task.payload, "pricing_context": "public_listed"}
    persist_snapshot(session, task, page, update_current=False)
    config = Settings()
    stamp = now(session)
    monkeypatch.setattr(analytics_projection, "now", lambda db: stamp)
    rows = build_product(session, config, product.id, 1)
    assert analytics_projection.build_products(session, config, {product.id: 1}) == rows
    publish_product(session, config, rows)
    current = list_products(session, parameters(config))
    assert current["items"][0]["variant_count"] == 2
    assert len(current["items"]) == 1
    historical = next(row for row in rows if row["basis"] == "latest_valid_snapshot")["payload"]
    assert historical["title"] == page.detail.title
    assert historical["sku_count"] == 2 and historical["snapshot_id"] is not None
    for kind in ("overview", "workbench", "catalog_stats", "events_summary", "task_counts", "matching_candidates"):
        params = parameters(config)
        result, stamp = build_snapshot(session, config, kind, params)
        publish_snapshot(session, kind, params, 1, result, stamp)
        session.flush()
        assert read_snapshot(session, kind, params)[0] == 200


def test_mysql_refresh_coalescing_is_fenced(session):
    row = enqueue(session, "catalog_stats", parameters(Settings()))
    lease = claim(session, Settings())
    assert lease is not None
    enqueue(session, "catalog_stats", parameters(Settings()), force=True)
    complete(session, lease, lambda db: publish_snapshot(db, lease.kind, lease.parameters, lease.revision,
                                                       {"total": 0}, now(db)))
    assert row.requested_revision == 2 and row.completed_revision == 1


def test_mysql_query_budgets_are_restored_on_pool_return(mysql_engine):
    factory = sessionmaker(mysql_engine)
    with factory() as db:
        original = db.execute(text("SELECT @@SESSION.max_execution_time, @@SESSION.innodb_lock_wait_timeout")).one()
    with source_reader(factory) as db:
        assert db.scalar(text("SELECT @@SESSION.max_execution_time")) == 3000
        assert db.scalar(text("SELECT @@SESSION.innodb_lock_wait_timeout")) == 1
    with factory() as db:
        assert db.execute(text("SELECT @@SESSION.max_execution_time, @@SESSION.innodb_lock_wait_timeout")).one() == original
    with source_reader(factory, select_timeout_ms=30000) as db:
        assert db.scalar(text("SELECT @@SESSION.max_execution_time")) == 30000
    with factory() as db:
        assert db.execute(text("SELECT @@SESSION.max_execution_time, @@SESSION.innodb_lock_wait_timeout")).one() == original
    with pytest.raises(RuntimeError):
        with source_reader(factory) as db:
            raise RuntimeError("interrupted")
    with factory() as db:
        assert db.execute(text("SELECT @@SESSION.max_execution_time, @@SESSION.innodb_lock_wait_timeout")).one() == original


def test_native_async_worker_publishes_without_sync_driver_io(mysql_engine, monkeypatch):
    import asyncio
    from sqlalchemy import delete
    from app.models.analytics_read import AnalyticsRefreshRequest
    from app.services.analytics_refresh import key_for
    from app.services.async_analytics import AsyncAnalyticsRunner
    from app.storage.async_database import AsyncDatabase, make_async_engine
    config = Settings(database_url=mysql_engine.url.render_as_string(hide_password=False), analytics_concurrency=1)
    monkeypatch.setenv("ANALYTICS_ENABLED", "1")
    params = parameters(config, {"q": "ASYNC TEST FIXTURE"})
    async def check():
        database = AsyncDatabase(make_async_engine(config.database_url))
        try:
            for identifier in (90001, 90002):
                await database.transaction(enqueue, "product_projection", {"product_id": str(identifier)})
            assert await AsyncAnalyticsRunner(database, config).run_once()
            completed = await database.read(lambda db: db.scalars(select(AnalyticsRefreshRequest.completed_revision)
                .where(AnalyticsRefreshRequest.kind == "product_projection")).all())
            assert completed == [1, 1]
            await database.transaction(enqueue, "catalog_stats", params)
            assert await AsyncAnalyticsRunner(database, config).run_once()
            value = await database.read(lambda db: db.get(AnalyticsSnapshot, (2, key_for("catalog_stats", params))).payload)
            assert value["total"] == 0
        finally:
            await database.close()
    try:
        asyncio.run(check())
    finally:
        with sessionmaker(mysql_engine).begin() as db:
            db.execute(delete(AnalyticsSnapshot))
            db.execute(delete(AnalyticsRefreshRequest))


def test_empty_statistics_invalidation_does_not_lock_new_projection_requests(mysql_engine):
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import delete
    from app.models.analytics_read import AnalyticsRefreshRequest
    from app.services.analytics_refresh import invalidate_statistics
    factory = sessionmaker(mysql_engine)
    try:
        with factory.begin() as db:
            enqueue(db, "product_projection", {"product_id": "10001"})
        with factory.begin() as publisher:
            invalidate_statistics(publisher, Settings())
            def insert_next():
                with factory.begin() as writer:
                    writer.execute(text("SET SESSION innodb_lock_wait_timeout=1"))
                    try:
                        enqueue(writer, "product_projection", {"product_id": "10002"})
                    finally:
                        writer.execute(text("SET SESSION innodb_lock_wait_timeout=50"))
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(insert_next).result(timeout=4)
    finally:
        with factory.begin() as db:
            db.execute(delete(AnalyticsRefreshRequest))


def test_bulk_backfill_coalesces_pending_jobs_without_losing_revisions_or_retry_times(session):
    from app.models.analytics_read import AnalyticsRefreshRequest as Job
    from app.services.analytics_refresh import enqueue_many, key_for
    kind = "product_projection"
    first, second, third = [{"product_id": str(identifier)} for identifier in (100, 101, 102)]
    finished = enqueue(session, kind, first)
    finished.completed_revision = 1
    finished.is_pending = False
    finished.pending_since = None
    pending = enqueue(session, kind, second, force=True)
    pending.requested_revision = 7
    pending.completed_revision = 4
    pending.lease_token = "00000000-0000-0000-0000-000000000001"
    retry_time = pending.available_at = now(session) + timedelta(minutes=2)
    session.flush()
    enqueue_many(session, kind, [first, second, third, first])
    session.expire_all()
    assert finished.requested_revision == 2 and finished.completed_revision == 1 and finished.is_pending
    assert finished.pending_since is not None
    assert pending.requested_revision == 7 and pending.completed_revision == 4 and pending.available_at == retry_time
    assert pending.lease_token == "00000000-0000-0000-0000-000000000001"
    assert session.get(Job, (2, kind, key_for(kind, third))).requested_revision == 1


def test_ui_access_indexes_migrate_once_without_changing_columns(mysql_engine):
    from sqlalchemy import inspect
    from scripts.migrate_ui_indexes import INDEXES, migrate
    def columns():
        inspector = inspect(mysql_engine)
        return {index.table.name: [(column["name"], str(column["type"]), column["nullable"])
            for column in inspector.get_columns(index.table.name)] for index in INDEXES}
    before = columns()
    with mysql_engine.connect() as connection:
        for index in INDEXES:
            connection.exec_driver_sql(f"ALTER TABLE `{index.table.name}` DROP INDEX `{index.name}`")
    assert migrate(mysql_engine)["pending_statements"] == 3
    result = migrate(mysql_engine, execute=True)
    assert result == {"ready": True, "executed_statements": 3, "pending_statements": 0, "data_rows_modified": 0}
    assert columns() == before
    assert migrate(mysql_engine, execute=True)["executed_statements"] == 0
