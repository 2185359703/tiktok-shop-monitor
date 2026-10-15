from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.models import (
    Base, ChangeEvent, ChangeEventType, CrawlAttempt, CrawlTask, Product,
    ProductSnapshot, ProductVariant, SeedShopObservation, Shop, ShopSeedLink,
    TaskStatus, VariantSnapshot,
)
from app.storage.schema import mysql_comments_ddl, pending_comment_statements, schema_drift, sync_comments
from app.storage.database import make_session_factory, session_scope


NOW = datetime(2026, 10, 13, 10, 0, 0, 123456, tzinfo=timezone.utc)


def fingerprint(value):
    return sha256(value.encode()).hexdigest()


def make_shop(session, identifier="shop-1", region="US"):
    shop = Shop(
        region=region, platform_shop_id=identifier, canonical_name="Test clothing shop"
    )
    session.add(shop)
    session.flush()
    return shop


def make_product(session, identifier="product-1", shop=None):
    shop = shop or make_shop(session)
    product = Product(
        shop=shop, platform_product_id=identifier,
        product_url="https://example.test/product/" + identifier,
        title="Cotton T-shirt", price_min=Decimal("19.99"), price_max=Decimal("24.99"),
    )
    session.add(product)
    session.flush()
    return product


def make_snapshot(session, product, scheduled=NOW, context="public_listed"):
    snapshot = ProductSnapshot(
        product=product, scheduled_for=scheduled, captured_at=scheduled + timedelta(seconds=2),
        pricing_context=context, price_min=Decimal("19.99"), price_max=Decimal("24.99"),
        parser_version="1",
    )
    session.add(snapshot)
    session.flush()
    return snapshot


def assert_rejected(session, instance):
    with pytest.raises(DBAPIError) as rejected:
        with session.begin_nested():
            session.add(instance)
            session.flush()
    # PyMySQL categorizes CHECK violations as OperationalError, not IntegrityError.
    assert rejected.value.orig.args[0] in {1062, 1452, 3819}


def test_generated_sql_matches_mysql_structure(mysql_engine):
    assert schema_drift(mysql_engine) == []
    inspector = inspect(mysql_engine)
    assert set(inspector.get_table_names()) == set(Base.metadata.tables)
    for table in Base.metadata.sorted_tables:
        assert inspector.get_table_comment(table.name)["text"] == table.comment
        actual_columns = {column["name"]: column for column in inspector.get_columns(table.name)}
        assert all(actual_columns[column.name]["comment"] == column.comment for column in table.c)
        actual_unique = {tuple(item["column_names"]) for item in inspector.get_unique_constraints(table.name)}
        expected_unique = {
            tuple(column.name for column in constraint.columns)
            for constraint in table.constraints
            if constraint.__class__.__name__ == "UniqueConstraint"
        }
        assert expected_unique <= actual_unique, table.name
        actual_indexes = {item["name"] for item in inspector.get_indexes(table.name)}
        assert {index.name for index in table.indexes} <= actual_indexes, table.name
        actual_checks = {item["name"] for item in inspector.get_check_constraints(table.name)}
        expected_checks = {
            constraint.name for constraint in table.constraints
            if constraint.__class__.__name__ == "CheckConstraint"
        }
        assert expected_checks <= actual_checks, table.name


def test_offline_comment_migration_runs_on_mysql(mysql_engine):
    with mysql_engine.connect() as connection:
        for statement in mysql_comments_ddl(connection.engine.url.database).split(";"):
            statement = "\n".join(
                line for line in statement.splitlines() if not line.lstrip().startswith("--")
            ).strip()
            if statement:
                connection.exec_driver_sql(statement)
    assert schema_drift(mysql_engine) == []
    assert pending_comment_statements(mysql_engine) == []


def test_comment_sync_preserves_live_default_and_existing_data(mysql_engine):
    with mysql_engine.begin() as connection:
        connection.execute(insert(Shop).values(
            region="US", platform_shop_id="comment-test-record", canonical_name="Existing data"
        ))
    try:
        with mysql_engine.connect() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE shops COMMENT='Old table comment', "
                "MODIFY COLUMN canonical_name VARCHAR(255) NOT NULL "
                "DEFAULT 'Default COMMENT text' COMMENT 'Old column comment'"
            )
        assert sync_comments(mysql_engine) == 1
        assert sync_comments(mysql_engine) == 0
        with mysql_engine.connect() as connection:
            definition = connection.exec_driver_sql("SHOW CREATE TABLE shops").one()[1]
            assert "DEFAULT 'Default COMMENT text'" in definition
            assert connection.scalar(text(
                "SELECT canonical_name FROM shops WHERE platform_shop_id='comment-test-record'"
            )) == "Existing data"
        assert inspect(mysql_engine).get_table_comment("shops")["text"] == Shop.__table__.comment
        columns = {item["name"]: item for item in inspect(mysql_engine).get_columns("shops")}
        assert columns["canonical_name"]["comment"] == Shop.__table__.c.canonical_name.comment
    finally:
        with mysql_engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE shops ALTER COLUMN canonical_name DROP DEFAULT"
            )
            connection.exec_driver_sql(
                "DELETE FROM shops WHERE platform_shop_id='comment-test-record'"
            )


def test_create_all_is_repeatable_and_preserves_data(session):
    shop = make_shop(session)
    # DDL already exists: create_all must only inspect, not alter or recreate.
    Base.metadata.create_all(session.connection(), checkfirst=True)
    assert session.get(Shop, shop.id).canonical_name == "Test clothing shop"


def test_opaque_ids_and_market_uniqueness(session):
    first = make_shop(session, "00001")
    assert first.platform_shop_id == "00001"
    assert_rejected(session, Shop(region="US", platform_shop_id="00001", canonical_name="Duplicate"))
    make_shop(session, "00001", "GB")
    make_shop(session, "ShopABC")
    make_shop(session, "shopabc")


def test_same_name_does_not_merge_distinct_shops(session):
    first = make_shop(session, "one")
    second = make_shop(session, "two")
    assert first.canonical_name == second.canonical_name
    assert first.id != second.id


def test_seed_observation_and_resolution_link(session):
    seed = SeedShopObservation(
        region="US", category="women_clothing", seed_value="Example clothing",
        source_url="https://example.test/category", observed_at=date(2026, 9, 25),
        row_hash=fingerprint("seed-1"),
    )
    session.add(seed)
    session.flush()
    assert seed.shop_url is None
    assert_rejected(session, SeedShopObservation(
        region="US", category="women_clothing", seed_value="Example clothing",
        source_url="https://example.test/category", observed_at=date(2026, 9, 25),
        row_hash=fingerprint("seed-1"),
    ))
    gb_shop = make_shop(session, "gb-shop", "GB")
    assert_rejected(session, ShopSeedLink(
        region="US", seed_id=seed.id, shop_id=gb_shop.id,
        resolved_at=NOW, resolution_method="source_id",
    ))
    shop = make_shop(session)
    link = ShopSeedLink(
        region="US", seed=seed, shop=shop, resolved_at=NOW, resolution_method="source_id"
    )
    session.add(link)
    session.flush()
    assert link.seed_id == seed.id
    assert link.shop_id == shop.id
    assert_rejected(session, ShopSeedLink(
        region="US", seed_id=seed.id, shop_id=shop.id,
        resolved_at=NOW, resolution_method="manual",
    ))


def test_product_identity_and_shop_market_must_match(session):
    shop = make_shop(session)
    product = make_product(session, shop=shop)
    assert product.region == "US"
    assert_rejected(session, Product(
        platform="tiktok_shop", region="GB", shop_id=shop.id,
        platform_product_id="wrong-market", product_url="https://example.test/x",
    ))
    assert_rejected(session, Product(
        region="US", shop_id=shop.id, platform_product_id=product.platform_product_id,
        product_url="https://example.test/duplicate",
    ))


@pytest.mark.parametrize("minimum,maximum", [("-0.01", "2.00"), ("30.00", "20.00")])
def test_invalid_price_range_is_rejected(session, minimum, maximum):
    shop = make_shop(session)
    assert_rejected(session, Product(
        platform=shop.platform, region=shop.region, shop_id=shop.id,
        platform_product_id="invalid-price", product_url="https://example.test/x",
        price_min=Decimal(minimum), price_max=Decimal(maximum),
    ))


def test_money_unknown_fields_and_utf8_roundtrip(session):
    product = make_product(session)
    product.title = "棉质T恤 👚"
    product.color_summary = ["蓝色", "Black"]
    product.raw_attributes = {"size": "M", "material": "100% cotton"}
    session.flush()
    session.expire(product)
    assert product.price_min == Decimal("19.99")
    assert product.sales_count is None
    assert product.review_count is None
    assert product.title == "棉质T恤 👚"
    assert product.color_summary == ["蓝色", "Black"]
    assert product.raw_attributes["size"] == "M"


def test_snapshot_duplicate_retry_and_history(session):
    product = make_product(session)
    make_snapshot(session, product)
    assert_rejected(session, ProductSnapshot(
        product_id=product.id, scheduled_for=NOW, captured_at=NOW + timedelta(seconds=20),
        parser_version="1",
    ))
    # The same price still deserves a later successful observation.
    make_snapshot(session, product, NOW + timedelta(days=1))
    make_snapshot(session, product, NOW, "public_discount")
    assert len(session.scalars(
        select(ProductSnapshot).where(ProductSnapshot.product_id == product.id)
    ).all()) == 3


def test_sku_snapshots_enforce_product_ownership(session):
    product = make_product(session)
    other = make_product(session, "other-product", shop=product.shop)
    variant = ProductVariant(
        product=product, variant_key=fingerprint("black/M"), color="Black", size="M",
        price=Decimal("19.99"),
    )
    session.add(variant)
    session.flush()
    wrong_snapshot = make_snapshot(session, other)
    assert_rejected(session, VariantSnapshot(
        product_snapshot_id=wrong_snapshot.id, product_id=other.id,
        variant_id=variant.id, captured_at=NOW, price=Decimal("19.99"),
    ))
    snapshot = make_snapshot(session, product)
    session.add(VariantSnapshot(
        product_snapshot=snapshot, variant=variant, product_id=product.id,
        captured_at=NOW, price=Decimal("19.99"),
    ))
    session.flush()
    assert_rejected(session, VariantSnapshot(
        product_snapshot_id=snapshot.id, variant_id=variant.id, product_id=product.id,
        captured_at=NOW, price=Decimal("19.99"),
    ))


def test_event_dedup_and_snapshot_ownership(session):
    product = make_product(session)
    snapshot = make_snapshot(session, product)
    event = ChangeEvent(
        product=product, snapshot=snapshot, event_type=ChangeEventType.PRODUCT_NEW,
        new_value={"price": "19.99"}, event_hash=fingerprint("new-event"),
    )
    session.add(event)
    session.flush()
    assert_rejected(session, ChangeEvent(
        product_id=product.id, snapshot_id=snapshot.id,
        event_type=ChangeEventType.PRODUCT_NEW, event_hash=fingerprint("new-event"),
    ))
    other = make_product(session, "other-product", shop=product.shop)
    assert_rejected(session, ChangeEvent(
        product_id=other.id, snapshot_id=snapshot.id,
        event_type=ChangeEventType.PRODUCT_NEW, event_hash=fingerprint("wrong-event"),
    ))


def test_task_defaults_and_duplicate_delivery(session):
    task = CrawlTask(
        region="US", task_type="product_detail", resource_type="product",
        resource_id="product-1", idempotency_key=fingerprint("task-1"),
    )
    session.add(task)
    session.flush()
    assert task.status == TaskStatus.PENDING
    assert task.payload == {}
    assert task.attempt_count == 0
    assert task.max_attempts == 3
    assert task.created_at is not None
    assert_rejected(session, CrawlTask(
        region="US", task_type="product_detail", resource_type="product",
        resource_id="product-1", idempotency_key=fingerprint("task-1"),
    ))
    session.add(CrawlAttempt(task=task, attempt_no=1, succeeded=True))
    session.flush()
    assert_rejected(session, CrawlAttempt(task_id=task.id, attempt_no=1))


def test_mysql_rejects_invalid_status_even_without_orm(session):
    shop = make_shop(session)
    with pytest.raises(DBAPIError) as rejected:
        with session.begin_nested():
            session.execute(
                text("UPDATE shops SET status='not_a_real_status' WHERE id=:id"),
                {"id": shop.id},
            )
    assert rejected.value.orig.args[0] == 3819


def test_native_datetime_utc_roundtrip(session):
    shop = make_shop(session)
    local = NOW.astimezone(timezone(timedelta(hours=8)))
    shop.first_seen_at = local
    session.flush()
    session.expire(shop)
    assert shop.first_seen_at == NOW
    assert shop.first_seen_at.microsecond == 123456


def test_server_updated_at_refreshes_after_orm_update(session):
    shop = make_shop(session)
    baseline = datetime(2000, 1, 1, tzinfo=timezone.utc)
    shop.updated_at = baseline
    session.flush()
    shop.canonical_name = "New name"
    session.flush()
    assert shop.updated_at > baseline


def test_session_scope_rolls_back_failure(mysql_engine):
    factory = make_session_factory(mysql_engine)
    with pytest.raises(RuntimeError):
        with session_scope(factory) as db:
            db.add(Shop(
                region="US", platform_shop_id="rollback-marker", canonical_name="Must roll back"
            ))
            db.flush()
            raise RuntimeError("Simulate a failed write")
    with factory() as db:
        assert db.scalar(select(Shop).where(Shop.platform_shop_id == "rollback-marker")) is None


def test_mysql_upsert_updates_without_duplicate_rows(session):
    make_shop(session, "upsert-shop")
    statement = insert(Shop).values(
        region="US", platform_shop_id="upsert-shop", canonical_name="Renamed shop"
    )
    statement = statement.on_duplicate_key_update(canonical_name=statement.inserted.canonical_name)
    session.execute(statement)
    rows = session.scalars(
        select(Shop).where(Shop.platform_shop_id == "upsert-shop")
    ).all()
    assert len(rows) == 1
    session.refresh(rows[0])
    assert rows[0].canonical_name == "Renamed shop"


def test_parent_delete_does_not_erase_history(session):
    product = make_product(session)
    make_snapshot(session, product)
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.delete(product)
            session.flush()
