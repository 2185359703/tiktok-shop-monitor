"""Preview or add the three workbench tables; historical catalog tables are never altered."""

import argparse
import json

from sqlalchemy import inspect

from app.models import Base
from app.storage.database import configured_database_url, make_engine
from app.storage.schema import (
    INITIAL_TABLE_NAMES, WORKBENCH_TABLE_NAMES, model_tables, mysql_ddl,
    require_supported_server, schema_drift,
)


def additive_sql():
    """Compile the same current definitions without database creation or selection."""
    ddl = mysql_ddl(table_names=WORKBENCH_TABLE_NAMES)
    return "SET time_zone = '+00:00';\nSET NAMES utf8mb4;\n\n" + ddl[ddl.index("CREATE TABLE IF NOT EXISTS "):]


def schema_status(engine):
    existing = set(inspect(engine).get_table_names())
    missing_initial = sorted(set(INITIAL_TABLE_NAMES) - existing)
    differences = schema_drift(engine)
    tables = {name: "present" if name in existing else "missing" for name in WORKBENCH_TABLE_NAMES}
    return {"tables": tables, "initial_tables_missing": missing_initial, "schema_drift": differences,
            "ready": not missing_initial and not differences and all(value == "present" for value in tables.values())}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--execute", action="store_true", help="Create only missing workbench tables in the configured existing database.")
    modes.add_argument("--check", action="store_true", help="Read table metadata and readiness only; no schema or data writes.")
    args = parser.parse_args(argv)
    if not args.execute and not args.check:
        # Do not even load the local environment or construct an engine in preview mode.
        print(json.dumps({"mode": "dry_run", "tables": list(WORKBENCH_TABLE_NAMES),
                          "database_connections": 0, "catalog_table_alterations": 0, "data_writes": 0}, ensure_ascii=False))
        print(additive_sql(), end="")
        return 0
    engine = make_engine(configured_database_url())
    try:
        require_supported_server(engine)
        before = schema_status(engine)
        if args.check:
            print(json.dumps({"mode": "check", **before}, ensure_ascii=False, indent=2))
            return 0 if before["ready"] else 1
        if before["initial_tables_missing"]:
            raise ValueError("Initial schema tables are missing; this command does not initialize the catalog.")
        if before["schema_drift"]:
            raise ValueError("Existing model tables differ from their definitions; this command does not ALTER them.")
        Base.metadata.create_all(engine, tables=model_tables(WORKBENCH_TABLE_NAMES), checkfirst=True)
        after = schema_status(engine)
        print(json.dumps({"mode": "execute", "created_tables": [name for name in WORKBENCH_TABLE_NAMES if before["tables"][name] == "missing" and after["tables"][name] == "present"],
                          "catalog_table_alterations": 0, "data_writes": 0, **after}, ensure_ascii=False, indent=2))
        return 0 if after["ready"] else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # Database credentials, driver messages and SQL parameters are never echoed.
        print(json.dumps({"error_type": type(error).__name__, "message": "工作台扩展表操作未完成；未修改旧表或采集任务。"}, ensure_ascii=False))
        raise SystemExit(1)
