"""Concurrent derived-data jobs with native async SQL and coroutine heartbeats."""

import asyncio
import logging
import re

from app.services.analytics_runtime import source_reader
from app.services.analytics_projection import build_event, build_products, publish_event, publish_product
from app.services.analytics_refresh import LeaseLost, claim_batch, complete_batch, failed_batch, heartbeat_batch
from app.services.analytics_snapshot import build_snapshots, publish_snapshot
from app.services.analytics_refresh import HEAVY_KINDS, STAT_KINDS, SUMMARY_KINDS
from app.services.analytics_rollup import rollups_ready, unfiltered
from app.services.async_heavy_slot import heavy_slot


class AsyncAnalyticsRunner:
    def __init__(self, database, config):
        self.database, self.config, self.steps = database, config, 0

    async def renew(self, leases):
        while True:
            await asyncio.sleep(10)
            await self.database.transaction(heartbeat_batch, leases)

    def build(self, leases):
        lease = leases[0]
        with source_reader(self.database.sync_factory,
                           select_timeout_ms=30000 if lease.kind in STAT_KINDS else 3000) as db:
            if lease.kind == "product_projection":
                data = build_products(db, self.config, {int(item.parameters["product_id"]): item.revision for item in leases})
                return lambda session: publish_product(session, self.config, data)
            if lease.kind == "event_projection":
                data = build_event(db, self.config, int(lease.parameters["event_id"]))
                return lambda session: publish_event(session, self.config, data)
            results = build_snapshots(db, self.config, lease.kind, [item.parameters for item in leases])
            def publish(session):
                for item, (data, stamp) in zip(leases, results):
                    publish_snapshot(session, item.kind, item.parameters, item.revision, data, stamp)
            return publish

    async def run_once(self, *, slot_name=None, only_kinds=None, exclude_kinds=(), default_statistics=None):
        prefer_statistics = self.steps % 8 == 7
        slot_name = slot_name or f"analysis_slot:{self.steps % self.config.analytics_concurrency}"
        self.steps += 1
        leases = await self.database.transaction(claim_batch, self.config, prefer_statistics=prefer_statistics, slot_name=slot_name,
                                                only_kinds=only_kinds, exclude_kinds=exclude_kinds,
                                                default_statistics=default_statistics)
        if not leases:
            return False
        lease = leases[0]
        async def prepare():
            summary = (lease.kind in SUMMARY_KINDS and unfiltered(lease.parameters)
                       and await self.database.read(rollups_ready, self.config))
            if lease.kind in HEAVY_KINDS and not summary:
                async with heavy_slot(self.database, self.config):
                    return await self.database.call(self.build, leases)
            return await self.database.call(self.build, leases)
        preparation = asyncio.create_task(prepare())
        renewal = asyncio.create_task(self.renew(leases))
        phase = "prepare"
        try:
            done, _ = await asyncio.wait({preparation, renewal}, return_when=asyncio.FIRST_COMPLETED)
            if renewal in done:
                await renewal
            publish = await preparation
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)
            phase = "publish"
            await self.database.transaction(complete_batch, leases, publish)
        except Exception as error:
            args = getattr(getattr(error, "orig", None), "args", ())
            code = args[0] if args and isinstance(args[0], int) else None
            tables = sorted(set(re.findall(r"(?:FROM|JOIN|UPDATE|INTO)\s+([a-z_]+)",
                                           getattr(error, "statement", "") or "", re.I)))
            logging.getLogger(__name__).warning("analytics_refresh_failed kind=%s phase=%s error=%s database_code=%s tables=%s",
                                               lease.kind, phase, type(error).__name__, code, tables)
            try:
                await self.database.transaction(failed_batch, leases, error)
            except LeaseLost:
                pass
        finally:
            preparation.cancel()
            renewal.cancel()
            await asyncio.gather(preparation, renewal, return_exceptions=True)
        return True
