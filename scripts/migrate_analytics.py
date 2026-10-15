"""Preview or create the four derived read tables; never ALTER source tables."""

import argparse
import json

from sqlalchemy import inspect

from app.models import Base
from app.models.analytics_read import ANALYTICS_TABLE_NAMES
from app.storage.database import make_engine
from app.storage.schema import INITIAL_TABLE_NAMES, model_tables, mysql_ddl, require_supported_server, schema_drift


def additive_sql():
    sql = mysql_ddl(table_names=ANALYTICS_TABLE_NAMES)
    return "-- Rebuildable UI read models. Apply with scripts.migrate_analytics.\nSET NAMES utf8mb4;\nSET time_zone = '+00:00';\n\n" + sql[sql.index("CREATE TABLE IF NOT EXISTS "):]


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
        names = set(inspect(engine).get_table_names())
        if not set(INITIAL_TABLE_NAMES).issubset(names) or schema_drift(engine):
            raise ValueError("Source schema is missing or has drift; this migration never alters it.")
        if args.execute:
            Base.metadata.create_all(engine, tables=model_tables(ANALYTICS_TABLE_NAMES), checkfirst=True)
        ready = set(ANALYTICS_TABLE_NAMES).issubset(inspect(engine).get_table_names()) and not schema_drift(engine)
        print(json.dumps({"ready": ready, "mode": "execute" if args.execute else "check", "source_table_alterations": 0}))
        return 0 if ready else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
