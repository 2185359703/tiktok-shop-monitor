"""Real InnoDB admission races, with one disposable database per test."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text

from app.crawlers.public_shop import PublicPageSnapshot, ShopCandidate
from app.models import CollectionLaunch, CollectionOperation, CrawlTask, Product, ResolutionStatus, SeedShopObservation, Shop, TaskStatus, TaskType
from app.services.collection_intake import create_launch
from app.services.collection_management import ACTIVE_STATES, ManagementError, retry_task
from app.services.shop_discovery import resolve_seed_from_snapshot
from app.services.task_runtime import ExecutionResult, claim_task, database_now, finalize_task
from app.storage.database import make_engine, make_session_factory
from app.storage.schema import mysql_ddl
from test_collection_intake import CONFIG, definition, product, roots, store


GUARDED_KINDS = (TaskType.PRODUCT_DISCOVERY, TaskType.PRODUCT_DETAIL, TaskType.PRODUCT_REVIEWS)


@pytest.fixture
def isolated_factory(mysql_engine):
    name = "tiktok_shop_monitor_test_launch_" + uuid4().hex[:12]
    assert len(name) <= 64 and name.startswith("tiktok_shop_monitor_test_")
    url = mysql_engine.url.set(database=name)
    admin = make_engine(url, with_database=False)
    engine, created = None, False
    try:
        with admin.connect() as connection:
            assert connection.scalar(text("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = :name"),
                {"name": name}) is None
            for statement in mysql_ddl(name).split(";"):
                statement = "\n".join(line for line in statement.splitlines() if not line.lstrip().startswith("--")).strip()
                if statement:
                    connection.exec_driver_sql(statement)
                    if statement.startswith("CREATE DATABASE"):
                        created = True
        engine = make_engine(url)
        @event.listens_for(engine, "connect")
        def bound_lock_waits(connection, _):
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION innodb_lock_wait_timeout = 5")
        yield make_session_factory(engine)
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            with admin.connect() as connection:
                connection.exec_driver_sql(f"DROP DATABASE {name}")
        admin.dispose()


def resource_fixture(db, kind):
    shop = store(db)
    return shop if kind == TaskType.PRODUCT_DISCOVERY else product(db, shop)


def launch_fixture(db, kind, resource_id, *, dead=False):
    launch, created = create_launch(db, CONFIG, definition([resource_id], kind=kind), uuid4())
    assert created
    task = roots(db, launch)[0]
    if dead:
        task.status, task.finished_at = TaskStatus.DEAD, database_now(db)
        db.flush()
    return launch, task


def active_count(db):
    return db.scalar(select(func.count()).select_from(CrawlTask).where(CrawlTask.status.in_(ACTIVE_STATES)))


@pytest.mark.parametrize("kind", GUARDED_KINDS)
@pytest.mark.parametrize("operation", ["launch", "retry"])
def test_source_finalization_and_admission_do_not_invert_task_resource_locks(isolated_factory, kind, operation):
    factory = isolated_factory
    with factory.begin() as db:
        resource_id = resource_fixture(db, kind).id
        _, previous = launch_fixture(db, kind, resource_id, dead=True)
        previous_uuid = previous.task_id
        _, current = launch_fixture(db, kind, resource_id)
        lease = claim_task(db, current.task_id, kind, "concurrency-fixture", CONFIG)
        assert lease is not None

    task_locked, resource_locked = Event(), Event()
    engine = factory.kw["bind"]
    table = "shops" if kind == TaskType.PRODUCT_DISCOVERY else "products"

    def resource_was_locked(connection, cursor, statement, parameters, context, executemany):
        if connection.info.get("test_role") == "admission" and f"FROM {table} " in statement and "FOR UPDATE" in statement:
            resource_locked.set()

    def finalize():
        with factory.begin() as db:
            db.connection().info["test_role"] = "source"
            def apply(session, task):
                task_locked.set()
                assert resource_locked.wait(8), "Admission did not acquire its resource lock."
                model = Shop if kind == TaskType.PRODUCT_DISCOVERY else Product
                resource = session.scalar(select(model).where(model.id == resource_id).with_for_update())
                if model is Shop:
                    resource.canonical_name = "Completed source fixture"
                else:
                    resource.title = "Completed source fixture"
            return finalize_task(db, lease, CONFIG, result=ExecutionResult(apply=apply))

    def admit():
        assert task_locked.wait(8), "Source did not acquire its task lock."
        try:
            with factory.begin() as db:
                db.connection().info["test_role"] = "admission"
                if operation == "launch":
                    create_launch(db, CONFIG, definition([resource_id], kind=kind), uuid4())
                else:
                    retry_task(db, CONFIG, previous_uuid)
        except ManagementError as error:
            return str(error)
        return "unexpected_success"

    event.listen(engine, "after_cursor_execute", resource_was_locked)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            completion, admission = pool.submit(finalize), pool.submit(admit)
            assert admission.result(timeout=15) == "collection_resource_busy"
            assert completion.result(timeout=15) == TaskStatus.SUCCEEDED
    finally:
        event.remove(engine, "after_cursor_execute", resource_was_locked)
    with factory() as db:
        assert active_count(db) == 0
        assert db.scalar(select(func.count()).select_from(CollectionLaunch)) == 2
        assert db.scalar(select(func.count()).select_from(CrawlTask)) == 2
        assert db.scalar(select(func.count()).select_from(CollectionOperation)) == 2


def test_duplicate_seed_import_releases_seed_before_source_resolution(isolated_factory, monkeypatch):
    from app.services import seed_importer
    factory = isolated_factory
    platform_id = "7490000000000000003"
    url = f"https://shop.tiktok.com/us/store/fixture/{platform_id}"
    body = definition(kind=TaskType.SHOP_DISCOVERY, urls=[url])
    with factory.begin() as db:
        launch, _ = create_launch(db, CONFIG, body, uuid4())
        current = roots(db, launch)[0]
        lease = claim_task(db, current.task_id, current.task_type, "seed-concurrency-fixture", CONFIG)
        seed_id = int(current.resource_id)
        stamp = database_now(db)
    snapshot = PublicPageSnapshot(source_url=url, final_url=url, status=200, response_bytes=100, response_hash="f" * 64,
        latency_ms=1, network_attempts=1, started_at=stamp, captured_at=stamp,
        candidates=(ShopCandidate(platform_id, "Resolved fixture", ("Resolved fixture",), url, "US", ("fixture",)),))
    task_locked, seed_locked = Event(), Event()
    original_ensure = seed_importer.ensure_seed_task

    def ensure_with_signal(db, seed, stamp):
        seed_locked.set()
        return original_ensure(db, seed, stamp)
    monkeypatch.setattr(seed_importer, "ensure_seed_task", ensure_with_signal)

    def complete():
        with factory.begin() as db:
            def apply(session, task):
                task_locked.set()
                assert seed_locked.wait(8), "Duplicate import did not acquire its seed lock."
                outcome, _ = resolve_seed_from_snapshot(session, seed_id, snapshot, "fixture-safe-evidence",
                    runtime_task_id=lease.task_id, runtime_lease_token=lease.lease_token)
                assert outcome == "resolved"
            return finalize_task(db, lease, CONFIG, result=ExecutionResult(apply=apply))

    def duplicate_import():
        assert task_locked.wait(8), "Source did not acquire its task lock."
        try:
            with factory.begin() as db:
                create_launch(db, CONFIG, body, uuid4())
        except ManagementError as error:
            return str(error)
        return "unexpected_success"

    with ThreadPoolExecutor(max_workers=2) as pool:
        completion, duplicate = pool.submit(complete), pool.submit(duplicate_import)
        assert duplicate.result(timeout=15) == "collection_resource_busy"
        assert completion.result(timeout=15) == TaskStatus.SUCCEEDED
    with factory() as db:
        assert db.get(SeedShopObservation, seed_id).resolution_status == ResolutionStatus.RESOLVED
        assert db.scalar(select(func.count()).select_from(CollectionLaunch)) == 1
        assert db.scalar(select(func.count()).select_from(CollectionOperation)) == 1
        assert db.scalar(select(func.count()).select_from(CrawlTask).where(CrawlTask.task_type == TaskType.SHOP_DISCOVERY)) == 1


@pytest.mark.parametrize("kind", GUARDED_KINDS)
def test_new_launch_and_failed_root_retry_admit_only_one_active_task(isolated_factory, kind):
    factory = isolated_factory
    with factory.begin() as db:
        resource_id = resource_fixture(db, kind).id
        _, original = launch_fixture(db, kind, resource_id, dead=True)
        original_uuid = original.task_id
    start = Barrier(2)

    def admit(operation):
        start.wait(timeout=8)
        try:
            with factory.begin() as db:
                if operation == "launch":
                    create_launch(db, CONFIG, definition([resource_id], kind=kind), uuid4())
                else:
                    retry_task(db, CONFIG, original_uuid)
            return "accepted"
        except ManagementError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(admit, value) for value in ("launch", "retry")]
        assert sorted(future.result(timeout=15) for future in futures) == ["accepted", "collection_resource_busy"]
    with factory() as db:
        assert active_count(db) == 1
        assert db.scalar(select(func.count()).select_from(CrawlTask)) == 2
        assert db.scalar(select(func.count()).select_from(CollectionOperation)) == 2


@pytest.mark.parametrize("kind", GUARDED_KINDS)
def test_retry_checks_current_tasks_after_old_repeatable_read_snapshot(isolated_factory, kind):
    factory = isolated_factory
    with factory.begin() as db:
        resource_id = resource_fixture(db, kind).id
        _, original = launch_fixture(db, kind, resource_id, dead=True)
        original_uuid = original.task_id
    with factory() as stale:
        assert stale.connection().get_isolation_level() == "REPEATABLE READ"
        assert active_count(stale) == 0
        with factory.begin() as fresh:
            launch_fixture(fresh, kind, resource_id)
        assert active_count(stale) == 0, "The test must retain the earlier consistent-read snapshot."
        with pytest.raises(ManagementError, match="collection_resource_busy"):
            retry_task(stale, CONFIG, original_uuid)
        stale.rollback()
    with factory() as db:
        assert active_count(db) == 1
        assert db.scalar(select(func.count()).select_from(CrawlTask)) == 2


def test_simultaneous_request_uuid_replays_one_launch_and_audit(isolated_factory):
    factory = isolated_factory
    with factory.begin() as db:
        resource_id = resource_fixture(db, TaskType.PRODUCT_DISCOVERY).id
    engine, request, both_read = factory.kw["bind"], uuid4(), Barrier(2)

    def synchronize_initial_reads(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT ") and "FROM collection_launches " in statement and "collection_launches.request_key =" in statement and "FOR UPDATE" not in statement:
            both_read.wait(timeout=8)

    def create():
        with factory.begin() as db:
            launch, created = create_launch(db, CONFIG, definition([resource_id]), request)
            return launch.id, created

    event.listen(engine, "after_cursor_execute", synchronize_initial_reads)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(create) for _ in range(2)]
            results = [future.result(timeout=15) for future in futures]
        assert results[0][0] == results[1][0]
        assert sorted(value[1] for value in results) == [False, True]
    finally:
        event.remove(engine, "after_cursor_execute", synchronize_initial_reads)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(CollectionLaunch)) == 1
        assert db.scalar(select(func.count()).select_from(CrawlTask)) == 1
        assert db.scalar(select(func.count()).select_from(CollectionOperation)) == 1


def test_existing_retry_is_replayed_after_cancellation_and_new_active_launch(isolated_factory):
    factory = isolated_factory
    with factory.begin() as db:
        resource_id = resource_fixture(db, TaskType.PRODUCT_DISCOVERY).id
        launch, original = launch_fixture(db, TaskType.PRODUCT_DISCOVERY, resource_id, dead=True)
        child, created = retry_task(db, CONFIG, original.task_id)
        assert created
        child.status, child.finished_at = TaskStatus.DEAD, database_now(db)
        launch.cancel_requested_at = launch.cancel_completed_at = database_now(db)
        db.flush()
        launch_fixture(db, TaskType.PRODUCT_DISCOVERY, resource_id)
        replay, created = retry_task(db, CONFIG, original.task_id)
        assert not created and replay.id == child.id
        assert active_count(db) == 1
        assert db.scalar(select(func.count()).select_from(CrawlTask)) == 3
