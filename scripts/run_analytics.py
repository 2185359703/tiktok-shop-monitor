"""Run asynchronous derived-data jobs; never visits source websites."""

import argparse
import asyncio
import json
import logging
import time

from app.services.analytics_refresh import enabled
from app.services.analytics_runtime import backfill_batch, maintain, ready, tables_ready
from app.services.async_analytics import AsyncAnalyticsRunner
from app.settings import Settings
from app.storage.async_database import AsyncDatabase, make_async_engine
from scripts.async_runtime import pause_until_stop
from scripts.runtime_support import stop_signal, log_failure


async def run(config, args):
    database = AsyncDatabase(make_async_engine(config.database_url))
    try:
        if not await database.read(lambda db: tables_ready(db.connection())):
            raise ValueError("Run scripts.migrate_analytics --execute before enabling analytics.")
        if args.status:
            print(json.dumps({"enabled": enabled(), "ready": await database.read(ready, config)}))
            return 0
        if not enabled():
            raise ValueError("Set ANALYTICS_ENABLED=1 on all writers and the API before starting the backfill.")
        stop, runner, last_maintenance, last_backfill = stop_signal(), AsyncAnalyticsRunner(database, config), 0, 0
        while not stop.is_set():
            if time.monotonic() - last_backfill >= 1 or args.once:
                await database.transaction(backfill_batch, config)
                last_backfill = time.monotonic()
            if time.monotonic() - last_maintenance >= 10 or args.once:
                await database.transaction(maintain, config)
                last_maintenance = time.monotonic()
            jobs = await asyncio.gather(*(runner.run_once() for _ in range(config.analytics_concurrency)))
            if args.once:
                print(json.dumps({"processed": any(jobs), "jobs_processed": sum(jobs)}))
                return 0
            await pause_until_stop(stop, .05 if any(jobs) else 1)
        return 0
    finally:
        await database.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    return asyncio.run(run(Settings.load(), args))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        raise SystemExit(main())
    except Exception as error:
        log_failure("analytics_failed", error)
        raise SystemExit(1)
