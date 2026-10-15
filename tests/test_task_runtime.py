from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.main import create_app
from app.models import CrawlAttempt, CrawlTask, ResourceType, Shop, TaskStatus, TaskType
from app.observability.metrics import Metrics
from app.queue.contracts import TaskMessage
from app.queue.retry_policy import Failure, TaskFailure
from app.services.scheduler import Scheduler
from app.services.task_runtime import claim_task, database_now, ExecutionResult, finalize_task, LeaseLost, recover_expired_leases, renew_lease, save_checkpoint
from app.settings import Settings
from app.storage.database import make_session_factory, session_scope
from app.workers.runtime import Worker


CONFIG = Settings(enabled_task_types=(TaskType.SHOP_DISCOVERY,))


@pytest.fixture
def runtime_db(mysql_engine):
    factory = make_session_factory(mysql_engine)
    created = []
    def new_task(kind=TaskType.SHOP_DISCOVERY, region="US"):
        with session_scope(factory) as db:
            now = database_now(db)
            task = CrawlTask(region=region, task_type=kind, resource_type=ResourceType.SEED, resource_id="1",
                             payload={"seed_id": "1"}, scheduled_for=now, available_at=now - timedelta(seconds=1))
            db.add(task)
            db.flush()
            created.append(task.task_id)
            return task.task_id
    yield factory, new_task
    with session_scope(factory) as db:
        identifiers = list(db.scalars(select(CrawlTask.id).where(CrawlTask.task_id.in_(created))))
        if identifiers:
            db.execute(delete(CrawlAttempt).where(CrawlAttempt.task_id.in_(identifiers)))
            db.execute(delete(CrawlTask).where(CrawlTask.id.in_(identifiers)))


def claim(factory, task_id):
    with session_scope(factory) as db:
        return claim_task(db, task_id, TaskType.SHOP_DISCOVERY, "test-worker", CONFIG)


def test_two_workers_only_one_claims_and_one_attempt_is_created(runtime_db):
    factory, new_task = runtime_db
    task_id, barrier = new_task(), Barrier(2)
    def run():
        barrier.wait(timeout=5)
        return claim(factory, task_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = [future.result() for future in [pool.submit(run), pool.submit(run)]]
    assert sum(item is not None for item in (first, second)) == 1
    with factory() as db:
        assert len(db.scalars(select(CrawlAttempt)).all()) == 1
        task = db.scalar(select(CrawlTask).where(CrawlTask.task_id == task_id))
        assert task.status == TaskStatus.RUNNING and task.attempt_count == 1


def test_success_and_domain_writes_are_atomic(runtime_db):
    factory, new_task = runtime_db
    lease = claim(factory, new_task())
    shop_external_id = uuid4().hex
    def bad_write(db, task):
        db.add(Shop(region="US", platform_shop_id=shop_external_id, canonical_name="rollback"))
        db.flush()
        raise TaskFailure(Failure("field_invalid", "字段校验失败。"))
    with pytest.raises(TaskFailure), session_scope(factory) as db:
        finalize_task(db, lease, CONFIG, result=ExecutionResult(apply=bad_write))
    with factory() as db:
        assert db.scalar(select(Shop.id).where(Shop.platform_shop_id == shop_external_id)) is None
        assert db.get(CrawlTask, lease.id).status == TaskStatus.RUNNING
    with session_scope(factory) as db:
        assert finalize_task(db, lease, CONFIG, result=ExecutionResult(checkpoint={"complete": True})) == TaskStatus.SUCCEEDED
    with pytest.raises(LeaseLost), session_scope(factory) as db:
        finalize_task(db, lease, CONFIG, result=ExecutionResult())


def test_retry_preserves_checkpoint_and_exhaustion_is_dead(runtime_db):
    factory, new_task = runtime_db
    task_id = new_task()
    for attempt in range(1, 4):
        lease = claim(factory, task_id)
        assert lease.attempt_no == attempt
        with session_scope(factory) as db:
            save_checkpoint(db, lease, {"cursor": "page-2"})
            status = finalize_task(db, lease, CONFIG, failure=Failure("network_error", "网络失败。", True))
            assert status == (TaskStatus.RETRY_WAITING if attempt < 3 else TaskStatus.DEAD)
        with session_scope(factory) as db:
            task = db.get(CrawlTask, lease.id)
            assert task.checkpoint == {"cursor": "page-2"} and task.lease_token is None
            task.available_at = database_now(db) - timedelta(seconds=1)
    assert claim(factory, task_id) is None
    with factory() as db:
        attempts = db.scalars(select(CrawlAttempt).order_by(CrawlAttempt.attempt_no)).all()
        assert [row.attempt_no for row in attempts] == [1, 2, 3]
        assert all(row.succeeded is False and row.finished_at is not None for row in attempts)


def test_expired_lease_recovers_and_old_worker_cannot_overwrite_new_owner(runtime_db):
    factory, new_task = runtime_db
    task_id = new_task()
    old = claim(factory, task_id)
    with session_scope(factory) as db:
        save_checkpoint(db, old, {"cursor": "resume-me"})
        renew_lease(db, old, CONFIG)
        task = db.get(CrawlTask, old.id)
        task.lease_expires_at = database_now(db) - timedelta(seconds=1)
    with session_scope(factory) as db:
        assert recover_expired_leases(db, CONFIG) == 1
        assert db.get(CrawlTask, old.id).status == TaskStatus.ABANDONED
        db.get(CrawlTask, old.id).available_at = database_now(db) - timedelta(seconds=1)
    new = claim(factory, task_id)
    assert new.lease_token != old.lease_token
    assert new.checkpoint == {"cursor": "resume-me"}
    with pytest.raises(LeaseLost), session_scope(factory) as db:
        finalize_task(db, old, CONFIG, result=ExecutionResult())
    with session_scope(factory) as db:
        assert finalize_task(db, new, CONFIG, result=ExecutionResult()) == TaskStatus.SUCCEEDED


def test_unknown_market_and_disabled_types_are_not_dispatched(runtime_db):
    factory, new_task = runtime_db
    product_id, other_market = new_task(TaskType.PRODUCT_DISCOVERY), new_task(region="GB")
    class Gate:
        def pause_remaining(self): return 0
        def reserve_publish(self, key): return "token"
    class Broker:
        def ready_count(self, kind): return 0
        def publish(self, message): raise AssertionError("unsupported task was published")
    assert Scheduler(CONFIG, factory, Broker(), Gate(), Metrics()).dispatch_once()["published"] == 0
    assert claim(factory, other_market) is None
    with factory() as db:
        assert db.scalar(select(CrawlTask).where(CrawlTask.task_id == product_id)).status == TaskStatus.PENDING


def test_scheduler_deduplicates_notifications_and_does_not_claim(runtime_db):
    factory, new_task = runtime_db
    task_id = new_task()
    class Gate:
        keys = set()
        def pause_remaining(self): return 0
        def reserve_publish(self, key):
            if key in self.keys: return None
            self.keys.add(key)
            return "token"
        def release_publish(self, key, token): self.keys.discard(key)
    class Broker:
        messages = []
        def ready_count(self, kind): return 0
        def publish(self, message): self.messages.append(message)
    broker, gate = Broker(), Gate()
    scheduler = Scheduler(CONFIG, factory, broker, gate, Metrics())
    assert scheduler.dispatch_once()["published"] == 1
    assert scheduler.dispatch_once()["published"] == 0
    assert broker.messages[0].task_id == task_id
    with factory() as db:
        assert db.scalar(select(CrawlTask).where(CrawlTask.task_id == task_id)).attempt_count == 0


class PublishGate:
    def __init__(self): self.keys = set()
    def pause_remaining(self): return 0
    def reserve_publish(self, key):
        if key in self.keys: return None
        self.keys.add(key)
        return "token"
    def release_publish(self, key, token): self.keys.discard(key)


class WatermarkBroker:
    def __init__(self, depths=None):
        self.depths, self.messages = dict(depths or {}), []
    def ready_count(self, kind): return self.depths.get(kind, 0)
    def publish(self, message):
        self.messages.append(message)
        self.depths[message.task_type] = self.ready_count(message.task_type) + 1


def test_scheduler_backpressure_does_not_starve_reports(runtime_db):
    factory, new_task = runtime_db
    new_task(TaskType.PRODUCT_DETAIL)
    report_id = new_task(TaskType.REPORT_GENERATION)
    config = replace(CONFIG, enabled_task_types=(TaskType.PRODUCT_DETAIL, TaskType.REPORT_GENERATION))
    broker = WatermarkBroker({TaskType.PRODUCT_DETAIL: 545241})
    result = Scheduler(config, factory, broker, PublishGate(), Metrics()).dispatch_once()
    assert result["backpressured_types"] == ["product_detail"]
    assert [message.task_id for message in broker.messages] == [report_id]


def test_scheduler_tiny_global_budget_rotates_independent_type_cursors(runtime_db):
    factory, new_task = runtime_db
    for _ in range(3): new_task(TaskType.PRODUCT_DETAIL)
    report_id = new_task(TaskType.REPORT_GENERATION)
    config = replace(CONFIG, enabled_task_types=(TaskType.PRODUCT_DETAIL, TaskType.REPORT_GENERATION),
                     dispatch_batch_size=1)
    broker = WatermarkBroker()
    scheduler = Scheduler(config, factory, broker, PublishGate(), Metrics())
    scheduler.dispatch_once()
    scheduler.dispatch_once()
    assert report_id in [message.task_id for message in broker.messages]


def test_scheduler_publishes_only_remaining_queue_capacity(runtime_db):
    factory, new_task = runtime_db
    for _ in range(5): new_task()
    broker = WatermarkBroker({TaskType.SHOP_DISCOVERY: 61})
    result = Scheduler(CONFIG, factory, broker, PublishGate(), Metrics()).dispatch_once()
    assert result["published"] == len(broker.messages) == 3


def test_images_and_full_details_share_watermark_and_rotate_tiny_budgets(runtime_db):
    factory, new_task = runtime_db
    full_id = new_task(TaskType.PRODUCT_DETAIL)
    image_id = new_task(TaskType.PRODUCT_DETAIL)
    with session_scope(factory) as db:
        db.scalar(select(CrawlTask).where(CrawlTask.task_id == image_id)).payload = {"capture_mode": "images_only"}
    config = replace(CONFIG, enabled_task_types=(TaskType.PRODUCT_DETAIL,), image_backfill_enabled=True,
                     dispatch_batch_size=1)
    broker = WatermarkBroker({TaskType.PRODUCT_DETAIL: 62})
    scheduler = Scheduler(config, factory, broker, PublishGate(), Metrics())
    assert scheduler.dispatch_once()["published"] == 1
    assert scheduler.dispatch_once()["published"] == 1
    assert [message.task_id for message in broker.messages] == [full_id, image_id]
    assert broker.ready_count(TaskType.PRODUCT_DETAIL) == 64
    assert scheduler.dispatch_once()["published"] == 0


def test_image_dispatch_is_opt_in_and_empty_full_lane_yields_its_budget(runtime_db):
    factory, new_task = runtime_db
    for _ in range(5):
        identifier = new_task(TaskType.PRODUCT_DETAIL)
        with session_scope(factory) as db:
            db.scalar(select(CrawlTask).where(CrawlTask.task_id == identifier)).payload = {"capture_mode": "images_only"}
    config = replace(CONFIG, enabled_task_types=(TaskType.PRODUCT_DETAIL,), dispatch_batch_size=4)
    broker = WatermarkBroker()
    assert Scheduler(config, factory, broker, PublishGate(), Metrics()).dispatch_once()["published"] == 0
    enabled = replace(config, image_backfill_enabled=True)
    assert Scheduler(enabled, factory, broker, PublishGate(), Metrics()).dispatch_once()["published"] == 4


def test_global_scan_budget_includes_dedup_skips_but_next_type_gets_a_turn(runtime_db):
    factory, new_task = runtime_db
    first_id = new_task(TaskType.PRODUCT_DETAIL)
    report_id = new_task(TaskType.REPORT_GENERATION)
    config = replace(CONFIG, enabled_task_types=(TaskType.PRODUCT_DETAIL, TaskType.REPORT_GENERATION),
                     dispatch_batch_size=1, dispatch_scan_limit=1)
    reserved = []
    class Gate(PublishGate):
        def reserve_publish(self, key):
            reserved.append(key)
            return None if key.startswith(first_id) else super().reserve_publish(key)
    broker = WatermarkBroker()
    scheduler = Scheduler(config, factory, broker, Gate(), Metrics())
    assert scheduler.dispatch_once()["published"] == 0 and len(reserved) == 1
    assert scheduler.dispatch_once()["published"] == 1
    assert broker.messages[0].task_id == report_id


def test_backpressure_still_recovers_leases_without_resetting_history(runtime_db):
    factory, new_task = runtime_db
    lease = claim(factory, new_task())
    with session_scope(factory) as db:
        task = db.get(CrawlTask, lease.id)
        task.lease_expires_at = database_now(db) - timedelta(seconds=1)
        task.checkpoint = {"cursor": "keep"}
    broker = WatermarkBroker({TaskType.SHOP_DISCOVERY: 1000})
    result = Scheduler(CONFIG, factory, broker, PublishGate(), Metrics()).dispatch_once()
    assert result["recovered"] == 1 and result["published"] == 0
    with factory() as db:
        task = db.get(CrawlTask, lease.id)
        assert task.checkpoint == {"cursor": "keep"} and task.attempt_count == 1
        assert db.scalar(select(CrawlAttempt)).error_type == "lease_expired"


def test_failed_publish_releases_reservation_and_does_not_skip_the_task(runtime_db):
    factory, new_task = runtime_db
    task_id = new_task()
    class FailsOnce(WatermarkBroker):
        failed = False
        def publish(self, message):
            if not self.failed:
                self.failed = True
                raise OSError("fixture publisher unavailable")
            super().publish(message)
    broker, gate = FailsOnce(), PublishGate()
    scheduler = Scheduler(CONFIG, factory, broker, gate, Metrics())
    with pytest.raises(OSError): scheduler.dispatch_once()
    assert not gate.keys
    assert scheduler.dispatch_once()["published"] == 1
    with factory() as db:
        assert db.scalar(select(CrawlTask).where(CrawlTask.task_id == task_id)).attempt_count == 0


def test_slow_report_preparation_keeps_sql_lease_and_mq_heartbeat_alive(runtime_db, monkeypatch):
    import time
    from app.workers.handlers import ReportGenerationHandler
    from app.workers import runtime as worker_runtime
    factory, new_task = runtime_db
    task_id = new_task(TaskType.REPORT_GENERATION)
    config = replace(CONFIG, enabled_task_types=(TaskType.REPORT_GENERATION,), heartbeat_seconds=1, lease_seconds=3)
    with session_scope(factory) as db:
        task = db.scalar(select(CrawlTask).where(CrawlTask.task_id == task_id))
        task.resource_type, task.resource_id = ResourceType.REPORT, "clothing_monitor"
        task.payload = {"report_type": "clothing_monitor", "lookback_hours": 24,
                        "pricing_context": config.pricing_context, "currency": config.currency}
    def slow_report(db, settings, **kwargs):
        time.sleep(3.4)
        return {"as_of": "2026-10-15T00:00:00+00:00", "summary": {"products": 1, "events_in_window": 0}}
    monkeypatch.setattr("app.workers.handlers.build_monitoring_report", slow_report)
    monkeypatch.setattr("app.workers.handlers.export_report", lambda *args: {"report_hash": "a" * 64, "files": {}})
    renewed, polls = [], []
    original = worker_runtime.renew_lease
    def counted(db, lease, settings):
        renewed.append(lease.task_id)
        return original(db, lease, settings)
    monkeypatch.setattr(worker_runtime, "renew_lease", counted)
    class NoWebsite:
        def pause_remaining(self): raise AssertionError("A report must not contact the site")
    worker = Worker(config, factory, {TaskType.REPORT_GENERATION: ReportGenerationHandler()},
                    NoWebsite(), Metrics(), "report-heartbeat-worker")
    assert worker.process(TaskMessage(task_id, TaskType.REPORT_GENERATION), lambda: polls.append(1)) == "ack"
    assert len(renewed) >= 2 and len(polls) > 5
    with factory() as db:
        assert db.scalar(select(CrawlTask).where(CrawlTask.task_id == task_id)).status == TaskStatus.SUCCEEDED


def test_worker_completes_before_ack_and_duplicate_delivery_has_no_effect(runtime_db):
    factory, new_task = runtime_db
    task_id = new_task()
    class Gate:
        def pause_remaining(self): return 0
    class Handler:
        calls = 0
        def prepare(self, lease, context):
            self.calls += 1
            return ExecutionResult(checkpoint={"done": True})
    handler = Handler()
    worker = Worker(CONFIG, factory, {TaskType.SHOP_DISCOVERY: handler}, Gate(), Metrics(), "worker-test")
    message = TaskMessage(task_id, TaskType.SHOP_DISCOVERY)
    assert worker.process(message) == "ack"
    assert worker.process(message) == "ack" and handler.calls == 1
    with factory() as db:
        assert db.scalar(select(CrawlTask).where(CrawlTask.task_id == task_id)).status == TaskStatus.SUCCEEDED
        assert db.scalar(select(CrawlAttempt)).succeeded is True


def test_worker_validation_failure_becomes_dead_and_pause_does_not_spend_attempt(runtime_db):
    factory, new_task = runtime_db
    task_id = new_task()
    class Gate:
        def pause_remaining(self): return 0
    class Handler:
        def prepare(self, lease, context):
            raise TaskFailure(Failure("invalid_fields", "字段校验失败。"))
    worker = Worker(CONFIG, factory, {TaskType.SHOP_DISCOVERY: Handler()}, Gate(), Metrics(), "worker-test")
    assert worker.process(TaskMessage(task_id, TaskType.SHOP_DISCOVERY)) == "reject"
    other = new_task()
    class Paused:
        def pause_remaining(self): return 30
    worker.gate = Paused()
    assert worker.process(TaskMessage(other, TaskType.SHOP_DISCOVERY)) == "ack"
    with factory() as db:
        task = db.scalar(select(CrawlTask).where(CrawlTask.task_id == other))
        assert task.attempt_count == 0 and task.available_at > database_now(db)


def test_api_reads_real_mysql_and_mutations_default_disabled(runtime_db, mysql_engine):
    factory, new_task = runtime_db
    task_id = new_task()
    with TestClient(create_app(settings=Settings(), engine=mysql_engine)) as client:
        assert client.get("/health/ready").status_code == 200
        assert client.get("/api/v1/capabilities").json()["enabled_task_types"] == []
        assert client.get("/api/v1/tasks").json()["items"][0]["task_id"] == task_id
        assert client.get(f"/api/v1/tasks/{task_id}/attempts").json()["attempts"] == []
        assert client.get("/api/v1/products").json()["items"] == []
        assert client.get("/api/v1/events").json()["items"] == []
        assert client.get("/api/v1/shops").status_code == 200
        assert client.get("/api/v1/overview").status_code == 200
        assert client.get("/metrics").status_code == 200
        assert client.post("/api/v1/tasks", json={"task_type": "shop_discovery", "resource_id": "1"}).status_code == 503
        error = client.get("/api/v1/tasks?limit=do-not-echo-password")
        assert error.status_code == 422 and "do-not-echo-password" not in error.text


def test_api_rejects_unknown_collector_and_bad_token(runtime_db, mysql_engine):
    token = "local-test-not-real-secret-12345"
    config = replace(CONFIG, api_token=token)
    with TestClient(create_app(settings=config, engine=mysql_engine)) as client:
        body = {"task_type": "report_generation", "resource_id": "1"}
        assert client.post("/api/v1/tasks", json=body).status_code == 401
        assert client.post("/api/v1/tasks", json=body, headers={"Authorization": f"Bearer {token}"}).status_code == 409


def test_worker_pumps_broker_and_renews_lease_during_slow_prepare(runtime_db, monkeypatch):
    import time
    from app.workers import runtime as worker_runtime
    factory, new_task = runtime_db
    task_id = new_task()
    config = replace(CONFIG, heartbeat_seconds=1, lease_seconds=6)
    renewed, polls = [], []
    original = worker_runtime.renew_lease
    def counted(db, lease, settings):
        renewed.append(lease.task_id)
        return original(db, lease, settings)
    monkeypatch.setattr(worker_runtime, "renew_lease", counted)
    class Gate:
        def pause_remaining(self): return 0
    class Handler:
        def prepare(self, lease, context):
            time.sleep(1.4)
            return ExecutionResult()
    worker = Worker(config, factory, {TaskType.SHOP_DISCOVERY: Handler()}, Gate(), Metrics(), "heartbeat-worker")
    assert worker.process(TaskMessage(task_id, TaskType.SHOP_DISCOVERY), lambda: polls.append(1)) == "ack"
    assert renewed and len(polls) > 1


def test_existing_session_cache_cannot_defeat_lease_fence(runtime_db):
    factory, new_task = runtime_db
    task_id = new_task()
    old = claim(factory, task_id)
    with factory() as cached:
        stale = cached.get(CrawlTask, old.id)
        assert stale.lease_token == old.lease_token
        with session_scope(factory) as db:
            db.get(CrawlTask, old.id).lease_expires_at = database_now(db) - timedelta(seconds=1)
        with session_scope(factory) as db:
            recover_expired_leases(db, CONFIG)
            db.get(CrawlTask, old.id).available_at = database_now(db) - timedelta(seconds=1)
        new = claim(factory, task_id)
        assert new.lease_token != old.lease_token
        with pytest.raises(LeaseLost):
            finalize_task(cached, old, CONFIG, result=ExecutionResult())
