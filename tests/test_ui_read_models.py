from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import sessionmaker

from app.api.routes_ui_v2 import router
from app.models import Product
from app.models.analytics_read import AnalyticsRefreshRequest as Job, AnalyticsSnapshot, EventReadModel, ProductReadModel
from app.services.analytics_projection import build_event, build_product, build_products, publish_event, publish_product
from app.services.analytics_queries import list_events, list_products, parameters
from app.services.analytics_refresh import VERSION, LeaseLost, claim, claim_batch, complete, complete_batch, enqueue, failed_batch, job_row, mark_products, now, scope
from app.services.analytics_runtime import AnalyticsRunner, backfill_batch
from app.services.analytics_service import build_workbench
from app.services.analytics_snapshot import build_current, build_overview, publish_snapshot, read_snapshot
from app.settings import Settings
from test_analytics import NOW, analytics_db, analytics_engine


def project(db):
    config = Settings()
    for identifier in range(1, 8):
        publish_product(db, config, build_product(db, config, identifier, 1))
    for identifier in (1, 2, 3):
        publish_event(db, config, build_event(db, config, identifier))
    db.commit()


def test_list_has_no_aggregate_and_counts_only_observed_skus(analytics_db):
    project(analytics_db)
    statements = []
    engine = analytics_db.get_bind()
    def capture(conn, cursor, statement, *args):
        statements.append(statement.lower())
    event.listen(engine, "before_cursor_execute", capture)
    try:
        result = list_products(analytics_db, parameters(Settings()), page_size=2, sort="price_asc")
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert [item["id"] for item in result["items"]] == ["1", "2"]
    assert result["items"][0]["variant_count"] == 2
    assert result["total"] is None and result["has_more"]
    assert len(statements) == 1
    assert not any(word in statements[0] for word in ("product_variants", "product_snapshots", "count(", "group by"))
    assert list_products(analytics_db, parameters(Settings(), {"attribute": "color", "value": "Red"}))["items"]


def test_recent_pages_break_equal_observation_times_without_duplicates(analytics_db):
    project(analytics_db)
    first = list_products(analytics_db, parameters(Settings()), page=1, page_size=2)
    second = list_products(analytics_db, parameters(Settings()), page=2, page_size=2)
    assert [item["id"] for item in first["items"]] == ["4", "3"]
    assert [item["id"] for item in second["items"]] == ["2", "1"]


def test_projection_statistics_preserve_existing_workbench_numbers(analytics_db):
    import time
    project(analytics_db)
    old = build_workbench(analytics_db, Settings(), as_of=NOW)
    new = build_current(analytics_db, parameters(Settings()), NOW, time.monotonic() + 30)
    assert new["summary"] == old["summary"]
    for field in ("price_histogram", "categories", "shops", "stock_distribution", "discount_buckets", "attribute_distributions",
                  "price_rating_scatter", "color_size_matrix", "collection_quality", "history_summary", "event_trend_daily", "events_by_type"):
        assert new[field] == old[field], field


@pytest.mark.parametrize("options", [{"currency": "EUR"}, {"shop_id": 2}, {"category": "Dresses"}, {"price_unknown": True}])
def test_filtered_projection_statistics_preserve_scope(analytics_db, options):
    import time
    project(analytics_db)
    old = build_workbench(analytics_db, Settings(), as_of=NOW, **options)
    new = build_current(analytics_db, parameters(Settings(), options), NOW, time.monotonic() + 30)
    assert new["summary"] == old["summary"]
    assert new["history_summary"] == old["history_summary"]


def test_snapshot_pending_coalesces_and_stale_result_survives_refresh(analytics_db):
    params = parameters(Settings())
    status, first = read_snapshot(analytics_db, "catalog_stats", params)
    assert status == 202
    read_snapshot(analytics_db, "catalog_stats", params)
    job = analytics_db.get(Job, (VERSION, "catalog_stats", first["_meta"]["query_key"]))
    assert job.requested_revision == 1
    stamp = now(analytics_db) - timedelta(seconds=90)
    publish_snapshot(analytics_db, "catalog_stats", params, 1, {"total": 4}, stamp)
    analytics_db.flush()
    saved = analytics_db.get(AnalyticsSnapshot, (VERSION, job.key_hash))
    saved.fresh_until = now(analytics_db) - timedelta(seconds=1)
    job.completed_revision = 1
    analytics_db.flush()
    status, result = read_snapshot(analytics_db, "catalog_stats", params)
    assert status == 200 and result["total"] == 4 and result["_meta"]["status"] == "stale"
    assert job.requested_revision == 2 and saved.payload == {"total": 4}


def test_changes_arriving_during_refresh_remain_pending_and_old_lease_cannot_publish(analytics_db):
    row = enqueue(analytics_db, "product_projection", {"product_id": "1"})
    lease = claim(analytics_db, Settings())
    assert lease is not None
    enqueue(analytics_db, "product_projection", {"product_id": "1"}, force=True)
    complete(analytics_db, lease, lambda db: None)
    assert row.completed_revision == 1 and row.requested_revision == 2
    with pytest.raises(LeaseLost):
        complete(analytics_db, lease, lambda db: pytest.fail("stale owner published"))


def test_batched_products_preserve_both_bases_with_fewer_queries(analytics_db, monkeypatch):
    from app.services import analytics_projection
    monkeypatch.setattr(analytics_projection, "now", lambda db: NOW)
    statements = []
    def capture(conn, cursor, statement, *args):
        statements.append(statement)
    engine = analytics_db.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        analytics_db.expunge_all()
        expected = [row for identifier in range(1, 9)
                    for row in build_product(analytics_db, Settings(), identifier, 3)]
        individual_count = len(statements)
        statements.clear()
        analytics_db.expunge_all()
        actual = build_products(analytics_db, Settings(), dict.fromkeys(range(1, 9), 3))
        assert actual == expected
        assert len(statements) < individual_count - 10
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def test_batch_publication_preserves_new_revisions_and_fences_every_member(analytics_db):
    for identifier in (1, 2):
        enqueue(analytics_db, "product_projection", {"product_id": str(identifier)})
    leases = claim_batch(analytics_db, Settings())
    assert len(leases) == 2
    enqueue(analytics_db, leases[0].kind, leases[0].parameters, force=True)
    complete_batch(analytics_db, leases, lambda db: publish_product(db, Settings(), build_products(
        db, Settings(), {int(item.parameters["product_id"]): item.revision for item in leases})))
    changed = analytics_db.get(Job, (VERSION, leases[0].kind, leases[0].key))
    assert changed.is_pending and changed.requested_revision == 2 and changed.completed_revision == 1
    next_leases = claim_batch(analytics_db, Settings())
    assert len(next_leases) == 1
    with pytest.raises(LeaseLost):
        complete_batch(analytics_db, leases, lambda db: pytest.fail("partially stale batch published"))


def test_failed_batch_retries_members_individually(analytics_db):
    for identifier in range(1, 11):
        enqueue(analytics_db, "product_projection", {"product_id": str(identifier)})
    leases = claim_batch(analytics_db, Settings())
    assert len(leases) == 8
    failed_batch(analytics_db, leases, ValueError("invalid source"))
    for lease in leases:
        analytics_db.get(Job, (VERSION, lease.kind, lease.key)).available_at = NOW - timedelta(days=1)
    analytics_db.flush()
    retry = claim_batch(analytics_db, Settings())
    assert len(retry) == 1


def test_busy_statistics_keep_batching_without_recording_source_failures(analytics_db):
    from app.queue.retry_policy import Failure, TaskFailure
    for hours in (24, 168, 720):
        enqueue(analytics_db, "workbench", parameters(Settings(), {"lookback_hours": hours}))
    leases = claim_batch(analytics_db, Settings(), prefer_statistics=True)
    failed_batch(analytics_db, leases, TaskFailure(Failure("analytics_heavy_busy", "busy", True, 300)))
    for lease in leases:
        row = analytics_db.get(Job, (VERSION, lease.kind, lease.key))
        assert row.attempts == 0 and row.last_error_code is None and row.is_pending
        row.available_at = NOW
    analytics_db.flush()
    assert len(claim_batch(analytics_db, Settings(), prefer_statistics=True)) == 3


@pytest.mark.parametrize("kind", ["workbench", "overview"])
def test_window_batches_match_independent_statistics_and_keep_filters_isolated(analytics_db, monkeypatch, kind):
    from app.models import ProductSnapshot
    from app.services import analytics_snapshot
    analytics_db.get(ProductSnapshot, 2).captured_at = NOW - timedelta(hours=48)
    project(analytics_db)
    monkeypatch.setattr(analytics_snapshot, "now", lambda db: NOW)
    config = Settings()
    windows = [parameters(config, {"lookback_hours": hours}) for hours in (24, 168, 720)]
    for params in windows:
        enqueue(analytics_db, kind, params)
    enqueue(analytics_db, kind, parameters(config, {"lookback_hours": 24, "shop_id": 2}))
    leases = claim_batch(analytics_db, config, prefer_statistics=True)
    assert len(leases) == 3
    assert {item.parameters["lookback_hours"] for item in leases} == {24, 168, 720}
    expected = [analytics_snapshot.build_snapshot(analytics_db, config, kind, params) for params in windows]
    original = getattr(analytics_snapshot, "build_current" if kind == "workbench" else "build_overview")
    calls = []
    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(analytics_snapshot, "build_current" if kind == "workbench" else "build_overview", counted)
    actual = analytics_snapshot.build_snapshots(analytics_db, config, kind, windows)
    assert actual == expected
    assert len(calls) == 1
    assert actual[0][0]["summary"]["events_in_window"] < actual[1][0]["summary"]["events_in_window"]


def test_statistics_drop_large_urls_but_preserve_image_coverage(analytics_db):
    import time
    from app.services.analytics_snapshot import build_catalog_stats, rows_for
    project(analytics_db)
    row = analytics_db.get(ProductReadModel, (VERSION, "current", 1))
    row.payload = {**row.payload, "image_url": "https://example.com/" + "x" * 8000,
                   "thumbnail_url": "https://example.com/" + "y" * 8000}
    analytics_db.flush()
    rows = list(rows_for(analytics_db, parameters(Settings()), "current", time.monotonic() + 30))
    assert rows[0][1]["image_url"] == 1 and "thumbnail_url" not in rows[0][1]
    stats = build_catalog_stats(analytics_db, parameters(Settings()), NOW, time.monotonic() + 30)
    assert next(field for field in stats["field_coverage"] if field["field"] == "image")["known_count"] == 1
    assert len(row.payload["image_url"]) > 8000


def test_transactional_dirty_mark_rolls_back_with_price_write(analytics_db):
    analytics_db.info["analytics_enabled"] = True
    old = analytics_db.get(Product, 1).price_min
    analytics_db.get(Product, 1).price_min = 5
    mark_products(analytics_db, [1, 1])
    mark_products(analytics_db, [1])
    assert analytics_db.scalar(select(Job.requested_revision).where(Job.kind == "product_projection")) == 1
    analytics_db.rollback()
    assert analytics_db.get(Product, 1).price_min == old
    assert analytics_db.scalar(select(func.count()).select_from(Job)) == 0


def test_two_statistics_filters_keep_independent_results(analytics_db):
    for shop, total in ((1, 2), (2, 3)):
        params = parameters(Settings(), {"shop_id": shop})
        publish_snapshot(analytics_db, "catalog_stats", params, 1, {"total": total}, now(analytics_db))
    analytics_db.flush()
    assert read_snapshot(analytics_db, "catalog_stats", parameters(Settings(), {"shop_id": 1}))[1]["total"] == 2
    assert read_snapshot(analytics_db, "catalog_stats", parameters(Settings(), {"shop_id": 2}))[1]["total"] == 3


def test_equivalent_filters_share_the_prewarmed_query_key():
    assert parameters(Settings(), {"q": "  ", "currency": "usd", "price_upper_exclusive": False}) == parameters(Settings())
    assert parameters(Settings(), {"price_min": "10.0"}) == parameters(Settings(), {"price_min": 10})


def test_backfill_resumes_without_flooding_a_persisted_backlog(analytics_db):
    for identifier in range(10000, 10512):
        enqueue(analytics_db, "product_projection", {"product_id": str(identifier)})
    analytics_db.commit()
    assert not backfill_batch(analytics_db, Settings())
    assert analytics_db.scalar(select(func.count()).select_from(Job).where(Job.kind == "product_projection")) == 512
    row = job_row(analytics_db, "control", {**scope(Settings()), "name": "backfill"})
    assert "product_after" not in row.parameters


def test_event_cursor_rejects_changed_filter_and_freezes_published_watermark(analytics_db):
    project(analytics_db)
    params = parameters(Settings(), {"lookback_hours": 720})
    first = list_events(analytics_db, Settings(), params, limit=1)
    assert len(first["items"]) == 1 and first["next_cursor"]
    second = list_events(analytics_db, Settings(), params, cursor=first["next_cursor"], limit=1)
    assert first["items"][0]["id"] != second["items"][0]["id"]
    assert second["window"] == first["window"]
    with pytest.raises(ValueError):
        list_events(analytics_db, Settings(), {**params, "q": "other"}, cursor=first["next_cursor"])


def test_v2_api_reads_published_data_and_does_not_call_legacy_builder(analytics_db, analytics_engine, monkeypatch):
    project(analytics_db)
    config = Settings()
    control = job_row(analytics_db, "control", {**scope(config), "name": "backfill"})
    control.parameters = {**control.parameters, "ready": True}
    analytics_db.commit()
    monkeypatch.setenv("ANALYTICS_ENABLED", "1")
    app = FastAPI()
    app.include_router(router)
    app.state.settings = config
    app.state.factory = sessionmaker(analytics_engine)
    with TestClient(app) as client:
        assert client.get("/api/v2/ui/bootstrap").json()["ready"]
        result = client.get("/api/v2/catalog/products?page_size=2")
        assert result.status_code == 200 and len(result.json()["items"]) == 2
        assert client.get("/api/v2/catalog/stats").status_code == 202
        assert client.get("/api/v2/events?limit=1&lookback_hours=720").status_code == 200
        assert client.get("/api/v2/catalog/products?sort=invalid").status_code == 422
