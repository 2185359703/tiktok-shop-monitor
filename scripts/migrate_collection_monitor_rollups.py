"""Add completion facts and monitoring rollups; activation is a separate step."""

import argparse
import json

from sqlalchemy import inspect

from app.models import Base
from app.models.collection_monitor import MONITOR_TABLE_NAMES
from app.storage.database import make_engine
from app.storage.schema import model_tables, mysql_ddl, require_supported_server, schema_drift


def additive_sql():
    sql = mysql_ddl(table_names=MONITOR_TABLE_NAMES)
    return "-- Rebuildable collection monitor facts and minute rollups. Does not activate backfill.\nSET NAMES utf8mb4;\nSET time_zone = '+00:00';\n\n" + sql[sql.index("CREATE TABLE IF NOT EXISTS "):]


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
        require_supported_server(engine)
        if not {"crawl_tasks", "crawl_attempts"}.issubset(inspect(engine).get_table_names()):
            raise ValueError("The existing collection schema is required.")
        if args.execute:
            Base.metadata.create_all(engine, tables=model_tables(MONITOR_TABLE_NAMES), checkfirst=True)
        existing = set(inspect(engine).get_table_names())
        drift = [item for item in schema_drift(engine) if any(name in item for name in MONITOR_TABLE_NAMES)]
        ready = set(MONITOR_TABLE_NAMES).issubset(existing) and not drift
        print(json.dumps({"ready": ready, "source_table_alterations": 0, "activated": False}))
        return 0 if ready else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
