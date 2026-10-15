from pathlib import Path

from sqlalchemy import inspect

from scripts.migrate_product_reviews import additive_sql, migrate
from test_shop_profile_migration import legacy_profile_engine, insert_legacy_rows, legacy_rows


def test_saved_review_migration_matches_model():
    path = Path(__file__).resolve().parents[1] / "migrations/007_product_reviews.sql"
    assert path.read_text(encoding="utf-8") == additive_sql()


def test_legacy_review_migration_preserves_existing_data_and_is_repeatable(legacy_profile_engine):
    engine = legacy_profile_engine
    insert_legacy_rows(engine)
    before = legacy_rows(engine)
    preview = migrate(engine)
    assert not preview["ready"] and preview["executed_statements"] == 0
    assert "product_reviews" not in inspect(engine).get_table_names()
    result = migrate(engine, execute=True)
    assert result["ready"] and result["data_rows_modified"] == 0
    assert legacy_rows(engine) == before
    assert migrate(engine, execute=True)["executed_statements"] == 0
