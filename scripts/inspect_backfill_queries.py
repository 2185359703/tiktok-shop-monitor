"""Inspect/cancel only this application's long read-only backfill status SQL."""

import argparse
import json

from sqlalchemy import text

from app.settings import Settings
from app.storage.database import make_engine


parser = argparse.ArgumentParser()
parser.add_argument("--cancel-stale-status-reads", action="store_true")
args = parser.parse_args()
engine = make_engine(Settings.load().database_url)
try:
    with engine.connect() as connection:
        rows = connection.execute(text("""SELECT ID, TIME, STATE, COMMAND, INFO
            FROM information_schema.PROCESSLIST
            WHERE DB=DATABASE() AND COMMAND='Query' AND ID<>CONNECTION_ID() ORDER BY TIME DESC""")).mappings().all()
        result = []
        for row in rows:
            sql = (row["INFO"] or "").lower().lstrip()
            status_read = (sql.startswith("select") and "join crawl_tasks" in sql
                           and "capture_mode" in sql and "image_source_version" in sql)
            item = {"connection_id": row["ID"], "seconds": row["TIME"], "state": row["STATE"],
                    "is_backfill_status_read": status_read, "temporary_workbench": "workbench_skus_" in sql,
                    "cancelled": False}
            if args.cancel_stale_status_reads and status_read and row["TIME"] > 60:
                connection.exec_driver_sql(f"KILL QUERY {int(row['ID'])}")
                item["cancelled"] = True
            result.append(item)
        print(json.dumps({"business_data_writes": 0, "queries": result}, ensure_ascii=False, indent=2))
finally:
    engine.dispose()
