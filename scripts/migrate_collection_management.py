"""Add durable collection plans, controls and audit without rewriting source data."""

import argparse
import json
import re
from pathlib import Path

from sqlalchemy import inspect, text

from app.models import Base
from app.models.collection import COLLECTION_TABLE_NAMES
from app.storage.database import make_engine
from app.storage.schema import normalize_mysql_type, require_supported_server


LOCK_NAME = "tiktok_collection_management_schema"


def additive_sql():
    # 012 is an immutable foundation. Later model columns (such as 015's
    # archived_at) must neither rewrite its SQL nor make its check fail.
    return (Path(__file__).resolve().parents[1] / "migrations/012_collection_management.sql").read_text(encoding="utf-8")


def migration_plan(connection):
    inspector = inspect(connection)
    existing = set(inspector.get_table_names())
    if not {"crawl_tasks", "crawl_attempts", "shops", "products"}.issubset(existing):
        raise ValueError("The existing collection schema is required.")
    statements = ["\n".join(line for line in value.splitlines() if not line.lstrip().startswith("--")).strip()
                  for value in additive_sql().split(";")]
    actions = []
    for statement in statements:
        table_match = re.match(r"CREATE TABLE IF NOT EXISTS ([a-z_]+)", statement)
        index_match = re.match(r"CREATE INDEX ([a-z_]+) ON ([a-z_]+) \(([^)]+)\)", statement)
        if table_match:
            name = table_match.group(1)
            if name not in COLLECTION_TABLE_NAMES:
                raise ValueError("Unexpected table in the historical collection migration.")
            if name not in existing:
                actions.append(statement)
                continue
            actual = {column["name"]: column for column in inspector.get_columns(name)}
            historical = re.findall(r"^\s+([a-z][a-z_0-9]*)\s+", statement, flags=re.MULTILINE)
            for column_name in historical:
                column = Base.metadata.tables[name].c[column_name]
                reflected = actual.get(column_name)
                if (reflected is None or column.nullable != reflected["nullable"]
                        or normalize_mysql_type(column.type.compile(dialect=connection.dialect)) !=
                        normalize_mysql_type(reflected["type"].compile(dialect=connection.dialect))):
                    raise ValueError(f"{name}.{column_name}: historical column definition differs")
        elif index_match:
            name, table, columns = index_match.groups()
            actual = next((item for item in inspector.get_indexes(table) if item["name"] == name), None) if table in existing else None
            if actual is None:
                actions.append(statement)
            elif actual["column_names"] != [value.strip() for value in columns.split(",")] or actual["unique"]:
                raise ValueError(f"{table}.{name}: historical index definition differs")
    return actions


def migrate(engine, *, execute=False):
    require_supported_server(engine)
    with engine.connect() as connection:
        if execute and connection.scalar(text("SELECT GET_LOCK(:name, 0)"), {"name": LOCK_NAME}) != 1:
            raise ValueError("Another collection management migration is running.")
        old_wait = connection.scalar(text("SELECT @@SESSION.lock_wait_timeout"))
        try:
            actions = migration_plan(connection)
            if execute:
                connection.execute(text("SET SESSION lock_wait_timeout=10"))
                for statement in actions:
                    connection.exec_driver_sql(statement)
            remaining = migration_plan(connection) if execute else actions
            return {"ready": not remaining, "executed_statements": len(actions) if execute else 0,
                    "pending_statements": len(remaining), "source_table_alterations": 0, "data_rows_modified": 0}
        finally:
            connection.execute(text("SET SESSION lock_wait_timeout=:value"), {"value": old_wait})
            if execute:
                connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": LOCK_NAME})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--execute", action="store_true")
    modes.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute and not args.check:
        print(additive_sql(), end="")
        return 0
    engine = make_engine()
    try:
        result = migrate(engine, execute=args.execute)
        print(json.dumps(result))
        return 0 if result["ready"] else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "message": "Collection management migration did not finish."}))
        raise SystemExit(1)
