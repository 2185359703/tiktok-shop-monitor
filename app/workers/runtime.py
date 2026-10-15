"""Prepare outside SQL, commit under lease fencing, then ACK on the MQ thread."""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event

from app.models import TaskStatus, TaskType
from app.queue.retry_policy import Failure, TaskFailure
from app.services.task_runtime import claim_task, defer_unclaimed_task, finalize_task, LeaseLost, renew_lease
from app.storage.database import session_scope
from app.workers.handlers import HandlerContext


logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, settings, factory, registry, gate, metrics, worker_id, stop=None):
        self.settings, self.factory, self.registry = settings, factory, registry
        self.gate, self.metrics, self.worker_id = gate, metrics, worker_id
        self.stop = stop if stop is not None else Event()

    def process(self, message, broker_poll=lambda: None) -> str:
        if message.task_type not in self.registry or message.task_type not in self.settings.enabled_task_types:
            return "reject"
        # Local comparisons remain executable while the public site is paused.
        paused = self.gate.pause_remaining() if message.task_type not in {TaskType.CHANGE_DETECTION, TaskType.REPORT_GENERATION} else 0
        if paused:
            with session_scope(self.factory) as session:
                defer_unclaimed_task(session, message.task_id, self.settings, paused)
            return "ack"
        with session_scope(self.factory) as session:
            lease = claim_task(session, message.task_id, message.task_type, self.worker_id, self.settings)
        if lease is None:
            # Duplicate, stale, terminal, another owner's lease or future retry.
            return "ack"
        self.metrics.active.inc()
        started, last_heartbeat = time.monotonic(), time.monotonic()
        context = HandlerContext(self.settings, self.gate, self.stop, session_factory=self.factory)
        result, failure = None, None
        try:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="collector") as pool:
                future = pool.submit(self.registry[message.task_type].prepare, lease, context)
                while True:
                    if self.stop.is_set():
                        raise LeaseLost("Worker 正在停止，保留任务等待租约恢复。")
                    try:
                        result = future.result(timeout=0.2)
                        break
                    except TimeoutError:
                        if future.done():
                            # A handler-raised TimeoutError is not a polling timeout.
                            future.result()
                        broker_poll()
                        if time.monotonic() - last_heartbeat >= self.settings.heartbeat_seconds:
                            with session_scope(self.factory) as session:
                                renew_lease(session, lease, self.settings)
                            last_heartbeat = time.monotonic()
            try:
                with session_scope(self.factory) as session:
                    status = finalize_task(session, lease, self.settings, result=result)
            except TaskFailure as error:
                # Domain validation rolled back: record only the failed attempt.
                failure = error.failure
        except TaskFailure as error:
            failure = error.failure
        except LeaseLost:
            logger.warning("lease_lost", extra={"task_id": lease.task_id, "worker_id": self.worker_id})
            return "ack"
        except Exception as error:
            # Infrastructure failure: no ACK. Closing the connection requeues the
            # notification; the durable lease and reaper recover the SQL task.
            logger.error("worker_infrastructure_error", extra={"task_id": lease.task_id,
                                                               "error_type": type(error).__name__})
            raise
        finally:
            self.metrics.active.dec()
            self.metrics.latency.labels(message.task_type.value).observe(time.monotonic() - started)
        if failure is not None:
            with session_scope(self.factory) as session:
                status = finalize_task(session, lease, self.settings, failure=failure)
        self.metrics.completed.labels(message.task_type.value, status.value).inc()
        if status == TaskStatus.SUCCEEDED and result is not None:
            progress = result.checkpoint or {}
            self.metrics.snapshots.inc(progress.get("snapshots_written", 0))
            self.metrics.variant_snapshots.inc(progress.get("variant_snapshots_written", 0))
            self.metrics.changes.inc(progress.get("events_written", 0))
        logger.info("task_completed", extra={"task_id": lease.task_id, "task_type": lease.task_type.value,
                                             "attempt": lease.attempt_no, "status": status.value,
                                             "worker_id": self.worker_id})
        return "reject" if status == TaskStatus.DEAD else "ack"
