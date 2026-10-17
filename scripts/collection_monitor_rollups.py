"""Activate only after every writer has the completion hook, or inspect/progress monitoring."""

import argparse
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.collection_monitor import CollectionMonitorState as State
from app.services.collection_rollups import activate, advance_rollups, reconcile, scoped, tables_ready
from app.settings import Settings
from app.storage.database import make_engine


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--activate", action="store_true", help="All attempt writers must already run the transactional completion hook.")
    modes.add_argument("--reconcile", action="store_true", help="After switching writers, repair the latest 25h without double counting existing facts.")
    modes.add_argument("--once", action="store_true", help="Process at most 500 historical and 500 pending records.")
    modes.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    config = Settings.load()
    engine = make_engine(config.database_url)
    try:
        if not tables_ready(engine):
            raise ValueError("Run scripts.migrate_collection_monitor_rollups --execute first.")
        with Session(engine) as db:
            if args.activate:
                activate(db, config)
                db.commit()
            elif args.reconcile:
                reconcile(db, config)
                db.commit()
            elif args.once:
                advance_rollups(db, config)
                db.commit()
            row = db.scalar(select(State).where(*scoped(State, config)))
            print(json.dumps({"activated": row is not None, "backfill_complete": bool(row and row.backfill_complete),
                "coverage_since": row.coverage_since.isoformat() if row else None,
                "backfill_until": row.backfill_until.isoformat() if row else None,
                "published_at": row.published_at.isoformat() if row and row.published_at else None,
                "generation": row.generation if row else 0}))
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
