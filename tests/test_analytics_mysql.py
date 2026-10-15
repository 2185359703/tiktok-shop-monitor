import re

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.models import Product, ProductSnapshot, ProductVariant
from app.services.analytics_service import (
    build_workbench, color_size_matrix, current_variant_condition, filters,
    observed_variants_query, shop_variant_aggregation, workbench_variant_query,
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


def assert_temporary_removed(session, name):
    assert re.fullmatch(r"workbench_skus_[a-f0-9]{32}", name)
    with pytest.raises(DBAPIError) as error:
        session.execute(text(f"SELECT COUNT(*) FROM `{name}`"))
    assert error.value.orig.args[0] == 1146


def test_mysql_temporary_projection_matches_exact_json_membership_and_cleans_up(analytics_mysql_reader):
    session = analytics_mysql_reader
    product, task = initial(session)
    page = snapshot()
    persist_product_detail(session, task, page)
    first, second = [variant.platform_variant_id for variant in page.detail.variants]
    clauses, scope = filters(Settings(), session=session)
    for identifiers, expected in (([first, first, second, int(first)], 2),
                                  ([int(first), None, False, first + " ", first + "x" * 129], 0),
                                  ([second], 1), ([], 0)):
        product.raw_attributes = {**product.raw_attributes, "observed_sku_ids": identifiers}
        session.flush()
        original = session.scalar(select(func.count()).select_from(Product).join(ProductVariant,
            ProductVariant.product_id == Product.id).where(Product.id == product.id,
            current_variant_condition(session)))
        with workbench_variant_query(session, clauses, scope["currency"]) as query:
            name = re.search(r"workbench_skus_[a-f0-9]{32}", str(query.compile(dialect=session.bind.dialect))).group(0)
            assert session.scalar(query.with_only_columns(func.count())) == original == expected
            projected_sql = str(query.compile(dialect=session.bind.dialect)).lower()
            assert " join " not in projected_sql and "products" not in projected_sql
            rows = session.execute(shop_variant_aggregation(query)).all()
            assert sum(row[1] for row in rows) == expected
            matrix = color_size_matrix(session, query, expected)
            assert matrix["total_variants"] == matrix["eligible_variants"] == expected
        assert_temporary_removed(session, name)


def test_mysql_temporary_projection_cleans_up_when_consumer_raises(analytics_mysql_reader):
    session = analytics_mysql_reader
    product, task = initial(session)
    persist_product_detail(session, task, snapshot())
    clauses, scope = filters(Settings(), session=session)
    with pytest.raises(RuntimeError):
        with workbench_variant_query(session, clauses, scope["currency"]) as query:
            name = re.search(r"workbench_skus_[a-f0-9]{32}", str(query.compile(dialect=session.bind.dialect))).group(0)
            assert session.scalar(query.with_only_columns(func.count())) == 2
            raise RuntimeError("consumer interrupted")
    assert_temporary_removed(session, name)
    assert session.get(Product, product.id).last_success_at is not None


def test_mysql_workbench_expands_observed_skus_once_without_business_writes(analytics_mysql_reader):
    session = analytics_mysql_reader
    product, task = initial(session)
    page = snapshot()
    persist_product_detail(session, task, page)
    before = (product.content_hash, product.data_version, product.last_success_at, product.raw_attributes)
    statements, observed_reads = [], []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
        values = parameters.values() if isinstance(parameters, dict) else parameters
        if any(isinstance(value, str) and "observed_sku_ids" in value for value in values):
            observed_reads.append(statement)
    event.listen(session.bind, "before_cursor_execute", capture)
    try:
        result = build_workbench(session, Settings(), as_of=page.captured_at)
    finally:
        event.remove(session.bind, "before_cursor_execute", capture)
    assert result["summary"]["variants"] == 2
    assert result["color_size_matrix"]["eligible_variants"] == 2
    assert len(observed_reads) == 1 and "JSON_TABLE" not in observed_reads[0].upper()
    assert sum(statement.lstrip().startswith("CREATE TEMPORARY TABLE") for statement in statements) == 1
    assert sum(statement.lstrip().startswith("DROP TEMPORARY TABLE") for statement in statements) == 1
    assert session.scalar(select(func.count()).select_from(ProductSnapshot)) == 0
    assert (product.content_hash, product.data_version, product.last_success_at, product.raw_attributes) == before
    assert session.bind.get_isolation_level() == "READ COMMITTED"


def test_mysql_bounded_projection_preserves_sql_collation_for_colors(analytics_mysql_reader):
    session = analytics_mysql_reader
    product, task = initial(session)
    persist_product_detail(session, task, snapshot())
    variants = session.scalars(select(ProductVariant).where(ProductVariant.product_id == product.id)
                               .order_by(ProductVariant.id)).all()
    variants[0].color, variants[1].color = "Red ", "red"
    session.flush()
    clauses, scope = filters(Settings(), session=session)
    with workbench_variant_query(session, clauses, scope["currency"]) as query:
        assert sum(row[1] for row in session.execute(shop_variant_aggregation(query))) == 2
        matrix = color_size_matrix(session, query, 2)
        assert len(matrix["colors"]) == 1 and matrix["colors"][0].casefold() == "red"
        assert matrix["eligible_variants"] == matrix["shown_variants"] == 2
