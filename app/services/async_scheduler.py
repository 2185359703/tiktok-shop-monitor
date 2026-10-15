"""Recover leases and redispatch due database rows; no SQL/MQ dual-write gap."""

from sqlalchemy import and_, func, or_, select

from app.models import CrawlTask, TaskStatus, TaskType
from app.queue.contracts import TaskMessage
from app.services.task_runtime import recover_expired_leases
from app.services.snapshot_service import enqueue_due_snapshots
from app.services.shop_profile import enqueue_due_shop_profiles
from app.services.image_backfill import BACKFILL_MODES, capture_mode_expression


READY = (TaskStatus.PENDING, TaskStatus.RETRY_WAITING, TaskStatus.ABANDONED)


def task_counts(session, settings):
    return session.execute(select(CrawlTask.task_type, CrawlTask.status, func.count()).where(
        CrawlTask.platform == settings.platform, CrawlTask.region == settings.region,
    ).group_by(CrawlTask.task_type, CrawlTask.status)).all()


class AsyncScheduler:
    def __init__(self, settings, database, broker, gate, metrics):
        self.settings, self.database, self.broker, self.gate, self.metrics = settings, database, broker, gate, metrics
        self.cursors = {}
        self.rotation = 0
        self.detail_rotation = 0

    async def dispatch_lane(self, kind, mode, budget, scan_budget):
        """Share one broker queue's budget while keeping capture modes fair."""
        settings = self.settings
        cursor_key = (kind, mode)
        published = scanned = 0
        while scanned < scan_budget and published < budget:
            def read_rows(session):
                query = select(CrawlTask).where(
                    CrawlTask.platform == settings.platform, CrawlTask.region == settings.region,
                    CrawlTask.task_type == kind, CrawlTask.status.in_(READY),
                    CrawlTask.available_at <= func.utc_timestamp(6), CrawlTask.lease_token.is_(None),
                    CrawlTask.attempt_count < CrawlTask.max_attempts,
                )
                if kind == TaskType.PRODUCT_DETAIL:
                    query = query.where(capture_mode_expression().in_(mode) if isinstance(mode, tuple)
                                        else capture_mode_expression() == mode)
                cursor = self.cursors.get(cursor_key)
                if cursor:
                    priority, identifier = cursor
                    query = query.where(or_(CrawlTask.priority < priority,
                                           and_(CrawlTask.priority == priority, CrawlTask.id > identifier)))
                rows = session.scalars(query.order_by(CrawlTask.priority.desc(), CrawlTask.id)
                    .limit(min(100, scan_budget - scanned))).all()
                return rows
            rows = await self.database.read(read_rows)
            if not rows:
                self.cursors[cursor_key] = None
                break
            for task in rows:
                self.cursors[cursor_key] = task.priority, task.id
                scanned += 1
                key = f"{task.task_id}:{task.attempt_count}:{task.available_at.isoformat()}"
                token = await self.gate.reserve_publish(key)
                if token is None:
                    continue
                try:
                    await self.broker.publish(TaskMessage(task.task_id, task.task_type))
                except Exception:
                    await self.gate.release_publish(key, token)
                    self.cursors[cursor_key] = None
                    raise
                published += 1
                self.metrics.published.labels(task.task_type.value).inc()
                if published >= budget:
                    break
        return published, scanned

    async def dispatch_once(self) -> dict:
        settings = self.settings
        def recover(session):
            recovered = recover_expired_leases(session, settings)
            scheduled = enqueue_due_snapshots(session, settings, limit=settings.dispatch_batch_size)
            profiles_scheduled = enqueue_due_shop_profiles(session, settings, limit=settings.dispatch_batch_size)
            counts = task_counts(session, settings)
            return recovered, scheduled, profiles_scheduled, counts
        recovered, scheduled, profiles_scheduled, counts = await self.database.transaction(recover)
        self.metrics.recovered.inc(recovered)
        self.metrics.queue_depth.clear()
        for kind, status, count in counts:
            self.metrics.queue_depth.labels(kind.value, status.value).set(count)
        enabled = settings.enabled_task_types
        if enabled and await self.gate.pause_remaining():
            enabled = tuple(kind for kind in enabled if kind in {TaskType.CHANGE_DETECTION, TaskType.REPORT_GENERATION})
        if not enabled:
            return {"recovered": recovered, "published": 0, "snapshots_scheduled": scheduled,
                    "shop_profiles_scheduled": profiles_scheduled}
        published = scanned = 0
        enabled = tuple(enabled)
        start = self.rotation % len(enabled)
        self.rotation += 1
        ordered = enabled[start:] + enabled[:start]
        backpressured = []
        per_kind_budget = max(1, settings.dispatch_batch_size // len(enabled))
        per_kind_scan = max(1, settings.dispatch_scan_limit // len(enabled))
        for kind in ordered:
            if scanned >= settings.dispatch_scan_limit or published >= settings.dispatch_batch_size:
                break
            ready = await self.broker.ready_count(kind)
            high = (settings.dispatch_local_queue_high_watermark
                    if kind in {TaskType.CHANGE_DETECTION, TaskType.REPORT_GENERATION}
                    else settings.dispatch_queue_high_watermark)
            budget = min(per_kind_budget, high - ready, settings.dispatch_batch_size - published)
            if budget <= 0:
                backpressured.append(kind.value)
                continue
            modes = (None,)
            if kind == TaskType.PRODUCT_DETAIL:
                modes = ("full", BACKFILL_MODES) if settings.image_backfill_enabled else ("full",)
                first = self.detail_rotation % len(modes)
                self.detail_rotation += 1
                modes = modes[first:] + modes[:first]
            kind_published = kind_scanned = 0
            for position, mode in enumerate(modes):
                remaining_lanes = len(modes) - position
                remaining_budget = budget - kind_published
                remaining_scan = min(per_kind_scan - kind_scanned, settings.dispatch_scan_limit - scanned)
                if remaining_budget <= 0 or remaining_scan <= 0:
                    break
                lane_budget = max(1, remaining_budget // remaining_lanes)
                lane_scan = max(1, remaining_scan // remaining_lanes)
                sent, inspected = await self.dispatch_lane(kind, mode, lane_budget, lane_scan)
                published += sent
                scanned += inspected
                kind_published += sent
                kind_scanned += inspected
        return {"recovered": recovered, "published": published, "snapshots_scheduled": scheduled,
                "shop_profiles_scheduled": profiles_scheduled,
                "backpressured_types": backpressured}
