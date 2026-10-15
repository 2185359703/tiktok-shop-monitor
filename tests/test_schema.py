from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy.dialects.mysql import dialect
from sqlalchemy.orm import configure_mappers

from app.models import Base, MatchStatus, ProductImage, ProductMatch, ProductContent, ProductReview, ShopProfileSnapshot, SkuMatch
from app.models.base import UTCDateTime
from app.models.analytics_read import ANALYTICS_TABLE_NAMES
from app.storage.schema import INITIAL_TABLE_NAMES, WORKBENCH_TABLE_NAMES, model_tables, mysql_comments_ddl, mysql_ddl, validate_database_name


MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def normalized_sql(value):
    """Historical migration bytes remain intact; insignificant EOL spaces do not matter."""
    return "\n".join(line.rstrip() for line in value.splitlines()) + "\n"


def test_historical_ten_table_ddl_is_preserved():
    configure_mappers()
    assert len(INITIAL_TABLE_NAMES) == 10
    # Later column/CHECK migrations evolve these models. The original migration
    # remains immutable and is exercised independently by the migration tests.
    saved = normalized_sql((MIGRATIONS / "001_initial_schema.sql").read_text(encoding="utf-8"))
    assert sha256(saved.encode()).hexdigest() == "0c1b9a2e497b6e10d68bc1bc8c4959a84f05a9719565dc8a32ba7d39b8068088"


def test_current_schema_matches_registered_models():
    assert len(Base.metadata.tables) == 20
    assert set(Base.metadata.tables) == set(INITIAL_TABLE_NAMES) | set(WORKBENCH_TABLE_NAMES) | set(ANALYTICS_TABLE_NAMES) | {ProductContent.__tablename__, ShopProfileSnapshot.__tablename__, ProductReview.__tablename__}
    assert (MIGRATIONS / "current_schema.sql").read_text(encoding="utf-8") == mysql_ddl()
    assert (ProductImage.__tablename__, ProductMatch.__tablename__, SkuMatch.__tablename__) == WORKBENCH_TABLE_NAMES
    assert MatchStatus.CONFIRMED.value == "confirmed"


def test_all_tables_and_columns_have_chinese_comments():
    for table in Base.metadata.tables.values():
        assert table.comment and any("\u4e00" <= c <= "\u9fff" for c in table.comment), table.name
        for column in table.c:
            assert column.comment and any(
                "\u4e00" <= c <= "\u9fff" for c in column.comment
            ), f"{table.name}.{column.name}"
            assert len(column.comment.encode("utf-8")) <= 1024


def test_comment_migration_matches_models():
    saved = normalized_sql((MIGRATIONS / "002_chinese_comments.sql").read_text(encoding="utf-8"))
    assert sha256(saved.encode()).hexdigest() == "4e42cab63d851e856852f2059c9345f745250454c6e7997bcd2ab8544f5404f9"


@pytest.mark.parametrize("filename,names", [
    ("003_product_images.sql", ("product_images",)),
    ("004_product_matching.sql", ("product_matches", "sku_matches")),
    ("005_product_contents.sql", ("product_contents",)),
    ("008_analytics_read_models.sql", ANALYTICS_TABLE_NAMES),
])
def test_additive_migration_table_and_index_definitions_match_models(filename, names):
    saved = (MIGRATIONS / filename).read_text(encoding="utf-8")
    generated = mysql_ddl(table_names=names)
    # Headers differ because additive migrations select an existing database. Every
    # CREATE TABLE, column, CHECK, FK, unique constraint and index is checked exactly.
    assert normalized_sql(saved[saved.index("CREATE TABLE IF NOT EXISTS "):]) == generated[generated.index("CREATE TABLE IF NOT EXISTS "):]
    assert "ALTER TABLE" not in saved and "DROP TABLE" not in saved


def test_schema_subset_order_does_not_depend_on_new_tables():
    assert [table.name for table in model_tables(INITIAL_TABLE_NAMES)] == [table.name for table in model_tables(reversed(INITIAL_TABLE_NAMES))]
    with pytest.raises(ValueError):
        mysql_ddl(table_names=("nonexistent_table",))
    with pytest.raises(ValueError):
        mysql_comments_ddl(table_names=("nonexistent_table",))


def test_migration_preview_is_offline_and_cannot_emit_catalog_alterations(monkeypatch, capsys):
    from scripts import migrate_workbench
    def must_not_connect(*args, **kwargs):
        raise AssertionError("Offline preview must not load DB configuration or make an engine.")
    monkeypatch.setattr(migrate_workbench, "configured_database_url", must_not_connect)
    monkeypatch.setattr(migrate_workbench, "make_engine", must_not_connect)
    assert migrate_workbench.main([]) == 0
    output = capsys.readouterr().out
    assert '"database_connections": 0' in output
    assert output.count("CREATE TABLE IF NOT EXISTS ") == 3
    assert "CREATE DATABASE" not in output and "ALTER TABLE" not in output and "DROP TABLE" not in output
    assert "CREATE TABLE IF NOT EXISTS products (" not in output
    assert "CREATE TABLE IF NOT EXISTS crawl_tasks (" not in output


def test_datetime_converts_to_utc_and_preserves_microseconds():
    local = datetime(2026, 10, 13, 18, 15, 3, 123456, tzinfo=timezone(timedelta(hours=8)))
    column_type = UTCDateTime()
    stored = column_type.process_bind_param(local, dialect())
    assert stored == datetime(2026, 10, 13, 10, 15, 3, 123456)
    restored = column_type.process_result_value(stored, dialect())
    assert restored == local.astimezone(timezone.utc)
    assert column_type.process_bind_param(None, dialect()) is None
    assert column_type.process_result_value(None, dialect()) is None


def test_content_migration_preview_is_offline_and_only_creates_content_table(monkeypatch, capsys):
    from scripts import migrate_product_content
    def must_not_connect(*args, **kwargs):
        raise AssertionError("Content preview cannot connect or read credentials.")
    monkeypatch.setattr(migrate_product_content, "configured_database_url", must_not_connect)
    monkeypatch.setattr(migrate_product_content, "make_engine", must_not_connect)
    assert migrate_product_content.main([]) == 0
    output = capsys.readouterr().out
    assert '"database_connections": 0' in output
    assert output.count("CREATE TABLE IF NOT EXISTS ") == 1
    assert "CREATE TABLE IF NOT EXISTS product_contents (" in output
    assert "CREATE DATABASE" not in output and "ALTER TABLE" not in output and "DROP TABLE" not in output


@pytest.mark.parametrize("name", ["", "db; DROP DATABASE x", "db-name", "a" * 65])
def test_database_name_rejects_invalid_identifiers(name):
    with pytest.raises(ValueError):
        validate_database_name(name)
