"""Meaningful analytics checks use only a disposable in-memory database."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.schema import DropTable
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

from app.api.routes_analytics import router
from app.models import Base, ChangeEvent, Product, ProductSnapshot, ProductVariant, Shop
from app.models.product_image import ProductImage
from app.services.analytics_service import build_workbench, list_analytics_products
from app.settings import Settings


NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


@pytest.fixture
def analytics_engine():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    # Keep production mappings intact. Clone only test DDL into portable types.
    metadata = MetaData()
    for table in list(Base.metadata.sorted_tables):
        clone = table.to_metadata(metadata)
        for column in clone.columns:
            column.type = column.type.copy() if isinstance(column.type, TypeDecorator) else column.type.as_generic()
            if hasattr(column.type, "collation"):
                column.type.collation = None
            column.server_default = None
            column.server_onupdate = None
    metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def analytics_db(analytics_engine):
    with Session(analytics_engine) as db:
        def product(identifier, shop_id=1, **kwargs):
            return Product(id=identifier, shop_id=shop_id, platform="tiktok_shop", region="US",
                           platform_product_id=str(1000 + identifier), product_url=f"https://shop.tiktok.com/p/{identifier}",
                           title=f"Dress {identifier}", currency="USD", pricing_context="public_listed",
                           created_at=NOW, updated_at=NOW, first_seen_at=NOW - timedelta(days=4),
                           last_seen_at=NOW, **kwargs)
        db.add_all([Shop(id=1, platform="tiktok_shop", region="US", platform_shop_id="shop1", canonical_name="Alpha",
                         currency="USD", first_seen_at=NOW, last_seen_at=NOW, created_at=NOW, updated_at=NOW),
                    Shop(id=2, platform="tiktok_shop", region="US", platform_shop_id="shop2", canonical_name="Beta",
                         currency="USD", first_seen_at=NOW, last_seen_at=NOW, created_at=NOW, updated_at=NOW)])
        db.flush()
        db.add_all([
            product(1, price_min=Decimal("10.01"), price_max=Decimal("10.01"), original_category="Dresses",
                    stock_status="in_stock", last_success_at=NOW - timedelta(hours=2), discount_rate=Decimal("0.1"),
                    rating=Decimal("4.50"), review_count=10, material="Cotton", color_summary=["Red", "Red", "Blue"],
                    size_summary=["S", "M"], raw_attributes={"observed_sku_ids": ["sku1", "sku2"]}),
            product(2, price_min=Decimal("20.01"), price_max=Decimal("29.99"), original_category="Dresses",
                    stock_status="out_of_stock", last_success_at=NOW - timedelta(days=2), discount_rate=0,
                    rating=Decimal("4.00"), color_summary=["Red"], size_summary=["L"]),
            product(3, shop_id=2, price_min=Decimal("30.01"), original_category="Tops", discount_rate=None,
                    stock_status="unknown", brand="Brand B", last_success_at=NOW - timedelta(days=10)),
            product(4, shop_id=2, price_min=None, original_category=None, stock_status="unknown"),
        ])
        db.flush()
        # Other market, currency and pricing context must never contaminate USD.
        other = product(5, price_min=Decimal("999.99"), normalized_category="Dresses")
        other.currency = "EUR"
        wrong_context = product(6, price_min=Decimal("999.99"))
        wrong_context.pricing_context = "private_cart"
        wrong_market = product(7, price_min=Decimal("999.99"))
        wrong_market.region = "GB"
        db.add_all([other, wrong_context, wrong_market])
        db.add_all([ProductVariant(id=i, product_id=1, platform_variant_id=f"sku{i}", variant_key=str(i) * 64,
                                   currency="USD", stock_status=status, price=Decimal("10.01"), color="Red", size="S" if i == 1 else "M",
                                   created_at=NOW, updated_at=NOW) for i, status in [(1, "in_stock"), (2, "out_of_stock"), (3, "out_of_stock")]])
        db.flush()
        for identifier, product_id, when in [(1, 1, NOW - timedelta(hours=2)), (2, 1, NOW - timedelta(hours=1)),
                                            (3, 2, NOW - timedelta(hours=1)), (4, 5, NOW - timedelta(hours=1))]:
            db.add(ProductSnapshot(id=identifier, product_id=product_id, captured_at=when, scheduled_for=when,
                                   currency="EUR" if product_id == 5 else "USD", pricing_context="public_listed",
                                   source_status=200, parser_version="fixture", created_at=when))
        db.flush()
        db.add_all([ChangeEvent(id=1, product_id=1, snapshot_id=2, previous_snapshot_id=1, event_type="price_changed",
                                event_hash="a" * 64, detected_at=NOW - timedelta(hours=1), created_at=NOW),
                    ChangeEvent(id=2, product_id=2, snapshot_id=3, event_type="product_new", event_hash="b" * 64,
                                detected_at=NOW - timedelta(hours=1), created_at=NOW),
                    ChangeEvent(id=3, product_id=5, snapshot_id=4, event_type="product_new", event_hash="c" * 64,
                                detected_at=NOW - timedelta(hours=1), created_at=NOW)])
        db.commit()
        yield db


def by_key(rows, field):
    return {row[field]: row for row in rows}


def test_workbench_counts_every_scoped_product_and_preserves_unknowns(analytics_db):
    result = build_workbench(analytics_db, Settings(), as_of=NOW)
    assert result["summary"]["products"] == 4
    assert result["summary"]["shops"] == 2
    assert result["summary"]["detail_products"] == 3
    assert result["summary"]["price_stats"] == {"known_count": 3, "min": "10.01", "max": "30.01", "median": "20.01",
                                                "median_basis": "full_scope", "median_sample_count": 3}
    assert sum(row["count"] for row in result["price_histogram"]) == 4
    assert by_key(result["price_histogram"], "key")["unknown"]["count"] == 1
    assert by_key(result["stock_distribution"], "status")["unknown"]["count"] == 2
    assert by_key(result["discount_buckets"], "key")["unknown"]["count"] == 2
    assert by_key(result["collection_quality"]["field_coverage"], "field")["rating"]["coverage_pct"] == "50.00"
    assert by_key(result["collection_quality"]["field_coverage"], "field")["style"]["known_count"] == 0
    assert result["coverage"]["aggregation"] == "sql_full_scope"


def test_medians_attributes_and_sku_stock_do_not_count_retained_or_duplicate_rows(analytics_db):
    result = build_workbench(analytics_db, Settings(), as_of=NOW)
    dresses = by_key(result["categories"], "category")["Dresses"]
    assert dresses["price_median"] == "15.01"
    colors = result["attribute_distributions"]["colors"]
    assert colors["known_products"] == 2
    assert by_key(colors["items"], "value")["Red"]["count"] == 2
    assert by_key(colors["items"], "value")["Blue"]["count"] == 1
    assert result["summary"]["variants"] == 2
    assert result["summary"]["out_of_stock_variants"] == 1
    assert result["summary"]["sku_stockout_pct"] == "50.00"
    assert by_key(result["shops"], "id")["1"]["variant_count"] == 2
    assert result["attribute_distributions"]["styles"]["items"] == []
    matrix = result["color_size_matrix"]
    assert matrix["total_variants"] == matrix["shown_variants"] == 2
    assert matrix["colors"] == ["Red"]
    assert matrix["cells"] == [{"color": "Red", "size": "M", "count": 1, "in_stock": 0, "out_of_stock": 1, "unknown": 0},
                               {"color": "Red", "size": "S", "count": 1, "in_stock": 1, "out_of_stock": 0, "unknown": 0}]


def test_filters_apply_to_all_widgets_and_literal_search(analytics_db):
    result = build_workbench(analytics_db, Settings(), as_of=NOW, shop_id=1, category="Dresses", q="Dress 1")
    assert result["summary"]["products"] == 1
    assert result["summary"]["price_stats"]["median"] == "10.01"
    assert result["history_summary"]["snapshot_count"] == 2
    assert result["summary"]["events_in_window"] == 1
    assert result["attribute_distributions"]["colors"]["total_products"] == 1
    assert len(result["shops"]) == 1
    assert build_workbench(analytics_db, Settings(), as_of=NOW, q="%") ["summary"]["products"] == 0
    assert build_workbench(analytics_db, Settings(), as_of=NOW, q="Alpha")["summary"]["products"] == 2
    assert build_workbench(analytics_db, Settings(), as_of=NOW, q="Brand B")["summary"]["products"] == 1
    assert build_workbench(analytics_db, Settings(), as_of=NOW, currency="eur")["summary"]["products"] == 1


def test_history_events_and_scatter_are_observed_not_inferred(analytics_db):
    result = build_workbench(analytics_db, Settings(), as_of=NOW)
    assert result["history_summary"]["snapshot_count"] == 3
    assert result["history_summary"]["products_with_history"] == 2
    assert result["history_summary"]["products_with_two_observations"] == 1
    assert result["history_summary"]["span_hours"] == "1.00"
    assert result["summary"]["events_in_window"] == 2
    assert result["event_trend_daily"] == [{"date": "2026-10-14", "count": 2, "types": {"price_changed": 1, "product_new": 1}}]
    scatter = result["price_rating_scatter"]
    assert scatter["eligible_count"] == 2
    assert {point["id"] for point in scatter["points"]} == {"1", "2"}
    assert scatter["points"][1]["review_count"] is None
    assert "sales_estimate" not in result["summary"]


def test_empty_scope_does_not_create_fake_trend_or_zero_median(analytics_db):
    result = build_workbench(analytics_db, Settings(), as_of=NOW, shop_id=999)
    assert result["summary"]["products"] == 0
    assert result["summary"]["price_stats"]["median"] is None
    assert result["summary"]["detail_coverage_pct"] is None
    assert result["event_trend_daily"] == []
    assert result["history_summary"]["trend_ready"] is False
    assert result["price_rating_scatter"]["points"] == []


def test_unknown_price_drilldown_and_shanghai_observation_day(analytics_db):
    # The missing-price bar must drill into missing quotes, rather than every price.
    missing = list_analytics_products(analytics_db, Settings(), price_unknown=True)
    assert missing["total"] == 1 and missing["items"][0]["id"] == "4"
    assert list_analytics_products(analytics_db, Settings(), price_unknown=False)["total"] == 3

    # A delayed detector must not move a known observation to its detection day.
    analytics_db.get(ProductSnapshot, 1).captured_at = datetime(2026, 10, 13, 14, 30, tzinfo=timezone.utc)
    for identifier, hour in ((2, 15), (3, 16)):
        analytics_db.get(ProductSnapshot, identifier).captured_at = datetime(2026, 10, 13, hour, 30, tzinfo=timezone.utc)
        analytics_db.get(ChangeEvent, identifier - 1).detected_at = NOW
    analytics_db.commit()
    result = build_workbench(analytics_db, Settings(), as_of=NOW)
    assert result["coverage"]["event_date_timezone"] == "Asia/Shanghai"
    assert result["event_trend_daily"] == [
        {"date": "2026-10-13", "count": 1, "types": {"price_changed": 1}},
        {"date": "2026-10-14", "count": 1, "types": {"product_new": 1}},
    ]
    assert result["summary"]["events_in_window"] == 2


def test_repeated_logical_windows_at_one_observation_time_do_not_enable_a_trend(analytics_db):
    for identifier, offset in [(5, 1), (6, 2)]:
        analytics_db.add(ProductSnapshot(id=identifier, product_id=3, captured_at=NOW,
                         scheduled_for=NOW - timedelta(minutes=offset), currency="USD", pricing_context="public_listed",
                         source_status=200, parser_version="fixture", created_at=NOW))
    analytics_db.commit()
    result = build_workbench(analytics_db, Settings(), as_of=NOW, shop_id=2)
    assert result["history_summary"]["snapshot_count"] == 2
    assert result["history_summary"]["products_with_two_observations"] == 0
    assert result["history_summary"]["trend_ready"] is False


def test_invalid_source_snapshot_cannot_supply_history_or_change_event(analytics_db):
    analytics_db.add(ProductSnapshot(id=5, product_id=2, captured_at=NOW, scheduled_for=NOW,
                     currency="USD", pricing_context="public_listed", source_status=403, parser_version="fixture", created_at=NOW))
    analytics_db.flush()
    analytics_db.add(ChangeEvent(id=4, product_id=2, snapshot_id=5, previous_snapshot_id=3,
                     event_type="price_changed", event_hash="d" * 64, detected_at=NOW, created_at=NOW))
    analytics_db.commit()
    result = build_workbench(analytics_db, Settings(), as_of=NOW)
    assert result["history_summary"]["snapshot_count"] == 3
    assert result["summary"]["events_in_window"] == 2


def test_product_pagination_filter_sort_and_field_denominator(analytics_db):
    page1 = list_analytics_products(analytics_db, Settings(), page=1, page_size=2, sort="price_desc")
    page2 = list_analytics_products(analytics_db, Settings(), page=2, page_size=2, sort="price_desc")
    assert page1["total"] == page2["total"] == 4
    assert [row["id"] for row in page1["items"]] == ["3", "2"]
    assert [row["id"] for row in page2["items"]] == ["1", "4"]
    assert page2["items"][0]["variant_count"] == 2
    assert page2["items"][1]["price_kind"] == "unknown"
    assert by_key(page1["field_coverage"], "field")["price"]["total_count"] == 4
    filtered = list_analytics_products(analytics_db, Settings(), price_min=Decimal("15"), price_max=Decimal("25"), stock="out_of_stock")
    assert filtered["total"] == 1 and filtered["items"][0]["id"] == "2"
    assert filtered["items"][0]["price_min"] == "20.01"
    assert filtered["items"][0]["price_kind"] == "range"
    color = list_analytics_products(analytics_db, Settings(), attribute="color", value="Red")
    assert color["total"] == 2
    assert list_analytics_products(analytics_db, Settings(), attribute="size", value="L")["total"] == 1
    assert list_analytics_products(analytics_db, Settings(), attribute="material", value="Cotton")["total"] == 1
    assert list_analytics_products(analytics_db, Settings(), price_max=Decimal("20.01"), price_upper_exclusive=True)["total"] == 1


def test_routes_validate_and_execute_only_reads(analytics_db, analytics_engine):
    app = FastAPI()
    app.include_router(router)
    app.state.settings = Settings()
    app.state.factory = sessionmaker(analytics_engine)
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(analytics_engine, "before_cursor_execute", capture)
    try:
        with TestClient(app) as client:
            for path in ["/workbench?lookback_hours=0", "/workbench?lookback_hours=721", "/products?page_size=101",
                         "/products?sort=sales", "/products?price_min=20&price_max=10", "/workbench?currency=USDT",
                         "/products?price_min=NaN", "/products?price_min=1.001", "/products?attribute=color", "/products?value=Red"]:
                assert client.get("/api/v1/analytics" + path).status_code == 422
            response = client.get("/api/v1/analytics/products?page_size=1&sort=price_asc&q=Dress")
            assert response.status_code == 200
            assert response.json()["items"][0]["price_min"] == "10.01"
            assert response.json()["total"] == 4
            response = client.get("/api/v1/analytics/workbench?shop_id=1")
            assert response.status_code == 200
            assert response.json()["summary"]["products"] == 2
        assert statements and all(statement.lstrip().upper().startswith("SELECT") or
                                  (statement.lstrip().upper().startswith("PRAGMA") and "table_info" in statement)
                                  for statement in statements)
    finally:
        event.remove(analytics_engine, "before_cursor_execute", capture)


def test_aggregation_is_full_scope_above_old_report_limit_and_scatter_is_explicitly_bounded(analytics_db):
    rows = [{"id": identifier, "shop_id": 1, "platform": "tiktok_shop", "region": "US",
             "platform_product_id": f"large-{identifier}", "product_url": "https://shop.tiktok.com/p/sample",
             "title": "Bulk item", "currency": "USD", "pricing_context": "public_listed",
             "price_min": Decimal("40.01"), "rating": Decimal("4.00"), "data_version": 1,
             "stock_status": "unknown", "availability_status": "unknown",
             "created_at": NOW, "updated_at": NOW, "first_seen_at": NOW, "last_seen_at": NOW}
            for identifier in range(100, 20101)]
    analytics_db.execute(Product.__table__.insert(), rows)
    analytics_db.commit()
    result = build_workbench(analytics_db, Settings(), as_of=NOW)
    assert result["summary"]["products"] == 20005
    assert result["summary"]["price_stats"]["known_count"] == 20004
    assert result["summary"]["price_stats"]["median"] == "40.01"
    assert result["price_rating_scatter"]["eligible_count"] == 20003
    assert result["price_rating_scatter"]["returned_count"] == 300
    assert result["price_rating_scatter"]["truncated"] is True
    assert list_analytics_products(analytics_db, Settings(), page_size=1)["total"] == 20005


def test_real_cover_images_are_batched_scoped_and_counted_once(analytics_db):
    records = [(1, 1, "listing", 0, True), (2, 1, "detail", 5, True),
               (3, 2, "detail", 0, False), (4, 5, "detail", 0, True)]
    for identifier, product_id, source, position, current in records:
        analytics_db.add(ProductImage(id=identifier, product_id=product_id, image_key=str(identifier) * 64,
                         image_url=f"https://images.example/{identifier}.jpg", thumbnail_url=f"https://images.example/{identifier}-thumb.jpg",
                         source_url="https://shop.tiktok.com/p/fixture", source_kind=source, source_version="fixture",
                         role="cover", position=position, is_current=current, observed_at=NOW))
    analytics_db.commit()
    result = list_analytics_products(analytics_db, Settings(), page_size=4, sort="title")
    assert result["image_readiness"] is True
    products = by_key(result["items"], "id")
    assert products["1"]["image_url"] == "https://images.example/2.jpg"
    assert products["1"]["thumbnail_url"] == "https://images.example/2-thumb.jpg"
    assert products["1"]["image_observed_at"] == NOW.isoformat()
    assert products["2"]["image_url"] is None
    assert by_key(result["field_coverage"], "field")["image"]["known_count"] == 1
    workbench = build_workbench(analytics_db, Settings(), as_of=NOW)
    assert workbench["summary"]["image_known_products"] == 1
    assert workbench["summary"]["image_coverage_pct"] == "25.00"
    assert workbench["collection_quality"]["image_coverage"]["ready"] is True
    assert build_workbench(analytics_db, Settings(), as_of=NOW, currency="EUR")["summary"]["image_known_products"] == 1


def test_unmigrated_image_table_is_unknown_instead_of_zero_or_an_error(analytics_db):
    analytics_db.execute(DropTable(ProductImage.__table__))
    analytics_db.commit()
    result = list_analytics_products(analytics_db, Settings(), page_size=1)
    assert result["image_readiness"] is False
    assert result["items"][0]["image_url"] is None
    coverage = by_key(result["field_coverage"], "field")["image"]
    assert coverage["known_count"] is None and coverage["coverage_pct"] is None
    workbench = build_workbench(analytics_db, Settings(), as_of=NOW)
    assert workbench["summary"]["image_known_products"] is None
    assert workbench["coverage"]["image_readiness"] is False


@pytest.mark.parametrize(("sort", "expected"), [
    ("title_desc", ["4", "3", "2", "1"]), ("shop_asc", ["1", "2", "3", "4"]),
    ("shop_desc", ["3", "4", "1", "2"]), ("stock_asc", ["1", "2", "3", "4"]),
    ("stock_desc", ["2", "1", "3", "4"]), ("sku_count_asc", ["2", "3", "1", "4"]),
    ("sku_count_desc", ["1", "2", "3", "4"]),
])
def test_server_sorts_with_stable_pagination_and_unknown_details_last(analytics_db, sort, expected):
    first = list_analytics_products(analytics_db, Settings(), sort=sort, page_size=2, page=1)
    second = list_analytics_products(analytics_db, Settings(), sort=sort, page_size=2, page=2)
    assert [row["id"] for row in first["items"] + second["items"]] == expected
    assert by_key(first["items"] + second["items"], "id")["4"]["variant_count"] is None
