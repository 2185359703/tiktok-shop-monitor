"""Meaningful analytics checks use only a disposable in-memory database."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, create_engine, event, func
from sqlalchemy.dialects.mysql import dialect as mysql_dialect
from sqlalchemy.schema import CreateTable, DropTable
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

from app.api.routes_analytics import router
from app.models import Base, ChangeEvent, Product, ProductSnapshot, ProductVariant, Shop
from app.models.product_image import ProductImage
from app.services.analytics_service import (
    build_workbench, list_analytics_products, shop_variant_aggregation,
    filters, iter_workbench_projection_rows, workbench_projection_source, workbench_variant_query,
)
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


@pytest.mark.parametrize("observed", [False, True])
def test_sku_summary_keeps_empty_sets_and_unknown_stock_explicit(analytics_db, observed):
    product = analytics_db.get(Product, 1)
    if not observed:
        product.raw_attributes = {"observed_sku_ids": []}
    else:
        for identifier in (1, 2):
            variant = analytics_db.get(ProductVariant, identifier)
            variant.stock_status = "unknown" if identifier == 1 else "partial"
            variant.color = variant.size = None
    analytics_db.commit()
    result = build_workbench(analytics_db, Settings(), as_of=NOW)
    total = 2 if observed else 0
    assert result["summary"]["variants"] == total
    assert result["summary"]["known_stock_variants"] == 0
    assert result["summary"]["out_of_stock_variants"] == 0
    assert result["summary"]["sku_stockout_pct"] is None
    matrix = result["color_size_matrix"]
    assert matrix["total_variants"] == matrix["missing_attributes_variants"] == total
    assert matrix["eligible_variants"] == matrix["shown_variants"] == 0
    assert matrix["colors"] == matrix["sizes"] == matrix["cells"] == []


def test_global_sku_totals_include_shops_beyond_display_limit(analytics_db, monkeypatch):
    monkeypatch.setattr("app.services.analytics_service.SHOP_LIMIT", 1)
    analytics_db.get(Product, 3).raw_attributes = {"observed_sku_ids": ["beta-sku", "beta-sku"]}
    analytics_db.add(ProductVariant(id=10, product_id=3, platform_variant_id="beta-sku", variant_key="a" * 64,
                     currency="USD", stock_status="out_of_stock", price=Decimal("30.01"), color="Blue", size="L",
                     created_at=NOW, updated_at=NOW))
    analytics_db.commit()
    result = build_workbench(analytics_db, Settings(), as_of=NOW)
    assert result["coverage"]["shops_truncated"] and len(result["shops"]) == 1
    assert result["shops"][0]["id"] == "1" and result["shops"][0]["variant_count"] == 2
    assert result["summary"]["variants"] == result["summary"]["known_stock_variants"] == 3
    assert result["summary"]["out_of_stock_variants"] == 2
    assert result["summary"]["sku_stockout_pct"] == "66.67"
    assert result["color_size_matrix"]["total_variants"] == 3


def test_matrix_eligible_count_precedes_color_and_size_display_limits(analytics_db):
    combinations = [(f"Color {color:02d}", f"Size {size:02d}") for color in range(14) for size in range(11)]
    combinations += [(None, "Size 00"), ("Color 00", None)]
    sku_ids = []
    for identifier, (color, size) in enumerate(combinations, start=100):
        sku_id = f"matrix-{identifier}"
        sku_ids.append(sku_id)
        analytics_db.add(ProductVariant(id=identifier, product_id=1, platform_variant_id=sku_id,
                         variant_key=f"{identifier:064x}", currency="USD", stock_status="unknown",
                         color=color, size=size, created_at=NOW, updated_at=NOW))
    analytics_db.get(Product, 1).raw_attributes = {"observed_sku_ids": [*sku_ids, sku_ids[0]]}
    analytics_db.commit()
    result = build_workbench(analytics_db, Settings(), as_of=NOW)
    matrix = result["color_size_matrix"]
    assert result["summary"]["variants"] == matrix["total_variants"] == 156
    assert matrix["eligible_variants"] == 154 and matrix["missing_attributes_variants"] == 2
    assert len(matrix["colors"]) == 12 and len(matrix["sizes"]) == 10
    assert matrix["shown_variants"] == len(matrix["cells"]) == 120
    assert matrix["truncated"] is True
    assert all(cell["unknown"] == cell["count"] == 1 for cell in matrix["cells"])


@pytest.mark.parametrize("failure_at", [None, "consumer", "create", "drop"])
def test_mysql_temp_projection_is_connection_owned_and_cleaned_on_failures(failure_at, monkeypatch):
    statements = []
    batches = [[{"variant_id": 1, "shop_id": 2, "color": "Red", "size": "S", "stock_status": "unknown"}]]
    monkeypatch.setattr("app.services.analytics_service.iter_workbench_projection_rows", lambda *args: iter(batches))
    class BoundConnection:
        invalidated = False

        def get_isolation_level(self):
            return "READ COMMITTED"

        def execute(self, statement, parameters=None):
            statements.append(statement)
            if isinstance(statement, CreateTable) and failure_at == "create":
                raise RuntimeError("create interrupted")

        def exec_driver_sql(self, statement):
            statements.append(statement)
            if failure_at == "drop":
                raise RuntimeError("cleanup interrupted")

        def invalidate(self):
            self.invalidated = True

    connection = BoundConnection()
    session = SimpleNamespace(bind=SimpleNamespace(dialect=SimpleNamespace(name="mysql")),
                              connection=lambda: connection)
    def consume():
        with workbench_variant_query(session, [], "USD") as query:
            projected_sql = str(query.compile(dialect=mysql_dialect())).lower()
            assert "json_table" not in projected_sql and " join " not in projected_sql
            assert set(query.selected_columns.keys()) == {"variant_id", "shop_id", "color", "size", "stock_status"}
            aggregate_sql = str(shop_variant_aggregation(query).compile(dialect=mysql_dialect())).lower()
            assert " join " not in aggregate_sql and "product_variants" not in aggregate_sql and "products" not in aggregate_sql
            count_sql = str(query.with_only_columns(func.count()).compile(dialect=mysql_dialect())).lower()
            assert "from workbench_skus_" in count_sql
            if failure_at == "consumer":
                raise RuntimeError("consumer failed")
    if failure_at:
        with pytest.raises(RuntimeError):
            consume()
    else:
        consume()
    assert isinstance(statements[0], CreateTable)
    create_sql = str(statements[0].compile(dialect=mysql_dialect())).upper()
    assert "CREATE TEMPORARY TABLE" in create_sql and "ENGINE=INNODB" in create_sql
    assert connection.invalidated is (failure_at in {"create", "drop"})
    if failure_at != "create":
        assert statements[-1].startswith("DROP TEMPORARY TABLE `workbench_skus_")
        fill_sql = str(statements[1].compile(dialect=mysql_dialect(), compile_kwargs={"literal_binds": True})).upper()
        assert "INSERT IGNORE" in fill_sql and "VALUES" in fill_sql
        assert "SELECT" not in fill_sql and "JSON_TABLE" not in fill_sql


def test_projection_source_explain_controls_keep_the_same_fields_and_predicates():
    forced = workbench_projection_source([Product.shop_id == 17], "USD", force_variant_index=True)
    automatic = workbench_projection_source([Product.shop_id == 17], "USD", force_variant_index=False)
    def compiled(query):
        return str(query.compile(dialect=mysql_dialect(), compile_kwargs={"literal_binds": True}))
    forced_sql, automatic_sql = compiled(forced), compiled(automatic)
    assert forced_sql.replace(" FORCE INDEX (uq_variants_platform_id)", "") == automatic_sql
    assert "products.shop_id = 17" in forced_sql and "JSON_TYPE" in forced_sql.upper()
    assert "EXPLAIN FORMAT=JSON SELECT STRAIGHT_JOIN" in "EXPLAIN FORMAT=JSON " + forced_sql
    assert list(forced.selected_columns.keys()) == ["variant_id", "shop_id", "color", "size", "stock_status"]


def test_bounded_projection_pages_products_and_skus_without_buffering_catalog(analytics_db):
    analytics_db.get(Product, 1).raw_attributes = {"observed_sku_ids": ["sku1", "sku1", "sku2", 1, True]}
    analytics_db.get(Product, 2).raw_attributes = {"observed_sku_ids": "sku-a"}
    analytics_db.get(Product, 3).raw_attributes = {"observed_sku_ids": ["sku-9", "sku-9"]}
    for identifier, product_id, sku in ((10, 2, "sku-a"), (11, 2, "sku-b"), (12, 2, "sku-z"),
                                        (20, 3, "sku-0"), (21, 3, "sku-9")):
        analytics_db.add(ProductVariant(id=identifier, product_id=product_id, platform_variant_id=sku,
                         variant_key=f"{identifier:064x}", currency="USD", stock_status="unknown",
                         color=" Red ", size="S", created_at=NOW, updated_at=NOW))
    analytics_db.commit()
    observed_sizes = []
    class BoundedReader:
        bind = analytics_db.bind

        def execute(self, query):
            rows = analytics_db.execute(query).all()
            observed_sizes.append(len(rows))
            return SimpleNamespace(all=lambda: rows)
    clauses, scope = filters(Settings(), session=analytics_db)
    batches = list(iter_workbench_projection_rows(BoundedReader(), clauses, scope["currency"],
                                                 product_batch_size=1, sku_batch_size=1))
    assert {row["variant_id"] for batch in batches for row in batch} == {1, 2, 10, 21}
    assert all(len(batch) <= 1 for batch in batches) and max(observed_sizes) == 1
    assert next(row for batch in batches for row in batch if row["variant_id"] == 10)["color"] == " Red "


@pytest.mark.parametrize("boundary", ["items", "bytes"])
def test_projection_rejects_oversized_json_without_truncated_statistics(analytics_db, monkeypatch, boundary):
    if boundary == "items":
        monkeypatch.setattr("app.services.analytics_service.WORKBENCH_OBSERVED_ID_LIMIT", 1)
    else:
        monkeypatch.setattr("app.services.analytics_service.WORKBENCH_OBSERVED_BYTES_LIMIT", 1)
    clauses, scope = filters(Settings(), session=analytics_db)
    with pytest.raises(ValueError, match="未截断统计"):
        list(iter_workbench_projection_rows(analytics_db, clauses, scope["currency"]))
