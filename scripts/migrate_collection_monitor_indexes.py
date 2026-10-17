"""Add the ordered recent-attempt index without rebuilding collection data."""

import argparse
import json

from app.models.task import CrawlAttempt
from app.storage.database import make_engine
from scripts.migrate_ui_indexes import migrate, statement


INDEXES = tuple(index for index in CrawlAttempt.__table__.indexes if index.name == "ix_crawl_attempts_monitor")


def additive_sql():
    return "-- Ordered recent attempts for bounded collection monitoring.\n" + statement(INDEXES[0]) + ";\n"


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
        result = migrate(engine, execute=args.execute, indexes=INDEXES, lock_name="tiktok_collection_monitor_index")
        print(json.dumps(result))
        return 0 if result["ready"] else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
