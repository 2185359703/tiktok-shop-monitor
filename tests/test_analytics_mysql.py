import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.models import Product, ProductSnapshot, ProductVariant, Shop
from app.services.analytics_service import (
    aggregate_current_variants, build_workbench, color_size_matrix, filters,
    matrix_from_counts, observed_variants_query, shop_variant_aggregation, workbench_variant_query,
)
from app.services.product_detail import persist_product_detail
from app.settings import Settings
from test_product_detail import snapshot
from test_product_detail_storage import initial


def test_mysql_observed_sku_join_preserves_set_identity_and_ignores_numeric_impostors(session):
    product, task = initial(session)
    detail = snapshot()
    persist_product_detail(session, task, detail)
    first, second = [variant.platform_variant_id for variant in detail.detail.variants]
    product.raw_attributes = {**product.raw_attributes, "observed_sku_ids": [first, first, second, int(first)]}
    session.flush()
    clauses, scope = filters(Settings(), session=session)
    count = session.scalar(observed_variants_query(session, clauses, scope["currency"], func.count()))
    assert count == 2
    product.raw_attributes = {**product.raw_attributes, "observed_sku_ids": [int(first)]}
    session.flush()
    assert session.scalar(observed_variants_query(session, clauses, scope["currency"], func.count(ProductVariant.id))) == 0


def test_workbench_reuses_the_callers_uncommitted_repeatable_read_snapshot(mysql_engine):
    with mysql_engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        transaction = connection.begin()
        try:
            with Session(connection, join_transaction_mode="create_savepoint", expire_on_commit=False) as reader:
                product, task = initial(reader)
                page = snapshot()
                persist_product_detail(reader, task, page)
                reader.flush()
                result = build_workbench(reader, Settings(), as_of=page.captured_at)
                assert result["summary"]["products"] == 1 and result["summary"]["variants"] == 2
                assert transaction.is_active and connection.get_isolation_level() == "REPEATABLE READ"
        finally:
            transaction.rollback()
    with mysql_engine.connect() as independent:
        assert independent.scalar(select(func.count()).select_from(Product)) == 0


@pytest.fixture
def analytics_mysql_reader(mysql_engine):
    """Same disposable database as conftest, with an explicit RC transaction."""
    with mysql_engine.connect().execution_options(isolation_level="READ COMMITTED") as connection:
        transaction = connection.begin()
        with Session(connection, join_transaction_mode="create_savepoint", expire_on_commit=False) as session:
            try:
                yield session
            finally:
                session.close()
                transaction.rollback()


def test_mysql_batched_projection_matches_exact_typed_string_membership(analytics_mysql_reader):
    session = analytics_mysql_reader
    product, task = initial(session)
    page = snapshot()
    persist_product_detail(session, task, page)
    first, second = [variant.platform_variant_id for variant in page.detail.variants]
    clauses, scope = filters(Settings(), session=session)
    for identifiers, expected in (([first, first, second, int(first)], 2),
                                  ([int(first), None, False, first + " ", first + "x" * 129], 0),
                                  ([second], 1), ([], 0), (second, 1), (None, 0), ({"id": first}, 0),
                                  ([[first], {"id": second}], 0)):
        product.raw_attributes = {**product.raw_attributes, "observed_sku_ids": identifiers}
        session.flush()
        shops, counts = aggregate_current_variants(session, clauses, scope["currency"], batch_size=1)
        assert sum(values["total"] for values in shops.values()) == expected
        with workbench_variant_query(session, clauses, scope["currency"]) as query:
            rows = session.execute(shop_variant_aggregation(query)).all()
            assert sum(row[1] for row in rows) == expected
            assert matrix_from_counts(counts, expected) == color_size_matrix(session, query, expected)


def test_mysql_matrix_preserves_case_and_accents_regardless_of_default_collation(analytics_mysql_reader):
    session = analytics_mysql_reader
    product, task = initial(session)
    page = snapshot()
    persist_product_detail(session, task, page)
    originals = session.scalars(select(ProductVariant).where(ProductVariant.product_id == product.id)
        .order_by(ProductVariant.id)).all()
    originals[0].color, originals[0].size = " Black ", " M "
    originals[0].stock_status = "partial"
    originals[1].color, originals[1].size = "black", "M"
    identifiers = [variant.platform_variant_id for variant in originals]
    for offset, label in enumerate(("Café", "Cafe", "\t", "  "), start=1):
        identifier = f"label-{offset}"
        identifiers.append(identifier)
        session.add(ProductVariant(product_id=product.id, platform_variant_id=identifier,
            variant_key=f"{offset:064x}", currency="USD", color=label, size="L", stock_status="unknown"))
    product.raw_attributes = {**product.raw_attributes, "observed_sku_ids": identifiers}
    session.flush()
    clauses, scope = filters(Settings(), session=session)
    shops, counts = aggregate_current_variants(session, clauses, scope["currency"], batch_size=1)
    assert sum(values["total"] for values in shops.values()) == 6
    matrix = matrix_from_counts(counts, 6)
    assert matrix["eligible_variants"] == 5 and matrix["missing_attributes_variants"] == 1
    assert set(matrix["colors"]) == {"Black", "black", "Café", "Cafe", "\t"}
    with workbench_variant_query(session, clauses, scope["currency"]) as query:
        assert color_size_matrix(session, query, 6) == matrix


def test_mysql_workbench_projects_skus_without_business_writes(analytics_mysql_reader):
    session = analytics_mysql_reader
    product, task = initial(session)
    page = snapshot()
    persist_product_detail(session, task, page)
    before = (product.content_hash, product.data_version, product.last_success_at, product.raw_attributes)
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(session.bind, "before_cursor_execute", capture)
    try:
        result = build_workbench(session, Settings(), as_of=page.captured_at)
    finally:
        event.remove(session.bind, "before_cursor_execute", capture)
    assert result["summary"]["variants"] == 2
    assert result["color_size_matrix"]["eligible_variants"] == 2
    assert not any("product_variants" in statement.lower() and "json_" in statement.lower()
                   for statement in statements)
    assert not any(statement.lstrip().upper().startswith(("CREATE", "DROP", "INSERT", "UPDATE", "DELETE"))
                   for statement in statements)
    assert session.scalar(select(func.count()).select_from(ProductSnapshot)) == 0
    assert (product.content_hash, product.data_version, product.last_success_at, product.raw_attributes) == before
    assert session.bind.get_isolation_level() == "READ COMMITTED"


def test_mysql_selective_high_id_shop_has_bounded_read_work(analytics_mysql_reader):
    session = analytics_mysql_reader
    original, _ = initial(session)
    shop = Shop(region="US", platform_shop_id="high-id-scope", canonical_name="Scope fixture")
    session.add(shop)
    session.flush()
    session.execute(Product.__table__.insert(), [{"id": identifier, "shop_id": original.shop_id,
        "platform": "tiktok_shop", "region": "US", "platform_product_id": f"unrelated-{identifier}",
        "title": "Unrelated", "product_url": "https://shop.tiktok.com/p/fixture"}
        for identifier in range(1000, 1600)])
    session.execute(Product.__table__.insert(), [{"id": 999999, "shop_id": shop.id, "platform": "tiktok_shop", "region": "US",
        "platform_product_id": "late-scope", "product_url": "https://shop.tiktok.com/p/fixture",
        "raw_attributes": {"observed_sku_ids": ["scope-sku"]}}])
    session.add(ProductVariant(product_id=999999, platform_variant_id="scope-sku", variant_key="f" * 64,
        currency="USD", stock_status="in_stock", color="Black", size="M"))
    session.flush()
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)
    event.listen(session.bind, "before_cursor_execute", capture)
    try:
        clauses, scope = filters(Settings(), session=session, shop_id=shop.id)
        shops, _ = aggregate_current_variants(session, clauses, scope["currency"])
    finally:
        event.remove(session.bind, "before_cursor_execute", capture)
    assert dict(shops[shop.id]) == {"total": 1, "known": 1, "out_of_stock": 0}
    assert len(statements) <= 6


def test_mysql_batched_projection_remains_complete_above_multiple_product_batches(analytics_mysql_reader):
    """Set TIKTOK_ANALYTICS_SCALE_PRODUCTS=18000 for a 612k-current-SKU run.

    The default remains a multi-batch correctness check. All records live in
    the disposable test database; measurement excludes fixture construction.
    """
    session = analytics_mysql_reader
    original, _ = initial(session)
    count = int(os.environ.get("TIKTOK_ANALYTICS_SCALE_PRODUCTS", "257"))
    if not 257 <= count <= 50000:
        raise ValueError("Analytics scale verification requires 257-50000 synthetic test products.")
    now = datetime.now(timezone.utc)
    statuses = ("in_stock", "out_of_stock", "unknown", "partial")
    for start in range(0, count, 128):
        products, variants = [], []
        for offset in range(start, min(start + 128, count)):
            identifier = 10000 + offset
            ids = [f"scale-{identifier}-{index}" for index in range(34)]
            products.append({"id": identifier, "shop_id": original.shop_id, "platform": "tiktok_shop", "region": "US",
                "platform_product_id": f"scale-{identifier}", "product_url": "https://shop.tiktok.com/p/fixture",
                "title": "Synthetic analytics fixture", "currency": "USD", "pricing_context": "public_listed",
                "price_min": Decimal("24.99"), "price_max": Decimal("24.99"), "rating": Decimal("4.50"),
                "original_category": "Tops", "color_summary": ["Color 00", "Color 01"], "size_summary": ["Size 00", "Size 01"],
                "raw_attributes": {"observed_sku_ids": [*ids, ids[0], identifier, "other-currency"],
                                   "unused_evidence": "x" * 2048}, "last_success_at": now})
            for index, sku in enumerate([*ids, "retained", str(identifier), "other-currency"]):
                variants.append({"product_id": identifier, "platform_variant_id": sku,
                    "variant_key": f"{index:064x}", "currency": "EUR" if sku == "other-currency" else "USD",
                    "color": f"Color {index % 14:02d}", "size": f"Size {index % 11:02d}",
                    "stock_status": statuses[index % 4]})
        session.execute(Product.__table__.insert(), products)
        session.execute(ProductVariant.__table__.insert(), variants)
    session.flush()
    clauses, scope = filters(Settings(), session=session)
    try:
        import psutil
        process = psutil.Process()
    except ImportError:
        process = None
    rss_before = process.memory_info().rss / 1024**2 if process is not None else None
    begun = time.perf_counter()
    shops, cells = aggregate_current_variants(session, clauses, scope["currency"])
    elapsed = time.perf_counter() - begun
    assert sum(values["total"] for values in shops.values()) == count * 34
    assert sum(values["known"] for values in shops.values()) == count * 18
    assert sum(values["out_of_stock"] for values in shops.values()) == count * 9
    matrix = matrix_from_counts(cells, count * 34)
    assert matrix["eligible_variants"] == count * 34 and matrix["missing_attributes_variants"] == 0
    assert len(matrix["colors"]) == 12 and len(matrix["sizes"]) == 10 and matrix["truncated"]
    begun = time.perf_counter()
    workbench = build_workbench(session, Settings(), as_of=now)
    workbench_elapsed = time.perf_counter() - begun
    assert workbench["summary"]["products"] == count + 1
    assert workbench["summary"]["variants"] == count * 34
    assert workbench["summary"]["price_stats"]["median"] == "24.99"
    memory = process.memory_info() if process is not None else None
    print("ANALYTICS_LOCAL_MYSQL " + json.dumps({"synthetic_products": count, "current_skus": count * 34,
        "stored_skus": count * 37, "projection_seconds": round(elapsed, 4),
        "workbench_seconds": round(workbench_elapsed, 4), "rss_before_read_mib": rss_before,
        "rss_after_read_mib": memory.rss / 1024**2 if memory is not None else None,
        "process_peak_rss_mib": memory.peak_wset / 1024**2 if memory is not None and hasattr(memory, "peak_wset") else None},
        sort_keys=True))
