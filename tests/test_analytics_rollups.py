import time
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import delete, event

from app.models import Product, Shop
from app.models.analytics_read import AnalyticsProductRollup
from app.services.analytics_projection import build_product, publish_product
from app.services.analytics_queries import list_products, parameters
from app.services.analytics_refresh import SUMMARY_KINDS, claim_batch, enqueue, job_row, now, scope
from app.services.analytics_rollup import backfill_rollups, control_params, read_totals, rollups_ready, update_rollups
from app.services.analytics_snapshot import build_catalog_stats, build_current, build_overview
from app.settings import Settings
from test_analytics import NOW, analytics_db, analytics_engine
from test_ui_read_models import project


def enable(db):
    for _ in range(4):
        if backfill_rollups(db, Settings()):
            db.flush()
            return
    raise AssertionError("fixture backfill did not finish")


def results(db, stamp=NOW):
    return [build(db, parameters(Settings()), stamp, time.monotonic() + 30)
            for build in (build_current, build_overview, build_catalog_stats)]


def test_rollups_equal_full_scan_and_do_not_read_product_json(analytics_db):
    project(analytics_db)
    expected = results(analytics_db)
    filters = [{"attribute": "color", "value": "Red"}, {"attribute": "size", "value": "S"},
               {"attribute": "brand", "value": "Brand B"}, {"attribute": "material", "value": "Cotton"}]
    pages = [list_products(analytics_db, parameters(Settings(), options)) for options in filters]
    enable(analytics_db)
    assert [list_products(analytics_db, parameters(Settings(), options)) for options in filters] == pages
    statements = []
    def capture(conn, cursor, statement, *args):
        statements.append(statement.lower())
    engine = analytics_db.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert results(analytics_db) == expected
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert not any("product_read_models.payload" in sql or "product_read_models.facts" in sql for sql in statements)
    assert not any("analytics_product_rollups.members" in sql for sql in statements)
    # Time alone changes freshness; no write or re-bootstrap is necessary.
    later = results(analytics_db, NOW + timedelta(days=9))[0]
    assert later["collection_quality"]["freshness_buckets"][-2]["count"] == 3


def test_incremental_replacement_is_idempotent_and_moves_scope(analytics_db):
    project(analytics_db)
    enable(analytics_db)
    old = build_product(analytics_db, Settings(), 1, 1)
    product = analytics_db.get(Product, 1)
    product.price_min = product.price_max = Decimal("90.09")
    product.color_summary = [" Green ", "Green", "green"]
    product.shop_id = 2
    product.currency = "EUR"
    analytics_db.flush()
    new = build_product(analytics_db, Settings(), 1, 2)
    publish_product(analytics_db, Settings(), new)
    publish_product(analytics_db, Settings(), new)
    # Both projection fencing and a late bootstrap must retain the newer revision.
    publish_product(analytics_db, Settings(), old)
    update_rollups(analytics_db, old)
    analytics_db.flush()
    actual = results(analytics_db)
    row = job_row(analytics_db, "control", control_params(Settings()))
    row.parameters = {**row.parameters, "ready": False}
    analytics_db.flush()
    assert actual == results(analytics_db)
    assert actual[0]["summary"]["products"] == 3
    assert actual[0]["summary"]["price_stats"]["median"] == "25.01"


def test_rollup_and_projection_rollback_together(analytics_db):
    project(analytics_db)
    enable(analytics_db)
    analytics_db.commit()
    before = results(analytics_db)
    with analytics_db.begin_nested() as transaction:
        product = analytics_db.get(Product, 1)
        product.price_min = product.price_max = Decimal("88")
        analytics_db.flush()
        publish_product(analytics_db, Settings(), build_product(analytics_db, Settings(), 1, 2))
        analytics_db.flush()
        assert results(analytics_db) != before
        transaction.rollback()
    assert results(analytics_db) == before


def test_bootstrap_resumes_and_filtered_queries_keep_sql_semantics(analytics_db):
    project(analytics_db)
    analytics_db.execute(delete(AnalyticsProductRollup))
    assert not rollups_ready(analytics_db, Settings())
    expected = results(analytics_db)
    assert not backfill_rollups(analytics_db, Settings())
    analytics_db.commit()
    assert backfill_rollups(analytics_db, Settings())
    analytics_db.flush()
    assert results(analytics_db) == expected
    assert read_totals(analytics_db, parameters(Settings(), {"category": "Dresses"}), "current", time.monotonic() + 1) is None
    analytics_db.get(Shop, 1).canonical_name = "Renamed"
    analytics_db.flush()
    assert results(analytics_db)[0]["shops"][0]["name"] == "Renamed"


def test_shards_merge_exact_medians_and_scatter_order(analytics_db):
    project(analytics_db)
    source = build_product(analytics_db, Settings(), 1, 1)
    rows = []
    for identifier in range(10, 530):
        for original in source:
            value = deepcopy(original)
            value.update(product_id=identifier, platform_product_id=str(identifier))
            value["payload"].update(id=str(identifier), price_min=f"{identifier % 90}.01")
            value["price_min"] = Decimal(value["payload"]["price_min"])
            rows.append(value)
    publish_product(analytics_db, Settings(), rows)
    analytics_db.flush()
    expected = results(analytics_db)
    while not backfill_rollups(analytics_db, Settings()):
        analytics_db.flush()
    analytics_db.flush()
    actual = results(analytics_db)
    assert actual == expected
    assert actual[0]["price_rating_scatter"]["truncated"]
    assert actual[0]["summary"]["price_stats"]["median_sample_count"] == 523


def test_default_statistics_lane_cannot_be_taken_by_a_heavy_filtered_job(analytics_db):
    params = parameters(Settings())
    enqueue(analytics_db, "workbench", {**params, "shop_id": 1})
    enqueue(analytics_db, "workbench", {**params, "lookback_hours": 24})
    default = claim_batch(analytics_db, Settings(), slot_name="analysis_summary", only_kinds=SUMMARY_KINDS,
                          default_statistics=True)
    custom = claim_batch(analytics_db, Settings(), slot_name="analysis_heavy", only_kinds=SUMMARY_KINDS,
                         default_statistics=False)
    assert default[0].parameters == {**params, "lookback_hours": 24}
    assert custom[0].parameters == {**params, "shop_id": 1}


def test_rollup_upgrade_refreshes_defaults_without_inheriting_old_due_times(analytics_db):
    project(analytics_db)
    config = Settings()
    source = job_row(analytics_db, "control", {**scope(config), "name": "backfill"})
    source.parameters = {**source.parameters, "ready": True}
    request = enqueue(analytics_db, "catalog_stats", parameters(config), available_at=now(analytics_db) + timedelta(minutes=5))
    enable(analytics_db)
    assert request.available_at <= now(analytics_db)
