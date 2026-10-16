"""Run asynchronous derived-data jobs; never visits source websites."""

import argparse
import asyncio
import json
import logging
import time

from sqlalchemy.exc import OperationalError

from app.services.analytics_refresh import LIGHT_KINDS, enabled
from app.services.analytics_runtime import backfill_batch, maintain, ready, tables_ready
from app.services.async_analytics import AsyncAnalyticsRunner
from app.settings import Settings
from app.storage.async_database import AsyncDatabase, make_async_engine
from scripts.async_runtime import pause_until_stop
from scripts.runtime_support import stop_signal, log_failure


async def run(config, args):
    # Queue mutations need record locks, not repeatable-read gap locks over
    # absent statistic keys. Source aggregation still owns its explicit RR view.
    database = AsyncDatabase(make_async_engine(config.database_url).execution_options(isolation_level="READ COMMITTED"))
    light_worker = None
    try:
        if not await database.read(lambda db: tables_ready(db.connection())):
            raise ValueError("Run scripts.migrate_analytics --execute before enabling analytics.")
        if args.status:
            print(json.dumps({"enabled": enabled(), "ready": await database.read(ready, config)}))
            return 0
        if not enabled():
            raise ValueError("Set ANALYTICS_ENABLED=1 on all writers and the API before starting the backfill.")
        stop, runner, last_backfill = stop_signal(), AsyncAnalyticsRunner(database, config), 0
        async def refresh_light_resources():
            light_runner, last_maintenance = AsyncAnalyticsRunner(database, config), 0
            while not stop.is_set():
                if time.monotonic() - last_maintenance >= 10:
                    try:
                        await database.transaction(maintain, config)
                    except OperationalError as error:
                        code = getattr(error.orig, "args", (None,))[0]
                        if code not in {1205, 1213}:
                            raise
                        logging.getLogger(__name__).info("analytics_maintenance_retry", extra={"database_code": code})
                        await pause_until_stop(stop, 1)
                        continue
                    last_maintenance = time.monotonic()
                processed = await light_runner.run_once(slot_name="analysis_light", only_kinds=LIGHT_KINDS)
                await pause_until_stop(stop, .1 if processed else 1)
        if not args.once:
            light_worker = asyncio.create_task(refresh_light_resources())
        while not stop.is_set():
            if light_worker is not None and light_worker.done():
                await light_worker
            if time.monotonic() - last_backfill >= 1 or args.once:
                await database.transaction(backfill_batch, config)
                last_backfill = time.monotonic()
            if args.once:
                await database.transaction(maintain, config)
            jobs = await asyncio.gather(*(runner.run_once(exclude_kinds=() if args.once else LIGHT_KINDS)
                                         for _ in range(config.analytics_concurrency)))
            if args.once:
                print(json.dumps({"processed": any(jobs), "jobs_processed": sum(jobs)}))
                return 0
            # These jobs only read/write our database. Source-site pacing lives
            # in the collection gate; avoid an artificial 20-jobs/s refill cap.
            await pause_until_stop(stop, .001 if any(jobs) else 1)
        return 0
    finally:
        if light_worker is not None:
            light_worker.cancel()
            await asyncio.gather(light_worker, return_exceptions=True)
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
