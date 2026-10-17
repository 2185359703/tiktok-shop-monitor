"""Create additive one-off collection launch tables without changing source rows."""

import argparse
import json

from sqlalchemy import inspect

from app.models import Base
from app.models.collection_launch import LAUNCH_TABLE_NAMES
from app.storage.database import make_engine
from app.storage.schema import model_tables, mysql_ddl, require_supported_server, schema_drift


def additive_sql():
    sql = mysql_ddl(table_names=LAUNCH_TABLE_NAMES)
    return "-- Explicit collection launches and durable task membership.\nSET NAMES utf8mb4;\nSET time_zone = '+00:00';\n\n" + sql[sql.index("CREATE TABLE IF NOT EXISTS "):]


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
        existing = set(inspect(engine).get_table_names())
        if not {"crawl_tasks", "shops", "products", "seed_shop_observations"}.issubset(existing):
            raise ValueError("Existing source collection tables are required")
        if args.execute:
            Base.metadata.create_all(engine, tables=model_tables(LAUNCH_TABLE_NAMES), checkfirst=True)
        existing = set(inspect(engine).get_table_names())
        drift = [item for item in schema_drift(engine) if any(name in item for name in LAUNCH_TABLE_NAMES)]
        ready = set(LAUNCH_TABLE_NAMES).issubset(existing) and not drift
        print(json.dumps({"ready": ready, "source_data_rows_modified": 0}))
        return 0 if ready else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
