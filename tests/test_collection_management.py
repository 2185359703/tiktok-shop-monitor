import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from threading import Barrier, Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, inspect, select, text
from sqlalchemy.orm import sessionmaker

from app.main import create_app
from app.models import CollectionControl, CollectionOperation, CollectionPlan, CollectionRun, CollectionRunTask, CrawlAttempt, CrawlTask, Product, ResourceType, Shop, TaskStatus, TaskType
from app.observability.metrics import Metrics
from app.services.collection_management import ManagementError, cancel_task, create_plan, enqueue_due_collection_plans, is_collection_paused, list_resources, retry_task, run_plan, run_summary, set_control, set_plan_archived, update_plan
from app.services.scheduler import Scheduler
from app.services.task_runtime import ExecutionResult, claim_task, database_now, finalize_task
from app.settings import Settings
from app.storage.database import make_session_factory, session_scope
from scripts.migrate_collection_management import additive_sql


CONFIG = Settings(enabled_task_types=(TaskType.SHOP_PROFILE, TaskType.PRODUCT_SNAPSHOT,
    TaskType.PRODUCT_DISCOVERY, TaskType.PRODUCT_DETAIL, TaskType.CHANGE_DETECTION, TaskType.REPORT_GENERATION),
    api_token="collection-management-test-only-token")
PREFIX = "/api/v1/collection-management"
AUTH = {"Authorization": f"Bearer {CONFIG.api_token}"}


def shop(db, *, region="US"):
    platform_id = str(uuid4().int)[:18]
    row = Shop(platform="tiktok_shop", region=region, platform_shop_id=platform_id, canonical_name="Management fixture",
        canonical_url=f"https://www.tiktok.com/shop/store/management-fixture/{platform_id}")
    db.add(row)
    db.flush()
    return row


def product(db, store):
    platform_id = str(uuid4().int)[:18]
    row = Product(platform="tiktok_shop", region=store.region, shop_id=store.id,
        platform_product_id=platform_id, title="Management product", product_url=f"https://shop.tiktok.com/us/pdp/{platform_id}",
        currency="USD", pricing_context="public_listed", last_success_at=database_now(db))
    db.add(row)
    db.flush()
    return row


def definition(ids, *, kind=TaskType.SHOP_PROFILE, mode="interval", enabled=True):
    return {"name": "测试采集计划", "task_type": kind.value, "resource_ids": [str(value) for value in ids],
            "schedule_mode": mode, "interval_seconds": (3600 if kind == TaskType.SHOP_PROFILE else 60) if mode == "interval" else None,
            "enabled": enabled}


def task(db, *, kind=TaskType.PRODUCT_DETAIL, status=TaskStatus.PENDING, region="US"):
    now = database_now(db)
    row = CrawlTask(platform="tiktok_shop", region=region, task_type=kind, resource_type=ResourceType.PRODUCT,
        resource_id="1", payload={"product_id": "1"}, checkpoint={"page_number": 2}, status=status,
        scheduled_for=now, available_at=now - timedelta(seconds=1))
    db.add(row)
    db.flush()
    return row


def test_plan_schedule_coalesces_missed_periods_and_tracks_exact_roots(session):
    stores = [shop(session), shop(session)]
    plan, created = create_plan(session, CONFIG, definition([value.id for value in stores]), uuid4())
    assert created
    now = database_now(session)
    original_due = now - timedelta(hours=5, minutes=30)
    plan.next_run_at = original_due
    session.flush()
    assert enqueue_due_collection_plans(session, CONFIG) == 2
    assert enqueue_due_collection_plans(session, CONFIG) == 0
    runs = session.scalars(select(CollectionRun)).all()
    assert len(runs) == 1
    run = runs[0]
    assert run.scheduled_for == original_due + timedelta(hours=5)
    assert plan.next_run_at == original_due + timedelta(hours=6)
    report = run_summary(session, run, include_tasks=True)
    assert report["status"] == "queued" and report["task_count"] == 2
    assert report["coverage_scope"] == "root_tasks"
    members = session.scalars(select(CrawlTask).join(CollectionRunTask, CollectionRunTask.task_id == CrawlTask.id)).all()
    assert {row.resource_id for row in members} == {str(value.id) for value in stores}
    assert all(row.payload["scheduled_for"] == run.scheduled_for.isoformat() for row in members)
    members[0].status = TaskStatus.SUCCEEDED
    members[1].status = TaskStatus.DEAD
    session.flush()
    assert run_summary(session, run)["status"] == "partial"


def test_manual_run_replay_is_idempotent_and_overlapping_roots_are_rejected(session):
    plan, _ = create_plan(session, CONFIG, definition([shop(session).id], enabled=False), uuid4())
    request_id = uuid4()
    first, created = run_plan(session, CONFIG, plan.id, request_id)
    assert created and not plan.enabled
    replay, created = run_plan(session, CONFIG, plan.id, request_id)
    assert replay.id == first.id and not created
    with pytest.raises(ManagementError, match="collection_plan_busy"):
        run_plan(session, CONFIG, plan.id, uuid4())
    assert session.scalar(select(func.count()).select_from(CrawlTask)) == 1
    set_control(session, CONFIG, True)
    assert run_plan(session, CONFIG, plan.id, request_id)[0].id == first.id
    with pytest.raises(ManagementError, match="collection_paused"):
        run_plan(session, CONFIG, plan.id, uuid4())


def test_once_plan_is_disabled_after_one_dispatch_and_snapshot_payload_is_executable(session):
    item = product(session, shop(session))
    plan, _ = create_plan(session, CONFIG, definition([item.id], kind=TaskType.PRODUCT_SNAPSHOT, mode="once"), uuid4())
    assert enqueue_due_collection_plans(session, CONFIG) == 1
    assert not plan.enabled and plan.next_run_at is None
    row = session.scalar(select(CrawlTask))
    assert row.payload["product_id"] == str(item.id)
    assert row.payload["platform_product_id"] == item.platform_product_id
    assert row.payload["product_url"] == item.product_url
    assert row.task_type == TaskType.PRODUCT_SNAPSHOT
    assert enqueue_due_collection_plans(session, CONFIG) == 0


def test_resource_validation_is_market_scoped_and_failed_run_cannot_partially_enqueue(session):
    foreign = shop(session, region="GB")
    with pytest.raises(ManagementError, match="invalid_plan_resources"):
        create_plan(session, CONFIG, definition([foreign.id]), uuid4())
    stores = [shop(session), shop(session)]
    plan, _ = create_plan(session, CONFIG, definition([row.id for row in stores]), uuid4())
    stores[1].canonical_url = None
    session.flush()
    assert enqueue_due_collection_plans(session, CONFIG) == 0
    assert session.scalar(select(func.count()).select_from(CrawlTask)) == 0
    report = run_summary(session, session.scalar(select(CollectionRun)))
    assert report["status"] == "failed" and report["error_code"] == "invalid_plan_resources"
    assert report["requested_count"] == 2 and report["task_count"] == 0


def test_pause_blocks_queued_claims_but_preserves_inflight_and_local_work(session):
    source, queued, local = task(session), task(session), task(session, kind=TaskType.CHANGE_DETECTION)
    lease = claim_task(session, source.task_id, source.task_type, "management-worker", CONFIG)
    set_control(session, CONFIG, True)
    assert is_collection_paused(session, CONFIG)
    assert claim_task(session, queued.task_id, queued.task_type, "management-worker", CONFIG) is None
    assert queued.status == TaskStatus.PENDING and queued.attempt_count == 0
    assert finalize_task(session, lease, CONFIG, result=ExecutionResult()) == TaskStatus.SUCCEEDED
    assert claim_task(session, local.task_id, local.task_type, "management-worker", CONFIG) is not None
    set_control(session, CONFIG, False)
    assert claim_task(session, queued.task_id, queued.task_type, "management-worker", CONFIG) is not None
    assert [row.action for row in session.scalars(select(CollectionOperation).order_by(CollectionOperation.id))] == [
        "collection_paused", "collection_resumed"]


def test_pause_resume_preserves_protective_cooldown_and_scheduler_keeps_local_dispatch(session):
    source, local = task(session), task(session, kind=TaskType.CHANGE_DETECTION)
    store = shop(session)
    plan, _ = create_plan(session, CONFIG, definition([store.id]), uuid4())
    set_control(session, CONFIG, True)

    class Gate:
        protective_pause = 900
        def pause_remaining(self): return self.protective_pause
        def reserve_publish(self, key): return "reserved"
    class Broker:
        messages = []
        def ready_count(self, kind): return 0
        def publish(self, message): self.messages.append(message)
    factory = sessionmaker(bind=session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint")
    gate, broker = Gate(), Broker()
    result = Scheduler(CONFIG, factory, broker, gate, Metrics()).dispatch_once()
    assert result["collection_paused"] and result["plan_tasks_scheduled"] == 0
    assert [value.task_id for value in broker.messages] == [local.task_id]
    set_control(session, CONFIG, False)
    assert gate.protective_pause == 900
    broker.messages.clear()
    result = Scheduler(CONFIG, factory, broker, gate, Metrics()).dispatch_once()
    assert not result["collection_paused"]
    assert all(value.task_type == TaskType.CHANGE_DETECTION for value in broker.messages)
    assert source.status == TaskStatus.PENDING


def test_cancel_refuses_running_and_retry_creates_one_child_without_rewriting_history(session):
    pending = task(session)
    cancelled, changed = cancel_task(session, CONFIG, pending.task_id)
    assert changed and cancelled.status == TaskStatus.CANCELLED
    assert not cancel_task(session, CONFIG, pending.task_id)[1]
    running = task(session)
    lease = claim_task(session, running.task_id, running.task_type, "management-worker", CONFIG)
    with pytest.raises(ManagementError, match="invalid_task_transition"):
        cancel_task(session, CONFIG, running.task_id)
    original = task(session, kind=TaskType.PRODUCT_DISCOVERY, status=TaskStatus.DEAD)
    original.resource_type = ResourceType.SHOP
    original.resource_id = str(shop(session).id)
    original.attempt_count = original.max_attempts = 3
    original.last_error = "kept private legacy text"
    old_finish = database_now(session)
    original.finished_at = old_finish
    session.add(CrawlAttempt(task_id=original.id, attempt_no=3, succeeded=False, finished_at=old_finish, error_type="network_error"))
    session.flush()
    child, created = retry_task(session, CONFIG, original.task_id)
    replay, repeated = retry_task(session, CONFIG, original.task_id)
    assert created and not repeated and child.id == replay.id
    assert child.id != original.id and child.attempt_count == 0 and child.max_attempts == 3
    assert child.scheduled_for == original.scheduled_for and child.checkpoint == original.checkpoint
    assert child.payload["root_task_id"] == original.task_id and child.payload["retry_of_task_id"] == original.task_id
    assert (original.status, original.attempt_count, original.finished_at, original.last_error) == (
        TaskStatus.DEAD, 3, old_finish, "kept private legacy text")
    assert session.scalar(select(func.count()).select_from(CrawlAttempt).where(CrawlAttempt.task_id == original.id)) == 1
    assert lease is not None


def test_plan_edits_preserve_fixed_existing_run_scope_and_enforce_interval(session):
    first, second = shop(session), shop(session)
    plan, _ = create_plan(session, CONFIG, definition([first.id]), uuid4())
    run, _ = run_plan(session, CONFIG, plan.id, uuid4())
    update_plan(session, CONFIG, plan.id, {"resource_ids": [str(second.id)], "enabled": False})
    assert not plan.enabled and plan.next_run_at is None
    report = run_summary(session, run, include_tasks=True)
    assert report["tasks"][0]["resource_id"] == str(first.id)
    with pytest.raises(ManagementError, match="invalid_plan_interval"):
        update_plan(session, CONFIG, plan.id, {"interval_seconds": 60})


def test_retry_lineage_prevents_plan_overlap_without_rewriting_root_outcome(session):
    plan, _ = create_plan(session, CONFIG, definition([shop(session).id]), uuid4())
    run, _ = run_plan(session, CONFIG, plan.id, uuid4())
    root = session.scalar(select(CrawlTask).join(CollectionRunTask, CollectionRunTask.task_id == CrawlTask.id)
                          .where(CollectionRunTask.run_id == run.id))
    root.status = TaskStatus.DEAD
    session.flush()
    retry, _ = retry_task(session, CONFIG, root.task_id)
    session.flush()
    report = run_summary(session, run, include_tasks=True)
    assert report["status"] == "failed" and report["task_count"] == 1
    assert report["active_retry_count"] == 1 and report["retry_tasks"][0]["task_id"] == retry.task_id
    with pytest.raises(ManagementError, match="collection_plan_busy"):
        run_plan(session, CONFIG, plan.id, uuid4())
    plan.next_run_at = database_now(session) - timedelta(seconds=1)
    session.flush()
    assert enqueue_due_collection_plans(session, CONFIG) == 0
    retry.status = TaskStatus.DEAD
    session.flush()
    retry_again, _ = retry_task(session, CONFIG, retry.task_id)
    session.flush()
    assert run_summary(session, run)["active_retry_count"] == 1
    assert run_summary(session, run)["retry_task_count"] == 2
    assert retry_again.task_id != retry.task_id


def test_disabled_source_type_and_plan_budget_do_not_generate_work(session):
    store = shop(session)
    with pytest.raises(ManagementError, match="collector_not_enabled"):
        create_plan(session, replace(CONFIG, enabled_task_types=()), definition([store.id]), uuid4())
    plan, _ = create_plan(session, CONFIG, definition([store.id]), uuid4())
    assert enqueue_due_collection_plans(session, replace(CONFIG, enabled_task_types=())) == 0
    assert plan.next_run_at is not None
    assert enqueue_due_collection_plans(session, CONFIG, limit=1) == 1
    plan.next_run_at = database_now(session) - timedelta(seconds=1)
    session.flush()
    assert enqueue_due_collection_plans(session, CONFIG, limit=1) == 0
    assert session.scalar(select(func.count()).select_from(CollectionRun)) == 1
    assert plan.next_run_at > database_now(session)


def test_reduced_scheduler_budget_reports_oversized_plan_instead_of_starving(session):
    stores = [shop(session), shop(session)]
    plan, _ = create_plan(session, CONFIG, definition([row.id for row in stores]), uuid4())
    reduced = replace(CONFIG, dispatch_batch_size=1)
    assert enqueue_due_collection_plans(session, reduced, limit=1) == 0
    report = run_summary(session, session.scalar(select(CollectionRun)))
    assert report["error_code"] == "invalid_plan_resources" and report["status"] == "failed"
    assert plan.next_run_at > database_now(session)
    assert session.scalar(select(func.count()).select_from(CrawlTask)) == 0


def test_retry_racing_manual_plan_run_never_starts_overlapping_work(mysql_engine):
    factory = make_session_factory(mysql_engine)
    barrier = Barrier(2)
    with session_scope(factory) as db:
        store = shop(db)
        store_id = store.id
        plan, _ = create_plan(db, CONFIG, definition([store.id]), uuid4())
        plan_id = plan.id
        run, _ = run_plan(db, CONFIG, plan.id, uuid4())
        original = db.scalar(select(CrawlTask).join(CollectionRunTask, CollectionRunTask.task_id == CrawlTask.id)
                             .where(CollectionRunTask.run_id == run.id))
        original.status = TaskStatus.DEAD
        original_uuid = original.task_id
    try:
        def submit(action):
            with session_scope(factory) as db:
                barrier.wait(timeout=10)
                try:
                    if action == "run":
                        row, created = run_plan(db, CONFIG, plan_id, uuid4())
                    else:
                        row, created = retry_task(db, CONFIG, original_uuid)
                    return "created", created
                except ManagementError as error:
                    return error.code, False
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit, action) for action in ("run", "retry")]
            results = [future.result(timeout=30) for future in futures]
        assert sorted(value[0] for value in results) == ["collection_plan_busy", "created"]
        with factory() as db:
            active = db.scalar(select(func.count()).select_from(CrawlTask)
                .join(CollectionRunTask, CollectionRunTask.task_id == CrawlTask.id)
                .join(CollectionRun, CollectionRun.id == CollectionRunTask.run_id)
                .where(CollectionRun.plan_id == plan_id, CrawlTask.status == TaskStatus.PENDING))
            assert active == 1
    finally:
        with session_scope(factory) as db:
            run_ids = list(db.scalars(select(CollectionRun.id).where(CollectionRun.plan_id == plan_id)))
            task_ids = list(db.scalars(select(CollectionRunTask.task_id).where(CollectionRunTask.run_id.in_(run_ids))))
            task_uuids = list(db.scalars(select(CrawlTask.task_id).where(CrawlTask.id.in_(task_ids))))
            db.execute(delete(CollectionOperation).where(CollectionOperation.target_id.in_([str(plan_id), *task_uuids])))
            db.execute(delete(CollectionRunTask).where(CollectionRunTask.run_id.in_(run_ids)))
            db.execute(delete(CollectionRun).where(CollectionRun.id.in_(run_ids)))
            db.execute(delete(CollectionPlan).where(CollectionPlan.id == plan_id))
            db.execute(delete(CrawlTask).where(CrawlTask.id.in_(task_ids)))
            db.execute(delete(Shop).where(Shop.id == store_id))


@pytest.mark.parametrize("native_async", [False, True])
def test_scheduler_skips_plan_locked_by_manual_run_without_resource_lock_deadlock(mysql_engine, monkeypatch, native_async):
    """Force the tick to hold a shop while a manual run owns its plan lock.

    The plan selector must SKIP LOCKED, allowing the tick to commit and the
    manual run to acquire its resources. This is real MySQL row locking for
    both the synchronous and the production aiomysql scheduler.
    """
    from app.services import async_scheduler, collection_management, scheduler
    from app.storage.async_database import AsyncDatabase, make_async_engine

    factory = make_session_factory(mysql_engine)
    resource_locked, plan_locked = Event(), Event()
    with session_scope(factory) as db:
        store = shop(db)
        store_id = store.id
        config = replace(CONFIG, shop_profiles_enabled=True, shop_profile_shop_ids=(store.platform_shop_id,))
        plan, _ = create_plan(db, config, definition([store.id]), uuid4())
        plan_id = plan.id
    scheduler_module = async_scheduler if native_async else scheduler
    original_profiles = scheduler_module.enqueue_due_shop_profiles
    original_get_plan = collection_management.get_plan

    def periodic_profiles(db, settings, *, limit):
        result = original_profiles(db, settings, limit=limit)
        assert result == 1
        resource_locked.set()
        assert plan_locked.wait(timeout=10), "manual run did not acquire its plan lock"
        return result

    def manual_plan(db, settings, identifier, *, lock=False):
        result = original_get_plan(db, settings, identifier, lock=lock)
        if lock:
            assert resource_locked.is_set()
            # Signal the actual lock boundary. The later busy query may itself
            # wait on the scheduler's uncommitted task, depending on MySQL's
            # join order; waiting until resource validation invents a deadlock.
            plan_locked.set()
        return result

    monkeypatch.setattr(scheduler_module, "enqueue_due_shop_profiles", periodic_profiles)
    monkeypatch.setattr(collection_management, "get_plan", manual_plan)

    class Broker:
        def ready_count(self, kind): return 1000
        def publish(self, message): raise AssertionError("backpressure should prevent publishing")
    class Gate:
        def pause_remaining(self): return 0
    class AsyncBroker:
        async def ready_count(self, kind): return 1000
        async def publish(self, message): raise AssertionError("backpressure should prevent publishing")
    class AsyncGate:
        async def pause_remaining(self): return 0

    def tick():
        if not native_async:
            return Scheduler(config, factory, Broker(), Gate(), Metrics()).dispatch_once()
        async def run():
            database = AsyncDatabase(make_async_engine(mysql_engine.url))
            try:
                return await async_scheduler.AsyncScheduler(config, database, AsyncBroker(), AsyncGate(), Metrics()).dispatch_once()
            finally:
                await database.close()
        return asyncio.run(run())

    def manual():
        assert resource_locked.wait(timeout=10)
        with session_scope(factory) as db:
            run, created = run_plan(db, config, plan_id, uuid4())
            return run.id, created

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            ticking, requesting = pool.submit(tick), pool.submit(manual)
            result = ticking.result(timeout=20)
            run_id, created = requesting.result(timeout=20)
        assert result["shop_profiles_scheduled"] == 1 and result["plan_tasks_scheduled"] == 0
        assert created
        with factory() as db:
            assert run_summary(db, db.get(CollectionRun, run_id))["task_count"] == 1
            assert db.scalar(select(func.count()).select_from(CollectionRun).where(CollectionRun.plan_id == plan_id)) == 1
    finally:
        with session_scope(factory) as db:
            run_ids = list(db.scalars(select(CollectionRun.id).where(CollectionRun.plan_id == plan_id)))
            db.execute(delete(CollectionOperation).where(CollectionOperation.target_type == "plan", CollectionOperation.target_id == str(plan_id)))
            db.execute(delete(CollectionRunTask).where(CollectionRunTask.run_id.in_(run_ids)))
            db.execute(delete(CollectionRun).where(CollectionRun.id.in_(run_ids)))
            db.execute(delete(CollectionPlan).where(CollectionPlan.id == plan_id))
            db.execute(delete(CrawlTask).where(CrawlTask.task_type == TaskType.SHOP_PROFILE,
                CrawlTask.resource_type == ResourceType.SHOP, CrawlTask.resource_id == str(store_id)))
            db.execute(delete(Shop).where(Shop.id == store_id))


def test_concurrent_create_request_creates_one_plan_and_one_audit(mysql_engine):
    factory = make_session_factory(mysql_engine)
    request_id, barrier = uuid4(), Barrier(2)
    with session_scope(factory) as db:
        store = shop(db)
        store_id = store.id
    plan_id = None
    try:
        def submit():
            with session_scope(factory) as db:
                barrier.wait(timeout=10)
                row, created = create_plan(db, CONFIG, definition([store_id]), request_id)
                return row.id, created
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [future.result(timeout=30) for future in (pool.submit(submit), pool.submit(submit))]
        assert results[0][0] == results[1][0]
        plan_id = results[0][0]
        assert sorted(value[1] for value in results) == [False, True]
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(CollectionOperation).where(
                CollectionOperation.action == "plan_created", CollectionOperation.target_id == str(plan_id))) == 1
            with pytest.raises(ManagementError, match="idempotency_conflict"):
                create_plan(db, CONFIG, {**definition([store_id]), "name": "different"}, request_id)
    finally:
        with session_scope(factory) as db:
            if plan_id is not None:
                db.execute(delete(CollectionOperation).where(CollectionOperation.target_type == "plan", CollectionOperation.target_id == str(plan_id)))
                db.execute(delete(CollectionPlan).where(CollectionPlan.id == plan_id))
            db.execute(delete(Shop).where(Shop.id == store_id))


def test_api_authorization_payload_bounds_market_isolation_and_safe_audit(session, mysql_engine):
    store = shop(session)
    foreign = shop(session, region="GB")
    factory = sessionmaker(bind=session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint")
    with TestClient(create_app(CONFIG, mysql_engine)) as client:
        client.app.state.factory = factory
        payload = {**definition([store.id]), "request_id": str(uuid4())}
        assert client.get(PREFIX + "/control").status_code == 200
        assert client.post(PREFIX + "/plans", json=payload).status_code == 401
        assert client.put(PREFIX + "/control", json={"paused": True}).status_code == 401
        assert client.post(PREFIX + "/plans", json={**payload, "resource_ids": [str(foreign.id)]}, headers=AUTH).status_code == 422
        assert client.post(PREFIX + "/plans", json={**payload, "resource_ids": ["18446744073709551616"]}, headers=AUTH).status_code == 422
        assert client.post(PREFIX + "/plans", json={**payload, "resource_ids": [str(store.id)] * 101}, headers=AUTH).status_code == 422
        response = client.post(PREFIX + "/plans", json=payload, headers=AUTH)
        assert response.status_code == 200, response.text
        plan_id = response.json()["plan"]["id"]
        assert response.json()["created"]
        assert not client.post(PREFIX + "/plans", json=payload, headers=AUTH).json()["created"]
        assert client.get(PREFIX + f"/plans/{plan_id}").json()["resource_ids"] == [str(store.id)]
        run = client.post(PREFIX + f"/plans/{plan_id}/run", json={"request_id": str(uuid4())}, headers=AUTH)
        assert run.status_code == 200 and run.json()["run"]["task_count"] == 1
        history = client.get(PREFIX + f"/plans/{plan_id}/runs").json()["items"]
        assert len(history) == 1 and history[0]["coverage_scope"] == "root_tasks"
        assert client.get(PREFIX + "/plans/9999999999999").status_code == 404
        assert client.get(PREFIX + "/plans/18446744073709551616").status_code == 422
        assert client.patch(PREFIX + f"/plans/{plan_id}", json={"enabled": None}, headers=AUTH).status_code == 422
        assert client.put(PREFIX + "/control", json={"paused": True}, headers=AUTH).json()["paused"]
        audit_response = client.get(PREFIX + "/audit").text
        assert CONFIG.api_token not in audit_response and "Authorization" not in audit_response
        assert client.put(PREFIX + "/control", json={"paused": False}, headers=AUTH).json()["paused"] is False


def test_additive_management_migration_keeps_its_historical_definition():
    saved = Path(__file__).resolve().parents[1] / "migrations/012_collection_management.sql"
    assert saved.read_text(encoding="utf-8") == additive_sql()
    assert "ALTER TABLE" not in additive_sql() and "DROP TABLE" not in additive_sql()
    assert "archived_at" not in additive_sql()


@pytest.mark.parametrize("mode", ["once", "interval"])
def test_plan_edit_only_reschedules_actual_schedule_changes(session, monkeypatch, mode):
    from app.services import collection_management

    plan, _ = create_plan(session, CONFIG, definition([shop(session).id], mode=mode), uuid4())
    fixed_now = database_now(session)
    scheduled = fixed_now + timedelta(hours=4)
    plan.next_run_at = scheduled
    session.flush()
    monkeypatch.setattr(collection_management, "database_now", lambda db: fixed_now)
    # Existing clients submit complete form values, including null for once.
    update_plan(session, CONFIG, plan.id, {"name": "Renamed", "interval_seconds": plan.interval_seconds,
                                         "resource_ids": plan.resource_ids, "enabled": True})
    assert plan.next_run_at == scheduled
    audit_row = session.scalar(select(CollectionOperation).where(CollectionOperation.action == "plan_updated"))
    assert audit_row.details["fields"] == ["name"]
    update_plan(session, CONFIG, plan.id, {"name": "Renamed", "interval_seconds": plan.interval_seconds})
    assert session.scalar(select(func.count()).select_from(CollectionOperation).where(CollectionOperation.action == "plan_updated")) == 1
    update_plan(session, CONFIG, plan.id, {"enabled": False})
    assert not plan.enabled and plan.next_run_at is None
    update_plan(session, CONFIG, plan.id, {"name": "Still disabled"})
    assert plan.next_run_at is None
    update_plan(session, CONFIG, plan.id, {"enabled": True})
    assert plan.enabled and plan.next_run_at == fixed_now
    if mode == "once":
        update_plan(session, CONFIG, plan.id, {"interval_seconds": None})
        assert plan.next_run_at == fixed_now
        with pytest.raises(ManagementError, match="invalid_plan_interval"):
            update_plan(session, CONFIG, plan.id, {"interval_seconds": 3600})
    else:
        with pytest.raises(ManagementError, match="invalid_plan_interval"):
            update_plan(session, CONFIG, plan.id, {"interval_seconds": None})
        update_plan(session, CONFIG, plan.id, {"interval_seconds": 7200})
        assert plan.next_run_at == fixed_now + timedelta(hours=2)
        update_plan(session, CONFIG, plan.id, {"enabled": False, "interval_seconds": 10800})
        assert plan.next_run_at is None and plan.interval_seconds == 10800
        update_plan(session, CONFIG, plan.id, {"enabled": True})
        assert plan.next_run_at == fixed_now


@pytest.mark.parametrize("mode,enabled", [("once", True), ("once", False), ("interval", True), ("interval", False)])
def test_manual_run_schedule_semantics_preserve_explicit_disabled_state(session, monkeypatch, mode, enabled):
    from app.services import collection_management

    plan, _ = create_plan(session, CONFIG, definition([shop(session).id], mode=mode, enabled=enabled), uuid4())
    fixed_now = database_now(session)
    monkeypatch.setattr(collection_management, "database_now", lambda db: fixed_now)
    request_id = uuid4()
    run, created = run_plan(session, CONFIG, plan.id, request_id)
    assert created and run.scheduled_for == fixed_now
    if mode == "interval" and enabled:
        assert plan.enabled and plan.next_run_at == fixed_now + timedelta(hours=1)
    else:
        assert not plan.enabled and plan.next_run_at is None
    expected_due = plan.next_run_at
    monkeypatch.setattr(collection_management, "database_now", lambda db: fixed_now + timedelta(minutes=1))
    replay, created = run_plan(session, CONFIG, plan.id, request_id)
    assert not created and replay.id == run.id and plan.next_run_at == expected_due


def test_archive_restoration_preserves_history_and_fences_new_work(session):
    plan, _ = create_plan(session, CONFIG, definition([shop(session).id]), uuid4())
    request_id = uuid4()
    run, _ = run_plan(session, CONFIG, plan.id, request_id)
    original = session.scalar(select(CrawlTask).join(CollectionRunTask, CollectionRunTask.task_id == CrawlTask.id)
                              .where(CollectionRunTask.run_id == run.id))
    archived, changed = set_plan_archived(session, CONFIG, plan.id, True)
    archived_at = archived.archived_at
    assert changed and archived_at is not None and not archived.enabled and archived.next_run_at is None
    assert original.status == TaskStatus.PENDING
    assert not set_plan_archived(session, CONFIG, plan.id, True)[1]
    assert plan.archived_at == archived_at
    assert enqueue_due_collection_plans(session, CONFIG) == 0
    assert run_plan(session, CONFIG, plan.id, request_id)[0].id == run.id
    with pytest.raises(ManagementError, match="collection_plan_archived"):
        run_plan(session, CONFIG, plan.id, uuid4())
    with pytest.raises(ManagementError, match="collection_plan_archived"):
        update_plan(session, CONFIG, plan.id, {"enabled": True})
    assert cancel_task(session, CONFIG, original.task_id)[1]
    with pytest.raises(ManagementError, match="collection_plan_archived"):
        retry_task(session, CONFIG, original.task_id)
    assert run_summary(session, run)["status_counts"] == {TaskStatus.CANCELLED: 1}
    assert session.scalar(select(func.count()).select_from(CollectionRun)) == 1
    restored, changed = set_plan_archived(session, CONFIG, plan.id, False)
    assert changed and restored.archived_at is None and not restored.enabled and restored.next_run_at is None
    assert not set_plan_archived(session, CONFIG, plan.id, False)[1]
    assert enqueue_due_collection_plans(session, CONFIG) == 0
    retry, created = retry_task(session, CONFIG, original.task_id)
    assert created
    set_plan_archived(session, CONFIG, plan.id, True)
    # Replaying a previously accepted retry reads history; it creates no work.
    assert retry_task(session, CONFIG, original.task_id) == (retry, False)
    retry.status = TaskStatus.DEAD
    session.flush()
    with pytest.raises(ManagementError, match="collection_plan_archived"):
        retry_task(session, CONFIG, retry.task_id)
    actions = list(session.scalars(select(CollectionOperation.action).where(CollectionOperation.target_type == "plan")
                                   .order_by(CollectionOperation.id)))
    assert actions == ["plan_created", "plan_run", "plan_archived", "plan_restored", "plan_archived"]


def test_plan_filters_archive_routes_and_once_edit_are_market_scoped(session, mysql_engine):
    store, foreign = shop(session), shop(session, region="GB")
    once, _ = create_plan(session, CONFIG, {**definition([store.id], mode="once"), "name": "Retail_100%"}, uuid4())
    interval, _ = create_plan(session, CONFIG, {**definition([store.id]), "name": "RetailX100Y"}, uuid4())
    item = product(session, store)
    snapshot, _ = create_plan(session, CONFIG, definition([item.id], kind=TaskType.PRODUCT_SNAPSHOT, enabled=False), uuid4())
    foreign_plan = CollectionPlan(platform=CONFIG.platform, region="GB", request_key=uuid4().hex * 2,
        request_hash=uuid4().hex * 2, **definition([foreign.id]), next_run_at=database_now(session))
    session.add(foreign_plan)
    session.flush()
    factory = sessionmaker(bind=session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint")
    with TestClient(create_app(CONFIG, mysql_engine)) as client:
        client.app.state.factory = factory
        due = once.next_run_at.isoformat().replace("+00:00", "Z")
        response = client.patch(PREFIX + f"/plans/{once.id}", json={"name": "Retail_100%", "interval_seconds": None}, headers=AUTH)
        assert response.status_code == 200 and response.json()["next_run_at"].replace("+00:00", "Z") == due
        values = client.get(PREFIX + "/plans", params={"q": "_100%"}).json()["items"]
        assert [value["id"] for value in values] == [str(once.id)]
        values = client.get(PREFIX + "/plans", params={"task_type": "product_snapshot", "enabled": False}).json()["items"]
        assert [value["id"] for value in values] == [str(snapshot.id)]
        values = client.get(PREFIX + "/plans", params={"q": str(interval.id)}).json()["items"]
        assert [value["id"] for value in values] == [str(interval.id)]
        response = client.post(PREFIX + f"/plans/{once.id}/archive", json={})
        assert response.status_code == 401
        response = client.post(PREFIX + f"/plans/{once.id}/archive", json={}, headers=AUTH)
        assert response.status_code == 200 and response.json()["changed"] and response.json()["plan"]["archived_at"]
        assert not client.post(PREFIX + f"/plans/{once.id}/archive", json={}, headers=AUTH).json()["changed"]
        assert str(once.id) not in {value["id"] for value in client.get(PREFIX + "/plans").json()["items"]}
        assert [value["id"] for value in client.get(PREFIX + "/plans", params={"archived": True}).json()["items"]] == [str(once.id)]
        assert client.get(PREFIX + f"/plans/{once.id}").status_code == 200
        assert client.patch(PREFIX + f"/plans/{once.id}", json={"enabled": True}, headers=AUTH).status_code == 409
        assert client.post(PREFIX + f"/plans/{foreign_plan.id}/archive", json={}, headers=AUTH).status_code == 404
        assert client.get(PREFIX + f"/plans/{foreign_plan.id}").status_code == 404
        response = client.post(PREFIX + f"/plans/{once.id}/restore", json={}, headers=AUTH)
        assert response.json()["changed"] and not response.json()["plan"]["enabled"]
        assert response.json()["plan"]["archived_at"] is None and response.json()["plan"]["next_run_at"] is None
        first = client.get(PREFIX + "/plans", params={"limit": 1}).json()
        second = client.get(PREFIX + "/plans", params={"limit": 1, "before_id": first["next_before_id"]}).json()
        assert first["items"][0]["id"] != second["items"][0]["id"]


def test_resource_picker_uses_bounded_scalar_projection_and_distinguishes_eligibility(session):
    store = shop(session)
    store.profile_field_state = {"private_fixture": "should not be loaded"}
    complete, incomplete = product(session, store), product(session, store)
    incomplete.last_success_at = None
    incomplete.description = "should not be loaded"
    incomplete.raw_attributes = {"should": "not be loaded"}
    incomplete.color_summary = ["not selected"]
    foreign = product(session, shop(session, region="GB"))
    session.flush()
    statements = []
    def capture(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)
    event.listen(session.bind, "before_cursor_execute", capture)
    try:
        first = list_resources(session, CONFIG, TaskType.PRODUCT_SNAPSHOT, limit=1)
    finally:
        event.remove(session.bind, "before_cursor_execute", capture)
    assert len(statements) == 1
    assert all(name not in statements[0] for name in ("raw_attributes", "description", "color_summary", "profile_field_state"))
    assert first["items"] == [{"id": str(incomplete.id), "name": incomplete.title, "platform_id": incomplete.platform_product_id,
        "shop_name": store.canonical_name, "eligible": False, "eligibility_reason": "detail_not_collected"}]
    assert first["next_before_id"] == str(incomplete.id)
    second = list_resources(session, CONFIG, TaskType.PRODUCT_SNAPSHOT, limit=1, before_id=int(first["next_before_id"]))
    assert second["items"][0]["id"] == str(complete.id) and second["items"][0]["eligible"]
    assert second["next_before_id"] is None
    echoed = list_resources(session, CONFIG, TaskType.PRODUCT_DETAIL, ids=[str(incomplete.id), str(foreign.id)], limit=1)
    assert len(echoed["items"]) == 1 and echoed["items"][0]["eligible"] and echoed["next_before_id"] is None
    settings = replace(CONFIG, enabled_task_types=(*CONFIG.enabled_task_types, TaskType.PRODUCT_REVIEWS))
    assert list_resources(session, settings, TaskType.PRODUCT_REVIEWS, ids=[str(incomplete.id)])["items"][0]["eligible"]
    incomplete.currency = "GBP"
    session.flush()
    assert list_resources(session, CONFIG, TaskType.PRODUCT_DETAIL, ids=[str(incomplete.id)])["items"][0]["eligibility_reason"] == "unsupported_currency"
    incomplete.currency, incomplete.pricing_context = "USD", "member_only"
    session.flush()
    assert list_resources(session, CONFIG, TaskType.PRODUCT_DETAIL, ids=[str(incomplete.id)])["items"][0]["eligibility_reason"] == "unsupported_pricing_context"
    assert list_resources(session, settings, TaskType.PRODUCT_REVIEWS, ids=[str(incomplete.id)])["items"][0]["eligible"]
    store.canonical_url = None
    session.flush()
    assert list_resources(session, CONFIG, TaskType.PRODUCT_DISCOVERY, ids=[str(store.id)])["items"][0]["eligibility_reason"] == "invalid_source_url"
    assert list_resources(session, replace(CONFIG, enabled_task_types=()), TaskType.SHOP_PROFILE,
                          ids=[str(store.id)])["items"][0]["eligibility_reason"] == "collector_not_enabled"


def test_resource_picker_api_search_echo_and_bounds(session, mysql_engine):
    stores = [shop(session), shop(session), shop(session)]
    foreign = shop(session, region="GB")
    stores[0].canonical_name, stores[1].canonical_name = "Store_100%", "StoreX100Y"
    session.flush()
    factory = sessionmaker(bind=session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint")
    with TestClient(create_app(CONFIG, mysql_engine)) as client:
        client.app.state.factory = factory
        response = client.get(PREFIX + "/resources", params={"task_type": "shop_profile", "q": "_100%"})
        assert response.status_code == 200
        assert [row["id"] for row in response.json()["items"]] == [str(stores[0].id)]
        response = client.get(PREFIX + "/resources", params={"task_type": "product_discovery", "q": stores[1].platform_shop_id})
        assert [row["id"] for row in response.json()["items"]] == [str(stores[1].id)]
        params = [("task_type", "shop_profile"), ("limit", "1"), ("ids", str(stores[0].id)),
                  ("ids", str(stores[2].id)), ("ids", str(foreign.id))]
        response = client.get(PREFIX + "/resources", params=params)
        assert response.status_code == 200 and response.json()["next_before_id"] is None
        assert {row["id"] for row in response.json()["items"]} == {str(stores[0].id), str(stores[2].id)}
        for extra in [("q", "Store"), ("before_id", "5"), ("ids", str(stores[0].id)), ("ids", "18446744073709551616")]:
            assert client.get(PREFIX + "/resources", params=[*params, extra]).status_code == 422
        assert client.get(PREFIX + "/resources", params={"task_type": "shop_profile", "ids": ["1"] * 101}).status_code == 422
        assert client.get(PREFIX + "/resources", params={"task_type": "shop_profile", "limit": 51}).status_code == 422
        assert client.get(PREFIX + "/resources", params={"task_type": "shop_discovery"}).status_code == 422
        assert client.get(PREFIX + "/resources", params={"task_type": "shop_profile", "q": "9" * 80}).status_code == 200


def test_archive_migration_upgrades_legacy_data_and_keeps_012_rerunnable(mysql_engine, monkeypatch, capsys):
    from scripts import migrate_collection_management as foundation, migrate_collection_plan_archive as archive

    saved = Path(__file__).resolve().parents[1] / "migrations/015_collection_plan_archive.sql"
    assert saved.read_text(encoding="utf-8") == archive.additive_sql()
    factory = make_session_factory(mysql_engine)
    with session_scope(factory) as db:
        store = shop(db)
        store_id = store.id
        plan, _ = create_plan(db, CONFIG, definition([store.id]), uuid4())
        plan_id = plan.id
        run, _ = run_plan(db, CONFIG, plan.id, uuid4())
        run_id, due, name, resource_ids = run.id, plan.next_run_at, plan.name, plan.resource_ids
        task_id = db.scalar(select(CollectionRunTask.task_id).where(CollectionRunTask.run_id == run.id))
    try:
        with mysql_engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE collection_plans DROP COLUMN archived_at")
        assert foundation.migrate(mysql_engine)["ready"]
        assert foundation.migrate(mysql_engine, execute=True)["executed_statements"] == 0
        assert archive.migrate(mysql_engine) == {"ready": False, "executed_statements": 0, "pending_statements": 1, "data_rows_modified": 0}
        monkeypatch.setattr(archive, "make_engine", lambda: mysql_engine)
        assert archive.main(["--check"]) == 1
        assert archive.main(["--execute"]) == 0
        assert archive.main(["--check"]) == 0
        assert archive.migrate(mysql_engine, execute=True)["executed_statements"] == 0
        assert foundation.migrate(mysql_engine)["ready"]
        assert foundation.migrate(mysql_engine, execute=True)["executed_statements"] == 0
        with factory() as db:
            persisted = db.get(CollectionPlan, plan_id)
            assert persisted.archived_at is None and persisted.enabled
            assert (persisted.name, persisted.resource_ids, persisted.next_run_at) == (name, resource_ids, due)
            assert run_summary(db, db.get(CollectionRun, run_id))["task_count"] == 1
        output = capsys.readouterr().out
        assert '"ready": false' in output and '"ready": true' in output
    finally:
        archive.migrate(mysql_engine, execute=True)
        with session_scope(factory) as db:
            db.execute(delete(CollectionOperation).where(CollectionOperation.target_type == "plan", CollectionOperation.target_id == str(plan_id)))
            db.execute(delete(CollectionRunTask).where(CollectionRunTask.run_id == run_id))
            db.execute(delete(CollectionRun).where(CollectionRun.id == run_id))
            db.execute(delete(CollectionPlan).where(CollectionPlan.id == plan_id))
            db.execute(delete(CrawlTask).where(CrawlTask.id == task_id))
            db.execute(delete(Shop).where(Shop.id == store_id))


def test_historical_management_migration_creates_missing_tables_before_archive(mysql_engine):
    from scripts import migrate_collection_management as foundation, migrate_collection_plan_archive as archive

    # This engine belongs to conftest's uniquely named disposable test database.
    # Only empty management tables are removed to exercise a fresh 012 install.
    names = ("collection_run_tasks", "collection_runs", "collection_plans", "collection_operations", "collection_controls")
    with mysql_engine.begin() as connection:
        assert all(connection.scalar(text(f"SELECT COUNT(*) FROM {name}")) == 0 for name in names)
        source_counts = {name: connection.scalar(text(f"SELECT COUNT(*) FROM {name}")) for name in ("shops", "products", "crawl_tasks")}
        for name in names:
            connection.exec_driver_sql(f"DROP TABLE {name}")
    try:
        assert not foundation.migrate(mysql_engine)["ready"]
        assert foundation.migrate(mysql_engine, execute=True)["ready"]
        assert foundation.migrate(mysql_engine, execute=True)["executed_statements"] == 0
        assert "archived_at" not in {column["name"] for column in inspect(mysql_engine).get_columns("collection_plans")}
        assert archive.migrate(mysql_engine, execute=True)["ready"]
        assert foundation.migrate(mysql_engine)["ready"]
        with mysql_engine.connect() as connection:
            assert all(connection.scalar(text(f"SELECT COUNT(*) FROM {name}")) == count for name, count in source_counts.items())
    finally:
        foundation.migrate(mysql_engine, execute=True)
        archive.migrate(mysql_engine, execute=True)
