import json
from dataclasses import replace
from datetime import timedelta, timezone
from decimal import Decimal
from threading import Event

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.crawlers.product_detail import DetailSnapshot
from app.main import create_app
from app.models import ChangeEvent, ChangeEventType, CrawlAttempt, CrawlTask, Product, ProductSnapshot, ProductVariant, Shop, StockStatus, TaskStatus, TaskType, VariantSnapshot
from app.models.product_content import ProductContent
from app.models.product_image import ProductImage
from app.observability.metrics import Metrics
from app.queue.contracts import TaskMessage
from app.queue.retry_policy import TaskFailure
from app.services.change_detector import detect_snapshot_changes, stock_changes
from app.services.product_detail import persist_product_detail
from app.services.snapshot_service import enqueue_due_snapshots, ensure_snapshot_task, persist_snapshot, utc_window
from app.services.task_runtime import claim_task, database_now, ExecutionResult, finalize_task
from app.settings import Settings
from app.storage.database import make_session_factory, session_scope
from app.workers.contracts import HandlerContext
from app.workers.handlers import ChangeDetectionHandler, ProductSnapshotHandler, handler_registry
from app.workers.runtime import Worker
from test_product_detail import snapshot, SID
from test_product_detail_storage import initial


CONFIG = Settings(enabled_task_types=(TaskType.PRODUCT_SNAPSHOT, TaskType.CHANGE_DETECTION))


def baseline(session):
    product, task = initial(session)
    response = snapshot()
    persist_product_detail(session, task, response)
    saved = persist_snapshot(session, task, response, update_current=False)
    return product, task, response, saved


def next_observation(session, product, response, *, seconds=60):
    now = response.captured_at + timedelta(seconds=seconds)
    next_response = replace(response, started_at=now, captured_at=now)
    task, _ = ensure_snapshot_task(session, product, now)
    saved = persist_snapshot(session, task, next_response)
    return task, next_response, saved


def detect(session, snapshot_id):
    task = session.scalar(select(CrawlTask).where(CrawlTask.task_type == TaskType.CHANGE_DETECTION,
                                                CrawlTask.payload["snapshot_id"].as_string() == str(snapshot_id)))
    return detect_snapshot_changes(session, task)


def test_baseline_immutable_replay_and_identical_later_observation(session):
    product, detail_task, response, first = baseline(session)
    assert first["snapshots_written"] == 1 and first["variant_snapshots_written"] == 2
    assert detect(session, first["snapshot_id"])["baseline"] is True
    assert session.scalar(select(func.count()).select_from(ChangeEvent)) == 0
    changed = replace(response, detail=replace(response.detail, title="Replay must not overwrite", price_min=Decimal("1.00")))
    repeated = persist_snapshot(session, detail_task, changed)
    assert repeated["snapshots_written"] == 0 and product.title != "Replay must not overwrite"
    assert session.scalar(select(ProductSnapshot)).price_min == Decimal("81.89")
    _, later, second = next_observation(session, product, response)
    assert second["snapshots_written"] == 1
    unchanged = detect(session, second["snapshot_id"])
    assert unchanged["events_written"] == 0 and unchanged["comparison_skipped"] is False
    assert session.scalar(select(func.count()).select_from(ProductSnapshot)) == 2
    assert session.scalar(select(func.count()).select_from(VariantSnapshot)) == 4
    assert product.next_capture_at > later.captured_at


def test_product_and_sku_price_changes_have_evidence_and_replay_is_idempotent(session):
    product, _, response, first = baseline(session)
    detail = response.detail
    changed = replace(detail, price_min=Decimal("70.00"), price_max=Decimal("70.00"),
                      variants=tuple(replace(item, price=Decimal("70.00")) for item in detail.variants))
    _, _, second = next_observation(session, product, replace(response, detail=changed))
    result = detect(session, second["snapshot_id"])
    assert result["events_written"] == 3 and result["previous_snapshot_id"] == first["snapshot_id"]
    rows = session.scalars(select(ChangeEvent)).all()
    assert all(row.event_type == ChangeEventType.PRICE_CHANGED for row in rows)
    assert all(row.previous_snapshot_id == int(first["snapshot_id"]) and row.snapshot_id == int(second["snapshot_id"]) for row in rows)
    product_event = next(row for row in rows if row.variant_id is None)
    assert product_event.old_value["price_min"] == "81.89" and product_event.new_value["price_min"] == "70.00"
    assert detect(session, second["snapshot_id"])["events_written"] == 0
    assert session.scalar(select(func.count()).select_from(ChangeEvent)) == 3
    assert detect(session, first["snapshot_id"])["events_written"] == 0


@pytest.mark.parametrize("before,after,event", [
    (StockStatus.IN_STOCK, StockStatus.OUT_OF_STOCK, ChangeEventType.STOCKOUT),
    (StockStatus.PARTIAL, StockStatus.OUT_OF_STOCK, ChangeEventType.STOCKOUT),
    (StockStatus.OUT_OF_STOCK, StockStatus.PARTIAL, ChangeEventType.RESTOCKED),
    (StockStatus.OUT_OF_STOCK, StockStatus.IN_STOCK, ChangeEventType.RESTOCKED),
    (StockStatus.UNKNOWN, StockStatus.OUT_OF_STOCK, None),
    (StockStatus.IN_STOCK, StockStatus.UNKNOWN, None),
    (StockStatus.PARTIAL, StockStatus.IN_STOCK, None),
])
def test_stock_transition_semantics(before, after, event):
    changes = stock_changes(before, after)
    assert [row.event_type for row in changes] == ([event] if event else [])


def test_known_stockout_then_restock_and_unknown_intermediate_not_bridged(session):
    product, _, response, first = baseline(session)
    out = replace(response.detail, stock_status=StockStatus.OUT_OF_STOCK,
                  variants=tuple(replace(item, stock_status=StockStatus.OUT_OF_STOCK, available_quantity=0) for item in response.detail.variants))
    _, out_response, second = next_observation(session, product, replace(response, detail=out))
    assert detect(session, second["snapshot_id"])["events_written"] == 2  # product + previously in-stock SKU
    _, _, third = next_observation(session, product, replace(out_response, detail=response.detail))
    assert detect(session, third["snapshot_id"])["events_written"] == 2
    kinds = list(session.scalars(select(ChangeEvent.event_type)))
    assert kinds.count(ChangeEventType.STOCKOUT) == kinds.count(ChangeEventType.RESTOCKED) == 2


def test_null_prices_and_unknown_stock_are_not_change_events(session):
    product, _, response, _ = baseline(session)
    missing = replace(response.detail, price_min=None, price_max=None, stock_status=StockStatus.UNKNOWN,
                      variants=tuple(replace(item, price=None, stock_status=StockStatus.UNKNOWN) for item in response.detail.variants))
    _, unknown_response, second = next_observation(session, product, replace(response, detail=missing))
    assert detect(session, second["snapshot_id"])["events_written"] == 0
    out = replace(response.detail, stock_status=StockStatus.OUT_OF_STOCK,
                  variants=tuple(replace(item, stock_status=StockStatus.OUT_OF_STOCK) for item in response.detail.variants))
    _, _, third = next_observation(session, product, replace(unknown_response, detail=out))
    assert detect(session, third["snapshot_id"])["events_written"] == 0


def test_explicit_discount_start_and_end_skip_missing_value(session):
    product, task = initial(session)
    response = replace(snapshot(), detail=replace(snapshot().detail, discount_rate=Decimal("0.0000")))
    persist_product_detail(session, task, response)
    persist_snapshot(session, task, response, update_current=False)
    sale = replace(response.detail, discount_rate=Decimal("0.2000"))
    _, sale_response, second = next_observation(session, product, replace(response, detail=sale))
    assert detect(session, second["snapshot_id"])["events_written"] == 1
    assert session.scalar(select(ChangeEvent)).event_type == ChangeEventType.DISCOUNT_STARTED
    _, _, third = next_observation(session, product, replace(sale_response, detail=response.detail))
    assert detect(session, third["snapshot_id"])["events_written"] == 1


def test_missing_observed_sku_changes_specification_set_without_fabricating_stockout(session):
    product, _, response, _ = baseline(session)
    reduced = replace(response.detail, variants=response.detail.variants[:1], stock_status=StockStatus.IN_STOCK)
    _, _, second = next_observation(session, product, replace(response, detail=reduced))
    assert detect(session, second["snapshot_id"])["events_written"] == 1
    event = session.scalar(select(ChangeEvent))
    assert event.event_type == ChangeEventType.VARIANT_CHANGED and event.variant_id is None
    assert session.scalar(select(func.count()).select_from(ProductVariant)) == 2
    assert session.scalar(select(func.count()).select_from(VariantSnapshot)) == 3


def test_stable_content_change_is_separate_from_price_and_null_enrichment(session):
    product, _, response, _ = baseline(session)
    changed = replace(response.detail, title="New merchant title", attributes={**response.detail.attributes, "season": "Summer"})
    _, _, second = next_observation(session, product, replace(response, detail=changed))
    assert detect(session, second["snapshot_id"])["events_written"] == 1
    event = session.scalar(select(ChangeEvent))
    assert event.event_type == ChangeEventType.CONTENT_CHANGED and "season" not in event.new_value


def test_parser_version_changes_are_not_measured_price_changes(session):
    product, _, response, first = baseline(session)
    session.get(ProductSnapshot, int(first["snapshot_id"])).parser_version = "older_parser"
    session.flush()
    changed = replace(response.detail, price_min=Decimal("70.00"), price_max=Decimal("70.00"))
    _, _, second = next_observation(session, product, replace(response, detail=changed))
    skipped = detect(session, second["snapshot_id"])
    assert skipped["events_written"] == 0 and skipped["comparison_skipped"] is True


def test_changed_currency_is_not_compared_and_invalid_current_projection_fails(session):
    product, _, response, first = baseline(session)
    session.get(ProductSnapshot, int(first["snapshot_id"])).currency = "GBP"
    session.flush()
    _, _, second = next_observation(session, product, response)
    assert detect(session, second["snapshot_id"])["comparison_skipped"] is True
    current = session.get(ProductSnapshot, int(second["snapshot_id"]))
    current.raw_payload = {"snapshot_format": 1}
    session.flush()
    with pytest.raises(TaskFailure) as error:
        detect(session, second["snapshot_id"])
    assert error.value.failure.code == "invalid_change_snapshot"


def test_late_historical_insert_is_rejected_and_first_window_is_unchanged(session):
    product, _, response, first = baseline(session)
    next_observation(session, product, response)
    window = response.captured_at + timedelta(seconds=20)
    task, _ = ensure_snapshot_task(session, product, window)
    older = replace(response, started_at=window, captured_at=window)
    with pytest.raises(TaskFailure) as error:
        persist_snapshot(session, task, older)
    assert error.value.failure.code == "out_of_order_snapshot"
    assert session.scalar(select(func.count()).select_from(ProductSnapshot)) == 2


def test_snapshot_and_detection_enqueue_roll_back_with_worker_result(session, monkeypatch, tmp_path):
    product, _ = initial(session)
    response = snapshot()
    persist_product_detail(session, session.scalar(select(CrawlTask)), response)
    window = utc_window(response.captured_at, 3600)
    task, _ = ensure_snapshot_task(session, product, window)
    lease = claim_task(session, task.task_id, task.task_type, "snapshot-test", CONFIG)
    monkeypatch.setattr("app.workers.handlers.TikTokShopAdapter.fetch_product_detail", lambda self, source, shop, stop: response)
    result = ProductSnapshotHandler().prepare(lease, HandlerContext(replace(CONFIG, evidence_directory=tmp_path), None, Event()))
    original_apply = result.apply
    def fail(db, current):
        original_apply(db, current)
        raise RuntimeError("roll back snapshot and child task")
    with pytest.raises(RuntimeError), session.begin_nested():
        finalize_task(session, lease, CONFIG, result=replace(result, apply=fail))
    assert session.scalar(select(func.count()).select_from(ProductSnapshot)) == 0
    assert session.scalar(select(func.count()).select_from(VariantSnapshot)) == 0
    assert session.scalar(select(func.count()).select_from(CrawlTask).where(CrawlTask.task_type == TaskType.CHANGE_DETECTION)) == 0


def test_periodic_scheduling_requires_explicit_scope_and_is_idempotent(session):
    product, _, response, _ = baseline(session)
    assert enqueue_due_snapshots(session, Settings()) == 0
    config = replace(CONFIG, periodic_snapshots_enabled=True, monitored_product_ids=(product.platform_product_id,))
    assert enqueue_due_snapshots(session, config) == 1
    assert enqueue_due_snapshots(session, config) == 0
    task = session.scalar(select(CrawlTask).where(CrawlTask.task_type == TaskType.PRODUCT_SNAPSHOT))
    task.status, task.checkpoint = TaskStatus.CANCELLED, {"keep": True}
    session.flush()
    assert enqueue_due_snapshots(session, config) == 0 and task.checkpoint == {"keep": True}
    with pytest.raises(ValueError): replace(CONFIG, periodic_snapshots_enabled=True)
    with pytest.raises(ValueError): replace(CONFIG, snapshot_interval_seconds=1)


def test_saved_detail_projection_restores_exact_values_and_rejects_other_schema(tmp_path):
    response = snapshot()
    record = json.loads(response.save_evidence(tmp_path).read_text(encoding="utf-8"))
    assert DetailSnapshot.from_evidence(record) == response
    record["parser_version"] = "unknown"
    with pytest.raises(ValueError): DetailSnapshot.from_evidence(record)


def test_utc_window_same_in_all_timezones():
    response = snapshot()
    local = response.captured_at.astimezone(timezone(timedelta(hours=8)))
    assert utc_window(local, 3600) == utc_window(response.captured_at, 3600)
    with pytest.raises(ValueError): utc_window(local.replace(tzinfo=None), 3600)


@pytest.fixture
def committed_snapshot_data(mysql_engine):
    factory = make_session_factory(mysql_engine)
    with session_scope(factory) as db:
        product, _, response, first = baseline(db)
        changed = replace(response.detail, price_min=Decimal("70.00"), price_max=Decimal("70.00"))
        _, _, second = next_observation(db, product, replace(response, detail=changed))
        detection = db.scalar(select(CrawlTask).where(CrawlTask.task_type == TaskType.CHANGE_DETECTION,
                                  CrawlTask.payload["snapshot_id"].as_string() == second["snapshot_id"]))
        detection_uuid, product_id, shop_id = detection.task_id, product.id, product.shop_id
    yield factory, product_id, first, second, detection_uuid
    with session_scope(factory) as db:
        task_ids = list(db.scalars(select(CrawlTask.id).where(CrawlTask.resource_id == str(product_id))))
        db.execute(delete(ChangeEvent).where(ChangeEvent.product_id == product_id))
        db.execute(delete(VariantSnapshot).where(VariantSnapshot.product_id == product_id))
        db.execute(delete(ProductSnapshot).where(ProductSnapshot.product_id == product_id))
        db.execute(delete(ProductContent).where(ProductContent.product_id == product_id))
        db.execute(delete(ProductImage).where(ProductImage.product_id == product_id))
        db.execute(delete(ProductVariant).where(ProductVariant.product_id == product_id))
        db.execute(delete(Product).where(Product.id == product_id))
        db.execute(delete(CrawlAttempt).where(CrawlAttempt.task_id.in_(task_ids)))
        db.execute(delete(CrawlTask).where(CrawlTask.id.in_(task_ids)))
        db.execute(delete(Shop).where(Shop.id == shop_id))


def test_change_worker_continues_during_site_pause_and_api_evidence_is_visible(committed_snapshot_data, mysql_engine):
    factory, product_id, first, second, detection_uuid = committed_snapshot_data
    class PausedGate:
        def pause_remaining(self): raise AssertionError("Local comparisons must not wait on website pause")
    worker = Worker(CONFIG, factory, handler_registry(), PausedGate(), Metrics(), "comparison-test")
    assert worker.process(TaskMessage(detection_uuid, TaskType.CHANGE_DETECTION)) == "ack"
    assert worker.process(TaskMessage(detection_uuid, TaskType.CHANGE_DETECTION)) == "ack"
    with TestClient(create_app(settings=Settings(), engine=mysql_engine)) as client:
        history = client.get(f"/api/v1/products/{product_id}/snapshots").json()
        assert len(history["items"]) == 2 and history["items"][0]["variant_count"] == 2
        rows = client.get(f"/api/v1/products/{product_id}/snapshots/{second['snapshot_id']}/variants").json()
        assert len(rows["items"]) == 2
        events = client.get(f"/api/v1/events?product_id={product_id}&event_type=price_changed").json()
        assert len(events["items"]) == 1
        assert events["items"][0]["previous_snapshot_id"] == first["snapshot_id"]
        assert events["items"][0]["new_value"]["price_min"] == "70.00"
        assert client.get(f"/api/v1/products/{product_id}/snapshots/999999/variants").status_code == 404
