"""Add a compact covering index that MySQL can use for ordered recent pages."""

import argparse
import json

from app.models.analytics_read import ProductReadModel
from app.storage.database import make_engine
from scripts.migrate_ui_indexes import migrate, statement


INDEXES = tuple(index for index in ProductReadModel.__table__.indexes if index.name == "ix_read_products_recent_scope")


def additive_sql():
    return "-- Online scoped ordering index for bounded product pages.\n" + statement(INDEXES[0]) + ";\n"


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
        result = migrate(engine, execute=args.execute, indexes=INDEXES, lock_name="tiktok_ui_recent_index")
        print(json.dumps(result))
        return 0 if result["ready"] else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
