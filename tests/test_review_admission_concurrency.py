"""Automatic review admission must not wait behind a worker holding its task."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError

from app.models import CrawlTask, Product, ProductReview, Shop, TaskStatus
from app.services.product_reviews import ReviewCollectionBusy, enqueue_initial_review_tasks, ensure_initial_review_task, ensure_review_task, persist_review_page
from app.services.task_runtime import ExecutionResult, claim_task, database_now, finalize_task
from test_collection_launch_concurrency import isolated_factory
from test_review_storage import CONFIG, setup, snapshot


def extra_product(db, shop_id, number):
    row = Product(region="US", shop_id=shop_id, platform_product_id=str(number),
        product_url=f"https://shop.tiktok.com/us/pdp/{number}",
        last_success_at=database_now(db), review_count=10)
    db.add(row)
    db.flush()
    return row


@pytest.mark.parametrize("mode", ["initial", "batch", "manual"])
def test_review_completion_and_automatic_admission_do_not_deadlock(isolated_factory, monkeypatch, mode):
    factory = isolated_factory
    with factory.begin() as db:
        product, task = setup(db, max_pages=1)
        product.last_success_at, product.review_count = database_now(db), 38
        product_id = product.id
        other_id = extra_product(db, product.shop_id, 9001).id
        lease = claim_task(db, task.task_id, task.task_type, "finishing-reviews", CONFIG)
        stamp = task.available_at
    monkeypatch.setattr("app.services.product_reviews.database_now", lambda db: stamp)
    task_locked, product_locked = Event(), Event()
    engine = factory.kw["bind"]

    def observe_product_lock(connection, cursor, statement, parameters, context, executemany):
        if connection.info.get("test_role") == "admission" and "FROM products " in statement and "FOR UPDATE" in statement:
            product_locked.set()

    def source():
        try:
            with factory.begin() as db:
                db.connection().info["test_role"] = "source"
                def apply(session, current):
                    task_locked.set()
                    assert product_locked.wait(8), "Admission did not lock its product."
                    persist_review_page(session, current, snapshot())
                return finalize_task(db, lease, CONFIG, result=ExecutionResult(apply=apply)).value
        except OperationalError as error:
            return "mysql_error", error.orig.args[0]

    def admit():
        assert task_locked.wait(8), "Source did not lock its task."
        try:
            with factory.begin() as db:
                db.connection().info["test_role"] = "admission"
                if mode == "initial":
                    row, created = ensure_initial_review_task(db, CONFIG, product_id)
                    return "initial", row is None, created
                if mode == "manual":
                    ensure_review_task(db, CONFIG, product_id, max_pages=1)
                    return "unexpected_manual_success"
                return "batch", enqueue_initial_review_tasks(db, CONFIG, [product_id, other_id])
        except ReviewCollectionBusy as error:
            assert error.__suppress_context__
            return "busy", str(error)
        except OperationalError as error:
            return "mysql_error", error.orig.args[0]

    event.listen(engine, "after_cursor_execute", observe_product_lock)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            completed, admitted = pool.submit(source), pool.submit(admit)
            result = {"source": completed.result(timeout=15), "admission": admitted.result(timeout=15)}
        expected = {"initial": ("initial", True, False), "batch": ("batch", 1), "manual": ("busy", "review_collection_busy")}[mode]
        assert result == {"source": "succeeded", "admission": expected}
    finally:
        event.remove(engine, "after_cursor_execute", observe_product_lock)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(ProductReview)) == 5
        assert db.scalar(select(func.count()).select_from(CrawlTask)) == (2 if mode == "batch" else 1)


def new_shop(db):
    row = Shop(region="US", platform_shop_id="789000000000001", canonical_name="Admission fixture")
    db.add(row)
    db.flush()
    return row


@pytest.mark.parametrize("mode", ["initial", "batch"])
def test_existing_manual_task_is_seen_after_an_old_repeatable_read_snapshot(isolated_factory, mode):
    factory = isolated_factory
    with factory.begin() as db:
        product_id = extra_product(db, new_shop(db).id, 9002).id
    with factory() as stale:
        assert stale.connection().get_isolation_level() == "REPEATABLE READ"
        assert stale.scalar(select(func.count()).select_from(CrawlTask)) == 0
        with factory.begin() as fresh:
            manual, created = ensure_review_task(fresh, CONFIG, product_id, max_pages=20)
            assert created
            manual_id = manual.id
            manual.status, manual.finished_at = TaskStatus.DEAD, database_now(fresh)
        assert stale.scalar(select(func.count()).select_from(CrawlTask)) == 0
        if mode == "initial":
            existing, created = ensure_initial_review_task(stale, CONFIG, product_id)
            assert not created and existing.id == manual_id
            assert existing.status == TaskStatus.DEAD and existing.payload["max_pages"] == 20
        else:
            assert enqueue_initial_review_tasks(stale, CONFIG, [product_id]) == 0
        stale.commit()
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(CrawlTask)) == 1


def test_single_initial_admission_refreshes_product_eligibility_in_identity_map(isolated_factory):
    factory = isolated_factory
    with factory.begin() as db:
        product = extra_product(db, new_shop(db).id, 9003)
        product.last_success_at = product.review_count = None
        product_id = product.id
    with factory() as stale:
        cached = stale.get(Product, product_id)
        assert cached.last_success_at is None and cached.review_count is None
        with factory.begin() as fresh:
            current = fresh.get(Product, product_id)
            current.last_success_at, current.review_count = database_now(fresh), 10
        task, created = ensure_initial_review_task(stale, CONFIG, product_id)
        assert created and task.resource_id == str(product_id)
        assert cached.last_success_at is not None and cached.review_count == 10
        stale.commit()


def test_simultaneous_single_and_batch_initial_admission_create_one_task(isolated_factory):
    factory = isolated_factory
    with factory.begin() as db:
        product_id = extra_product(db, new_shop(db).id, 9004).id
    both_read = Barrier(2)

    def admit(mode):
        with factory.begin() as db:
            assert db.scalar(select(func.count()).select_from(CrawlTask)) == 0
            both_read.wait(timeout=8)
            if mode == "single":
                return int(ensure_initial_review_task(db, CONFIG, product_id)[1])
            return enqueue_initial_review_tasks(db, CONFIG, [product_id])

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(admit, mode) for mode in ("single", "batch")]
        assert sorted(future.result(timeout=15) for future in futures) == [0, 1]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(CrawlTask)) == 1


def test_uncontended_batch_uses_one_task_lookup_and_one_bulk_insert(isolated_factory):
    factory = isolated_factory
    with factory.begin() as db:
        shop_id = new_shop(db).id
        product_ids = [extra_product(db, shop_id, 10000 + index).id for index in range(50)]
    statements = []
    engine = factory.kw["bind"]

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append((statement, executemany, len(parameters)))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with factory.begin() as db:
            assert enqueue_initial_review_tasks(db, CONFIG, product_ids) == 50
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    lookups = [query for query, _, _ in statements if "FROM crawl_tasks " in query]
    inserts = [(many, count) for query, many, count in statements if query.startswith("INSERT INTO crawl_tasks")]
    assert len(lookups) == 1 and "FOR UPDATE NOWAIT" in lookups[0]
    assert inserts == [(True, 50)]
