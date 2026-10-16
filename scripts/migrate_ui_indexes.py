"""Add covering indexes online; never modify columns or business rows."""

import argparse
import json

from sqlalchemy import inspect, text

from app.models import CrawlTask, Product
from app.models.analytics_read import ProductReadModel
from app.storage.database import make_engine
from app.storage.schema import require_supported_server


INDEXES = tuple(next(index for index in model.__table__.indexes if index.name == name) for model, name in (
    (ProductReadModel, "ix_read_products_catalog"), (Product, "ix_products_recent_detail"),
    (CrawlTask, "ix_crawl_tasks_summary"), (ProductReadModel, "ix_read_products_title")))
LOCK_NAME = "tiktok_ui_read_indexes"


def statement(index):
    columns = ", ".join("`" + column.name + "`" for column in index.columns)
    return f"ALTER TABLE `{index.table.name}` ADD INDEX `{index.name}` ({columns}), ALGORITHM=INPLACE, LOCK=NONE"


def additive_sql():
    return "-- Online covering indexes for UI reads and background candidate scans.\n" + ";\n".join(
        statement(index) for index in INDEXES) + ";\n"


def migration_plan(connection):
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    actions = []
    for index in INDEXES:
        if index.table.name not in tables:
            raise ValueError("Create source and read-model tables before adding UI indexes.")
        actual = next((item for item in inspector.get_indexes(index.table.name) if item["name"] == index.name), None)
        if actual is None:
            actions.append(statement(index))
        elif actual["column_names"] != [column.name for column in index.columns] or actual["unique"]:
            raise ValueError("An existing UI index has a different definition.")
    return actions


def migrate(engine, *, execute=False):
    require_supported_server(engine)
    with engine.connect() as connection:
        if execute and connection.scalar(text("SELECT GET_LOCK(:name, 0)"), {"name": LOCK_NAME}) != 1:
            raise ValueError("Another UI index migration is running.")
        old_wait = connection.scalar(text("SELECT @@SESSION.lock_wait_timeout"))
        try:
            actions = migration_plan(connection)
            if execute:
                connection.execute(text("SET SESSION lock_wait_timeout=10"))
                for sql in actions:
                    connection.exec_driver_sql(sql)
            remaining = migration_plan(connection) if execute else actions
            return {"ready": not remaining, "executed_statements": len(actions) if execute else 0,
                    "pending_statements": len(remaining), "data_rows_modified": 0}
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
        print(json.dumps({"error_type": type(error).__name__, "message": "UI index migration did not finish."}))
        raise SystemExit(1)
