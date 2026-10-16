"""Create summary and attribute-index tables; source tables are unchanged."""

import argparse
import json

from sqlalchemy import inspect

from app.models import Base
from app.models.analytics_read import ANALYTICS_TABLE_NAMES, ROLLUP_TABLE_NAMES
from app.storage.database import make_engine
from app.storage.schema import model_tables, mysql_ddl, require_supported_server, schema_drift


def additive_sql():
    sql = mysql_ddl(table_names=ROLLUP_TABLE_NAMES)
    return "-- Incremental product summaries. Apply with scripts.migrate_analytics_rollups.\nSET NAMES utf8mb4;\nSET time_zone = '+00:00';\n\n" + sql[sql.index("CREATE TABLE IF NOT EXISTS "):]


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
        if not set(ANALYTICS_TABLE_NAMES).issubset(inspect(engine).get_table_names()) or schema_drift(engine):
            raise ValueError("Read models are missing or the existing schema has drift.")
        if args.execute:
            Base.metadata.create_all(engine, tables=model_tables(ROLLUP_TABLE_NAMES), checkfirst=True)
        ready = set(ROLLUP_TABLE_NAMES).issubset(inspect(engine).get_table_names()) and not schema_drift(engine)
        print(json.dumps({"ready": ready, "source_table_alterations": 0}))
        return 0 if ready else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
