"""Concurrent derived-data jobs with native async SQL and coroutine heartbeats."""

import asyncio
import logging

from app.services.analytics_runtime import source_reader
from app.services.analytics_projection import build_event, build_product, publish_event, publish_product
from app.services.analytics_refresh import LeaseLost, claim, complete, failed, heartbeat
from app.services.analytics_snapshot import build_snapshot, publish_snapshot
from app.services.analytics_refresh import STAT_KINDS
from app.services.async_heavy_slot import heavy_slot


class AsyncAnalyticsRunner:
    def __init__(self, database, config):
        self.database, self.config, self.steps = database, config, 0

    async def renew(self, lease):
        while True:
            await asyncio.sleep(10)
            await self.database.transaction(heartbeat, lease)

    def build(self, lease):
        with source_reader(self.database.sync_factory) as db:
            if lease.kind == "product_projection":
                data = build_product(db, self.config, int(lease.parameters["product_id"]), lease.revision)
                return lambda session: publish_product(session, self.config, data)
            if lease.kind == "event_projection":
                data = build_event(db, self.config, int(lease.parameters["event_id"]))
                return lambda session: publish_event(session, self.config, data)
            data, stamp = build_snapshot(db, self.config, lease.kind, lease.parameters)
            return lambda session: publish_snapshot(session, lease.kind, lease.parameters, lease.revision, data, stamp)

    async def run_once(self):
        prefer_statistics = self.steps % 8 == 7
        slot_name = f"analysis_slot:{self.steps % self.config.analytics_concurrency}"
        self.steps += 1
        lease = await self.database.transaction(claim, self.config, prefer_statistics=prefer_statistics, slot_name=slot_name)
        if lease is None:
            return False
        async def prepare():
            if lease.kind in STAT_KINDS:
                async with heavy_slot(self.database, self.config):
                    return await self.database.call(self.build, lease)
            return await self.database.call(self.build, lease)
        preparation = asyncio.create_task(prepare())
        renewal = asyncio.create_task(self.renew(lease))
        try:
            done, _ = await asyncio.wait({preparation, renewal}, return_when=asyncio.FIRST_COMPLETED)
            if renewal in done:
                await renewal
            publish = await preparation
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)
            await self.database.transaction(complete, lease, publish)
        except Exception as error:
            logging.getLogger(__name__).warning("analytics_refresh_failed", extra={"kind": lease.kind, "error_type": type(error).__name__})
            try:
                await self.database.transaction(failed, lease, error)
            except LeaseLost:
                pass
        finally:
            preparation.cancel()
            renewal.cancel()
            await asyncio.gather(preparation, renewal, return_exceptions=True)
        return True
