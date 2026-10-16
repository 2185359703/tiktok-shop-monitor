"""Run asynchronous derived-data jobs; never visits source websites."""

import argparse
import asyncio
import json
import logging
import time

from sqlalchemy.exc import OperationalError

from app.services.analytics_refresh import HEAVY_KINDS, LIGHT_KINDS, SUMMARY_KINDS, enabled
from app.services.analytics_runtime import backfill_batch, maintain, ready, tables_ready
from app.services.analytics_rollup import backfill_rollups, rollups_ready
from app.services.async_analytics import AsyncAnalyticsRunner
from app.settings import Settings
from app.storage.async_database import AsyncDatabase, make_async_engine
from scripts.async_runtime import pause_until_stop
from scripts.runtime_support import stop_signal, log_failure


async def run(config, args):
    # Queue mutations need record locks, not repeatable-read gap locks over
    # absent statistic keys. Source aggregation still owns its explicit RR view.
    database = AsyncDatabase(make_async_engine(config.database_url).execution_options(isolation_level="READ COMMITTED"))
    background_workers = []
    role = getattr(args, "role", "all")
    try:
        if not await database.read(lambda db: tables_ready(db.connection())):
            raise ValueError("Run scripts.migrate_analytics --execute and scripts.migrate_analytics_rollups --execute before enabling analytics.")
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
        async def refresh_heavy_resources(*, summaries=False):
            heavy_runner = AsyncAnalyticsRunner(database, config)
            while not stop.is_set():
                if summaries and not await database.read(rollups_ready, config):
                    await pause_until_stop(stop, 1)
                    continue
                processed = await heavy_runner.run_once(slot_name="analysis_summary" if summaries else "analysis_heavy",
                    only_kinds=SUMMARY_KINDS if summaries else HEAVY_KINDS, default_statistics=summaries)
                await pause_until_stop(stop, .1 if processed else 1)
        if not args.once:
            if role in {"all", "projections"}:
                background_workers.append(asyncio.create_task(refresh_light_resources()))
            if role in {"all", "heavy"}:
                background_workers.append(asyncio.create_task(refresh_heavy_resources()))
            if role in {"all", "summaries"}:
                background_workers.append(asyncio.create_task(refresh_heavy_resources(summaries=True)))
        while not stop.is_set():
            for worker in background_workers:
                if worker.done():
                    await worker
            if role not in {"all", "projections"}:
                await pause_until_stop(stop, 1)
                continue
            if time.monotonic() - last_backfill >= 1 or args.once:
                await database.transaction(backfill_batch, config)
                await database.transaction(backfill_rollups, config)
                last_backfill = time.monotonic()
            if args.once:
                await database.transaction(maintain, config)
            jobs = await asyncio.gather(*(runner.run_once(
                only_kinds=None if args.once else ("product_projection", "event_projection"))
                                         for _ in range(config.analytics_concurrency)))
            if args.once:
                print(json.dumps({"processed": any(jobs), "jobs_processed": sum(jobs)}))
                return 0
            # These jobs only read/write our database. Source-site pacing lives
            # in the collection gate; avoid an artificial 20-jobs/s refill cap.
            await pause_until_stop(stop, .001 if any(jobs) else 1)
        return 0
    finally:
        for worker in background_workers:
            worker.cancel()
        if background_workers:
            await asyncio.gather(*background_workers, return_exceptions=True)
        await database.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--role", choices=("all", "projections", "summaries", "heavy"), default="all")
    args = parser.parse_args(argv)
    if args.once and args.role != "all":
        parser.error("--once runs all roles; omit --role")
    return asyncio.run(run(Settings.load(), args))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        raise SystemExit(main())
    except Exception as error:
        log_failure("analytics_failed", error)
        raise SystemExit(1)
