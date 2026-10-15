"""Native coroutine implementation of the shared analytics/report lease."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

from app.queue.retry_policy import Failure, TaskFailure
from app.services.analytics_refresh import enabled, job_row, LEASE_SECONDS, LeaseLost, now, scope


@asynccontextmanager
async def heavy_slot(database, config):
    if database is None or not enabled():
        yield
        return
    parameters, token = {**scope(config), "name": "heavy_job_slot"}, str(uuid4())
    def acquire(db):
        row, stamp = job_row(db, "control", parameters), now(db)
        if row.lease_until and row.lease_until > stamp:
            raise TaskFailure(Failure("analytics_heavy_busy", "已有统计或报告正在生成，稍后重试。", True, 15))
        row.lease_token, row.lease_until = token, stamp + timedelta(seconds=LEASE_SECONDS)
    def owned(db, *, renew=False):
        row, stamp = job_row(db, "control", parameters), now(db)
        if row.lease_token != token or row.lease_until is None or row.lease_until <= stamp:
            raise LeaseLost("heavy_slot_lost")
        if renew:
            row.lease_until = stamp + timedelta(seconds=LEASE_SECONDS)
    def release(db):
        row = job_row(db, "control", parameters)
        if row.lease_token == token:
            row.lease_token = row.lease_until = None
    await database.transaction(acquire)
    async def heartbeat():
        while True:
            await asyncio.sleep(10)
            await database.transaction(owned, renew=True)
    renewal = asyncio.create_task(heartbeat())
    try:
        yield
        if renewal.done():
            await renewal
        await database.transaction(owned)
    finally:
        renewal.cancel()
        await asyncio.gather(renewal, return_exceptions=True)
        await database.transaction(release)
