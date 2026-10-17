"""Add reversible collection plan archival without changing existing plans."""

import argparse
import json

from sqlalchemy import inspect, text
from sqlalchemy.dialects.mysql import dialect
from sqlalchemy.schema import CreateColumn

from app.models.collection import CollectionPlan
from app.storage.database import make_engine
from app.storage.schema import normalize_mysql_type, require_supported_server


LOCK_NAME = "tiktok_collection_plan_archive"
COLUMN = CollectionPlan.__table__.c.archived_at


def statement(mysql):
    definition = str(CreateColumn(COLUMN).compile(dialect=mysql))
    return "ALTER TABLE collection_plans ADD COLUMN " + definition + ", ALGORITHM=INSTANT"


def additive_sql():
    return ("-- Reversible plan archival. Apply with scripts.migrate_collection_plan_archive.\n"
            "-- Existing plans remain unarchived; no business rows are rewritten.\n"
            + statement(dialect()) + ";\n")


def migration_plan(connection):
    inspector = inspect(connection)
    if not inspector.has_table("collection_plans"):
        raise ValueError("Apply the collection management migration before plan archival.")
    actual = next((column for column in inspector.get_columns("collection_plans") if column["name"] == COLUMN.name), None)
    if actual is None:
        return [statement(connection.dialect)]
    if (actual["nullable"] != COLUMN.nullable or normalize_mysql_type(actual["type"].compile(dialect=connection.dialect)) !=
            normalize_mysql_type(COLUMN.type.compile(dialect=connection.dialect))):
        raise ValueError("collection_plans.archived_at has an incompatible definition.")
    return []


def migrate(engine, *, execute=False):
    require_supported_server(engine)
    with engine.connect() as connection:
        if execute and connection.scalar(text("SELECT GET_LOCK(:name, 0)"), {"name": LOCK_NAME}) != 1:
            raise ValueError("Another collection plan archive migration is running.")
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
        print(json.dumps({"error_type": type(error).__name__, "message": "Collection plan archival migration did not finish."}))
        raise SystemExit(1)
