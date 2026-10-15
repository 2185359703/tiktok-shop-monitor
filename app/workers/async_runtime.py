"""Bounded coroutine tasks; HTTP, leases and SQL commits all yield to asyncio."""

import asyncio
import logging
import time
from contextlib import nullcontext

from app.models import TaskStatus, TaskType
from app.observability.metrics import Metrics
from app.queue.retry_policy import TaskFailure
from app.services.task_runtime import claim_task, defer_unclaimed_task, finalize_task, LeaseLost, renew_lease
from app.storage.database import session_scope
from app.workers.async_contracts import HandlerContext


logger = logging.getLogger(__name__)
LOCAL_TYPES = {TaskType.CHANGE_DETECTION, TaskType.REPORT_GENERATION}


class AsyncWorker:
    def __init__(self, settings, database, registry, gate, worker_id, metrics=None, *, concurrency=None):
        self.settings, self.database, self.registry = settings, database, registry
        self.gate, self.worker_id = gate, worker_id
        self.metrics = metrics or Metrics()
        self.slots = asyncio.Semaphore(concurrency or settings.worker_concurrency)
        self.report_slot = asyncio.Semaphore(1)

    async def transaction(self, function, *args, **kwargs):
        if hasattr(self.database, "transaction"):
            return await self.database.transaction(function, *args, **kwargs)
        # Compatibility for explicit sync factories injected by older tests and
        # one-off callers. Production entrypoints always pass AsyncDatabase.
        def execute():
            with session_scope(self.database) as session:
                return function(session, *args, **kwargs)
        return await asyncio.to_thread(execute)

    async def heartbeat(self, lease):
        while True:
            await asyncio.sleep(self.settings.heartbeat_seconds)
            await self.transaction(renew_lease, lease, self.settings)

    async def process(self, message):
        if message.task_type not in self.registry or message.task_type not in self.settings.enabled_task_types:
            return "reject"
        # Report materialization has a different memory cost from network waits.
        async with self.report_slot if message.task_type == TaskType.REPORT_GENERATION else nullcontext():
            return await self._process(message)

    async def _process(self, message):
        async with self.slots:
            paused = 0 if message.task_type in LOCAL_TYPES else await self.gate.pause_remaining()
            if paused:
                await self.transaction(defer_unclaimed_task, message.task_id, self.settings, paused)
                return "ack"
            lease = await self.transaction(claim_task, message.task_id, message.task_type, self.worker_id, self.settings)
            if lease is None:
                return "ack"
            context = HandlerContext(self.settings, self.gate, self.database)
            self.metrics.active.inc()
            started = time.monotonic()
            preparation = asyncio.create_task(self.registry[message.task_type].prepare(lease, context))
            heartbeat = asyncio.create_task(self.heartbeat(lease))
            result = None
            try:
                try:
                    finished, _ = await asyncio.wait({preparation, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
                    if heartbeat in finished:
                        await heartbeat
                    result = await preparation
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                    status = await self.transaction(finalize_task, lease, self.settings, result=result)
                except TaskFailure as error:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                    status = await self.transaction(finalize_task, lease, self.settings, failure=error.failure)
            except LeaseLost:
                logger.warning("lease_lost", extra={"task_id": lease.task_id, "worker_id": self.worker_id})
                return "ack"
            finally:
                preparation.cancel()
                heartbeat.cancel()
                await asyncio.gather(preparation, heartbeat, return_exceptions=True)
                self.metrics.active.dec()
                self.metrics.latency.labels(message.task_type.value).observe(time.monotonic() - started)
            self.metrics.completed.labels(message.task_type.value, status.value).inc()
            if status == TaskStatus.SUCCEEDED and result is not None:
                progress = result.checkpoint or {}
                self.metrics.snapshots.inc(progress.get("snapshots_written", 0))
                self.metrics.variant_snapshots.inc(progress.get("variant_snapshots_written", 0))
                self.metrics.changes.inc(progress.get("events_written", 0))
            logger.info("task_completed", extra={"task_id": lease.task_id, "task_type": lease.task_type.value,
                        "attempt": lease.attempt_no, "status": status.value, "worker_id": self.worker_id})
            return "reject" if status == TaskStatus.DEAD else "ack"
