"""Additive profile migration is verified only in disposable local schemas."""

import json
import re
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from app.storage.database import make_engine
from scripts import migrate_shop_profiles


MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
SHOP_URL = "https://shop.tiktok.com/us/store/legacy/7495552971774921097"


@pytest.fixture
def legacy_profile_engine(mysql_engine):
    """Create a separate legacy schema; never reuse or alter the configured DB."""
    url = mysql_engine.url
    if url.host not in {"127.0.0.1", "localhost"} or not re.fullmatch(r"tiktok_shop_monitor_test_[A-Za-z0-9_]+", url.database or ""):
        raise ValueError("Profile migration tests require an isolated local test database.")
    name = "tiktok_shop_monitor_test_profile_migration_" + uuid4().hex[:16]
    assert re.fullmatch(r"tiktok_shop_monitor_test_[A-Za-z0-9_]{1,40}", name) and len(name) <= 64
    target = url.set(database=name)
    admin, engine, created = make_engine(target, with_database=False), None, False
    try:
        with admin.connect() as connection:
            assert connection.scalar(text("SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = :name"), {"name": name}) is None
            connection.exec_driver_sql(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
            created = True
        engine = make_engine(target)
        initial = (MIGRATIONS / "001_initial_schema.sql").read_text(encoding="utf-8")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT DATABASE()")) == name
            for statement in initial.split(";"):
                statement = "\n".join(line for line in statement.splitlines() if not line.lstrip().startswith("--")).strip()
                if not statement or statement.startswith(("CREATE DATABASE", "USE ")):
                    continue
                connection.exec_driver_sql(statement)
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            # This exact validated name was generated and created above.
            with admin.connect() as connection:
                connection.exec_driver_sql(f"DROP DATABASE `{name}`")
        admin.dispose()


def insert_legacy_rows(engine):
    task_uuid = str(uuid4())
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO shops (id, platform_shop_id, canonical_name, canonical_url, region, follower_count, product_count, rating) "
                                "VALUES (41, '7495552971774921097', 'Legacy shop', :url, 'US', 10, 206, 4.25)"), {"url": SHOP_URL})
        connection.execute(text("INSERT INTO products (id, platform_product_id, shop_id, product_url, title, region) "
                                "VALUES (51, '1729591769491607881', 41, 'https://shop.tiktok.com/us/pdp/1729591769491607881', 'Legacy product', 'US')"))
        connection.execute(text("INSERT INTO crawl_tasks (id, task_id, task_type, resource_type, resource_id, payload, region, status) "
                                "VALUES (61, :task_id, 'product_discovery', 'shop', '41', :payload, 'US', 'succeeded')"),
                           {"task_id": task_uuid, "payload": json.dumps({"shop_id": "41", "platform_shop_id": "7495552971774921097"})})


def legacy_rows(engine):
    with engine.connect() as connection:
        return {
            "shop": dict(connection.execute(text("SELECT id, platform_shop_id, canonical_name, canonical_url, platform, region, follower_count, product_count, rating, created_at, updated_at FROM shops WHERE id = 41")).mappings().one()),
            "product": dict(connection.execute(text("SELECT * FROM products WHERE id = 51")).mappings().one()),
            "task": dict(connection.execute(text("SELECT * FROM crawl_tasks WHERE id = 61")).mappings().one()),
        }


def test_profile_migration_preview_never_connects_or_emits_destructive_sql(monkeypatch, capsys):
    def must_not_connect(*args, **kwargs):
        raise AssertionError("Preview must not create engines or read database configuration.")
    monkeypatch.setattr(migrate_shop_profiles, "make_engine", must_not_connect)
    assert migrate_shop_profiles.main([]) == 0
    output = capsys.readouterr().out
    assert '"database_connections": 0' in output and '"data_rows_modified": 0' in output
    assert "ALTER TABLE shops" in output and "ALTER TABLE crawl_tasks" in output
    assert output.count("CREATE TABLE IF NOT EXISTS ") == 1
    assert "CREATE TABLE IF NOT EXISTS shop_profile_snapshots" in output
    for forbidden in ("CREATE DATABASE", "DROP TABLE", "DROP COLUMN", "DELETE FROM", "TRUNCATE", "UPDATE shops"):
        assert forbidden not in output


def test_legacy_migration_preserves_identities_metrics_tasks_and_relations_then_repeats_as_noop(legacy_profile_engine):
    engine = legacy_profile_engine
    insert_legacy_rows(engine)
    before = legacy_rows(engine)
    assert "profile_captured_at" not in {column["name"] for column in inspect(engine).get_columns("shops")}
    with pytest.raises(DBAPIError) as rejected, engine.begin() as connection:
        connection.execute(text("INSERT INTO crawl_tasks (task_id, task_type, resource_type, resource_id, payload, region) "
                                "VALUES (:id, 'shop_profile', 'shop', '41', '{}', 'US')"), {"id": str(uuid4())})
    assert rejected.value.orig.args[0] == 3819
    preview = migrate_shop_profiles.migrate(engine)
    assert preview["mode"] == "check" and not preview["ready"] and preview["pending_statements"] >= 3
    assert legacy_rows(engine) == before and "shop_profile_snapshots" not in inspect(engine).get_table_names()
    result = migrate_shop_profiles.migrate(engine, execute=True)
    assert result["ready"] and result["pending_statements"] == 0 and result["executed_statements"] >= 3
    assert result["data_rows_modified"] == 0 and legacy_rows(engine) == before
    with engine.connect() as connection:
        values = connection.execute(text("SELECT " + ", ".join(migrate_shop_profiles.PROFILE_COLUMNS) + " FROM shops WHERE id = 41")).one()
        assert all(value is None for value in values)
        assert connection.scalar(text("SELECT COUNT(*) FROM shop_profile_snapshots")) == 0
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO crawl_tasks (task_id, task_type, resource_type, resource_id, payload, region) "
                                "VALUES (:id, 'shop_profile', 'shop', '41', '{}', 'US')"), {"id": str(uuid4())})
    assert migrate_shop_profiles.migrate(engine, execute=True) == {
        "mode": "execute", "ready": True, "executed_statements": 0, "pending_statements": 0, "data_rows_modified": 0,
    }
    assert legacy_rows(engine) == before
    reflected = inspect(engine)
    foreign_keys = {tuple(item["constrained_columns"]): (item["referred_table"], tuple(item["referred_columns"]))
                    for item in reflected.get_foreign_keys("shop_profile_snapshots")}
    assert foreign_keys[("shop_id", "platform", "region")] == ("shops", ("id", "platform", "region"))
    assert foreign_keys[("task_id",)] == ("crawl_tasks", ("id",))


def test_incompatible_existing_profile_column_fails_before_any_other_ddl(legacy_profile_engine):
    engine = legacy_profile_engine
    insert_legacy_rows(engine)
    before = legacy_rows(engine)
    with engine.connect() as connection:
        connection.exec_driver_sql("ALTER TABLE shops ADD COLUMN sales_count VARCHAR(64)")
    with pytest.raises(ValueError, match="sales_count"):
        migrate_shop_profiles.migrate(engine, execute=True)
    columns = {column["name"] for column in inspect(engine).get_columns("shops")}
    assert "profile_captured_at" not in columns and "shop_profile_snapshots" not in inspect(engine).get_table_names()
    assert legacy_rows(engine) == before


def test_missing_legacy_task_constraint_fails_before_shop_schema_is_changed(legacy_profile_engine):
    engine = legacy_profile_engine
    with engine.connect() as connection:
        connection.exec_driver_sql("ALTER TABLE crawl_tasks DROP CHECK ck_crawl_tasks_task_type")
    with pytest.raises(ValueError, match="约束缺失"):
        migrate_shop_profiles.migrate(engine, execute=True)
    assert "profile_captured_at" not in {column["name"] for column in inspect(engine).get_columns("shops")}
    assert "shop_profile_snapshots" not in inspect(engine).get_table_names()


@pytest.mark.parametrize("ddl,reason", [
    ("ALTER TABLE shop_profile_snapshots DROP FOREIGN KEY fk_shop_profile_snapshots_shop_scope", "foreign keys"),
    ("DROP INDEX ix_shop_profile_snapshots_history ON shop_profile_snapshots", "history index"),
    ("ALTER TABLE shop_profile_snapshots DROP CHECK ck_shop_profile_snapshots_observation_status", "observation"),
])
def test_existing_snapshot_table_with_missing_integrity_constraint_is_not_reported_ready(legacy_profile_engine, ddl, reason):
    engine = legacy_profile_engine
    assert migrate_shop_profiles.migrate(engine, execute=True)["ready"]
    with engine.connect() as connection:
        connection.exec_driver_sql(ddl)
    with pytest.raises(ValueError, match=reason):
        migrate_shop_profiles.migrate(engine)
