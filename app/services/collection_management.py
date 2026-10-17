"""Bounded, transaction-owned collection management using the existing task runtime."""

from copy import deepcopy
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.exc import IntegrityError

from app.crawlers.product_list import product_id_from_url
from app.crawlers.public_shop import shop_id_from_url
from app.models import CollectionControl, CollectionOperation, CollectionPlan, CollectionRun, CollectionRunTask, CrawlTask, Product, Shop, TaskStatus, TaskType
from app.services.seed_importer import fingerprint
from app.services.shop_profile import ensure_shop_profile_task
from app.services.snapshot_service import ensure_snapshot_task
from app.services.task_runtime import database_now


PLAN_TYPES = (TaskType.SHOP_PROFILE, TaskType.PRODUCT_SNAPSHOT)
RETRY_TYPES = (*PLAN_TYPES, TaskType.PRODUCT_DISCOVERY, TaskType.PRODUCT_DETAIL)
LOCAL_TYPES = {TaskType.CHANGE_DETECTION, TaskType.REPORT_GENERATION}
ACTIVE_STATES = (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.RETRY_WAITING, TaskStatus.ABANDONED)
MIN_INTERVAL = {TaskType.SHOP_PROFILE: 3600, TaskType.PRODUCT_SNAPSHOT: 60}
MAX_RESOURCES = 100
MAX_INTERVAL = 604800


class ManagementError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def market(model, settings):
    return model.platform == settings.platform, model.region == settings.region


def is_collection_paused(session, settings, *, lock=False):
    query = select(CollectionControl.paused).where(*market(CollectionControl, settings))
    if lock:
        # Shared locking reads serialize the claim boundary against a pause;
        # they do not serialize independent workers against one another.
        query = query.with_for_update(read=True)
    return bool(session.scalar(query))


def read_control(session, settings):
    row = session.scalar(select(CollectionControl).where(*market(CollectionControl, settings)))
    return {"platform": settings.platform, "region": settings.region,
            "paused": bool(row.paused) if row is not None else False,
            "updated_at": row.updated_at if row is not None else None,
            "writes_enabled": settings.api_token is not None,
            "supported_task_types": list(PLAN_TYPES), "enabled_task_types": list(settings.enabled_task_types),
            "limits": {"max_resources": min(MAX_RESOURCES, settings.dispatch_batch_size), "min_interval_seconds": MIN_INTERVAL,
                       "max_interval_seconds": MAX_INTERVAL}}


def audit(session, settings, action, target_type, target_id, **details):
    session.add(CollectionOperation(platform=settings.platform, region=settings.region, action=action,
        target_type=target_type, target_id=str(target_id), details=details))


def set_control(session, settings, paused):
    statement = insert(CollectionControl).values(platform=settings.platform, region=settings.region, paused=False)
    session.execute(statement.on_duplicate_key_update(id=CollectionControl.id))
    row = session.scalar(select(CollectionControl).where(*market(CollectionControl, settings))
                         .with_for_update().execution_options(populate_existing=True))
    if row.paused != paused:
        row.paused = paused
        audit(session, settings, "collection_paused" if paused else "collection_resumed", "market", settings.region,
              paused=paused, inflight_policy="finish")
    session.flush()
    return read_control(session, settings)


def plan_summary(plan):
    return {"id": str(plan.id), "name": plan.name, "task_type": plan.task_type,
            "resource_ids": plan.resource_ids, "schedule_mode": plan.schedule_mode,
            "interval_seconds": plan.interval_seconds, "enabled": plan.enabled,
            "next_run_at": plan.next_run_at, "last_run_at": plan.last_run_at,
            "created_at": plan.created_at, "updated_at": plan.updated_at}


def get_plan(session, settings, plan_id, *, lock=False):
    query = select(CollectionPlan).where(CollectionPlan.id == plan_id, *market(CollectionPlan, settings))
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = session.scalar(query)
    if row is None:
        raise ManagementError("collection_plan_not_found", 404)
    return row


def validate_interval(kind, mode, interval):
    if kind not in PLAN_TYPES:
        raise ManagementError("unsupported_plan_type", 422)
    if mode not in {"once", "interval"} or (mode == "once" and interval is not None) or (
            mode == "interval" and (type(interval) is not int or not MIN_INTERVAL[kind] <= interval <= MAX_INTERVAL)):
        raise ManagementError("invalid_plan_interval", 422)


def validate_resources(session, settings, kind, resource_ids, *, lock=False):
    if kind not in settings.enabled_task_types:
        raise ManagementError("collector_not_enabled")
    if not resource_ids or len(resource_ids) > min(MAX_RESOURCES, settings.dispatch_batch_size) or len(set(resource_ids)) != len(resource_ids):
        raise ManagementError("invalid_plan_resources", 422)
    model = Shop if kind == TaskType.SHOP_PROFILE else Product
    query = select(model).where(model.id.in_([int(value) for value in resource_ids]), *market(model, settings)).order_by(model.id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    rows = session.scalars(query).all()
    if len(rows) != len(resource_ids):
        raise ManagementError("invalid_plan_resources", 422)
    for row in rows:
        if kind == TaskType.SHOP_PROFILE:
            valid = row.canonical_url and shop_id_from_url(row.canonical_url) == row.platform_shop_id
        else:
            valid = (row.last_success_at is not None and row.currency == "USD" and row.pricing_context == "public_listed"
                     and product_id_from_url(row.product_url) == row.platform_product_id)
        if not valid:
            raise ManagementError("invalid_plan_resources", 422)
    return rows


def create_plan(session, settings, definition, request_id):
    key = fingerprint({"platform": settings.platform, "region": settings.region, "create_plan": str(request_id)})
    definition_hash = fingerprint(definition)
    existing = session.scalar(select(CollectionPlan).where(CollectionPlan.request_key == key))
    if existing is not None:
        if existing.request_hash != definition_hash:
            raise ManagementError("idempotency_conflict")
        return existing, False
    kind = TaskType(definition["task_type"])
    validate_interval(kind, definition["schedule_mode"], definition["interval_seconds"])
    validate_resources(session, settings, kind, definition["resource_ids"])
    now = database_now(session)
    plan = CollectionPlan(platform=settings.platform, region=settings.region, request_key=key, request_hash=definition_hash,
        **{**definition, "task_type": kind}, next_run_at=now if definition["enabled"] else None)
    try:
        # Do not lock a missing unique key: concurrent identical submissions
        # would otherwise deadlock while upgrading their shared gap locks.
        with session.begin_nested():
            session.add(plan)
            session.flush()
    except IntegrityError as error:
        if getattr(error.orig, "args", (None,))[0] != 1062:
            raise
        existing = session.scalar(select(CollectionPlan).where(CollectionPlan.request_key == key).with_for_update())
        if existing is None:
            raise
        if existing.request_hash != definition_hash:
            raise ManagementError("idempotency_conflict") from None
        return existing, False
    audit(session, settings, "plan_created", "plan", plan.id, resource_count=len(plan.resource_ids),
          task_type=kind.value, schedule_mode=plan.schedule_mode, enabled=plan.enabled)
    return plan, True


def update_plan(session, settings, plan_id, changes):
    plan = get_plan(session, settings, plan_id, lock=True)
    interval = changes.get("interval_seconds", plan.interval_seconds)
    validate_interval(plan.task_type, plan.schedule_mode, interval)
    if "resource_ids" in changes or changes.get("enabled") is True:
        validate_resources(session, settings, plan.task_type, changes.get("resource_ids", plan.resource_ids))
    enabled_before = plan.enabled
    for key in ("name", "resource_ids", "interval_seconds", "enabled"):
        if key in changes:
            setattr(plan, key, changes[key])
    now = database_now(session)
    if not plan.enabled:
        plan.next_run_at = None
    elif not enabled_before or plan.next_run_at is None:
        plan.next_run_at = now
    elif "interval_seconds" in changes:
        plan.next_run_at = now + timedelta(seconds=plan.interval_seconds)
    audit(session, settings, "plan_updated", "plan", plan.id, fields=sorted(changes),
          resource_count=len(plan.resource_ids), enabled=plan.enabled)
    session.flush()
    return plan


def plan_is_busy(session, plan):
    return session.scalar(select(CrawlTask.id).join(CollectionRunTask, CollectionRunTask.task_id == CrawlTask.id)
        .join(CollectionRun, CollectionRun.id == CollectionRunTask.run_id).where(CollectionRun.plan_id == plan.id,
            CrawlTask.status.in_(ACTIVE_STATES)).limit(1).with_for_update(read=True)) is not None


def run_summary(session, run, *, include_tasks=False):
    # Join only explicit members. A plan's root completion does not claim all
    # automatic change-detection or other downstream tasks have completed.
    query = select(CrawlTask).join(CollectionRunTask, CollectionRunTask.task_id == CrawlTask.id).where(
        CollectionRunTask.run_id == run.id, CrawlTask.platform == run.platform, CrawlTask.region == run.region)
    roots = query.where(CollectionRunTask.member_role == "root")
    retries = query.where(CollectionRunTask.member_role == "retry")
    counts = dict(session.execute(roots.with_only_columns(CrawlTask.status, func.count()).group_by(CrawlTask.status)).all())
    retry_counts = dict(session.execute(retries.with_only_columns(CrawlTask.status, func.count()).group_by(CrawlTask.status)).all())
    total = sum(counts.values())
    if run.error_code or total == 0:
        status = "failed"
    elif counts.get(TaskStatus.RUNNING, 0):
        status = "running"
    elif any(counts.get(value, 0) for value in ACTIVE_STATES):
        status = "queued"
    elif counts.get(TaskStatus.SUCCEEDED, 0) == total:
        status = "succeeded"
    elif counts.get(TaskStatus.SUCCEEDED, 0):
        status = "partial"
    else:
        status = "failed"
    result = {"id": str(run.id), "plan_id": str(run.plan_id), "trigger": run.trigger_kind,
              "scheduled_for": run.scheduled_for, "created_at": run.created_at, "status": status,
              "requested_count": run.requested_count, "task_count": total, "status_counts": counts,
              "retry_task_count": sum(retry_counts.values()), "retry_status_counts": retry_counts,
              "active_retry_count": sum(retry_counts.get(value, 0) for value in ACTIVE_STATES),
              "coverage_scope": "root_tasks", "error_code": run.error_code}
    if include_tasks:
        from app.api.routes import task_summary
        result["tasks"] = [task_summary(task) for task in session.scalars(roots.order_by(CrawlTask.id)).all()]
        result["retry_tasks"] = [task_summary(task) for task in session.scalars(retries.order_by(CrawlTask.id)).all()]
    return result


def _create_run(session, settings, plan, trigger, window, run_key):
    run = CollectionRun(platform=settings.platform, region=settings.region, plan_id=plan.id, run_key=run_key,
        trigger_kind=trigger, scheduled_for=window, requested_count=len(plan.resource_ids))
    session.add(run)
    session.flush()
    # Invalidated resources produce an honest failed run, never half a batch.
    try:
        with session.begin_nested():
            resources = validate_resources(session, settings, plan.task_type, plan.resource_ids, lock=True)
            for resource in resources:
                if plan.task_type == TaskType.SHOP_PROFILE:
                    ensure_shop_profile_task(session, resource, window)
                    task = session.scalar(select(CrawlTask).where(*market(CrawlTask, settings),
                        CrawlTask.task_type == TaskType.SHOP_PROFILE, CrawlTask.resource_id == str(resource.id),
                        CrawlTask.scheduled_for == window).order_by(CrawlTask.id).limit(1))
                else:
                    task, _ = ensure_snapshot_task(session, resource, window)
                session.add(CollectionRunTask(run_id=run.id, task_id=task.id))
            session.flush()
    except ManagementError as error:
        run.error_code = error.code
    except ValueError:
        run.error_code = "invalid_plan_resources"
    plan.last_run_at = database_now(session)
    session.flush()
    return run


def run_plan(session, settings, plan_id, request_id):
    paused = is_collection_paused(session, settings, lock=True)
    plan = get_plan(session, settings, plan_id, lock=True)
    key = fingerprint({"plan": str(plan.id), "manual": str(request_id)})
    existing = session.scalar(select(CollectionRun).where(CollectionRun.run_key == key).with_for_update())
    if existing is not None:
        return existing, False
    if paused:
        raise ManagementError("collection_paused")
    if plan.task_type not in settings.enabled_task_types:
        raise ManagementError("collector_not_enabled")
    if plan_is_busy(session, plan):
        raise ManagementError("collection_plan_busy")
    now = database_now(session)
    run = _create_run(session, settings, plan, "manual", now, key)
    if plan.schedule_mode == "once":
        plan.enabled, plan.next_run_at = False, None
    elif plan.enabled:
        plan.next_run_at = now + timedelta(seconds=plan.interval_seconds)
    audit(session, settings, "plan_run", "plan", plan.id, run_id=str(run.id), trigger="manual", resource_count=run.requested_count)
    session.flush()
    return run, True


def enqueue_due_collection_plans(session, settings, *, limit=100):
    """At most `limit` root tasks per tick; missed periods coalesce once."""
    if is_collection_paused(session, settings, lock=True):
        return 0
    now = database_now(session)
    plans = session.scalars(select(CollectionPlan).where(*market(CollectionPlan, settings),
        CollectionPlan.enabled.is_(True), CollectionPlan.task_type.in_(settings.enabled_task_types),
        CollectionPlan.next_run_at <= now).order_by(CollectionPlan.next_run_at, CollectionPlan.id)
        .limit(10).with_for_update(skip_locked=True)).all()
    roots = 0
    for plan in plans:
        # A reduced deployment budget invalidates an older oversized plan with
        # a visible failed run; it must not silently remain due forever.
        oversized = len(plan.resource_ids) > min(MAX_RESOURCES, settings.dispatch_batch_size)
        if not oversized and roots + len(plan.resource_ids) > limit:
            continue
        if plan_is_busy(session, plan):
            # Coalesce while the previous roots remain unfinished; never build
            # an unbounded backlog of observations for the same plan.
            plan.next_run_at = now + timedelta(seconds=plan.interval_seconds or 60)
            continue
        due = plan.next_run_at
        if plan.schedule_mode == "interval":
            steps = max(0, int((now - due).total_seconds()) // plan.interval_seconds)
            window = due + timedelta(seconds=steps * plan.interval_seconds)
            plan.next_run_at = window + timedelta(seconds=plan.interval_seconds)
        else:
            window = due
            plan.enabled, plan.next_run_at = False, None
        key = fingerprint({"plan": str(plan.id), "scheduled": window.isoformat()})
        if session.scalar(select(CollectionRun.id).where(CollectionRun.run_key == key)) is None:
            run = _create_run(session, settings, plan, "scheduled", window, key)
            roots += 0 if run.error_code else run.requested_count
    session.flush()
    return roots


def cancel_task(session, settings, task_id):
    task = session.scalar(select(CrawlTask).where(*market(CrawlTask, settings), CrawlTask.task_id == task_id)
                          .with_for_update().execution_options(populate_existing=True))
    if task is None:
        raise ManagementError("task_not_found", 404)
    if task.status == TaskStatus.CANCELLED:
        return task, False
    if task.status not in {TaskStatus.PENDING, TaskStatus.RETRY_WAITING, TaskStatus.ABANDONED} or task.lease_token is not None:
        raise ManagementError("invalid_task_transition")
    previous = task.status.value
    task.status, task.finished_at = TaskStatus.CANCELLED, database_now(session)
    audit(session, settings, "task_cancelled", "task", task.task_id, previous_status=previous)
    session.flush()
    return task, True


def retry_task(session, settings, task_id):
    is_collection_paused(session, settings, lock=True)
    source_id = session.scalar(select(CrawlTask.id).where(*market(CrawlTask, settings), CrawlTask.task_id == task_id))
    if source_id is None:
        raise ManagementError("task_not_found", 404)
    lineage = session.execute(select(CollectionRun.id, CollectionRun.plan_id).join(CollectionRunTask,
        CollectionRunTask.run_id == CollectionRun.id).where(CollectionRunTask.task_id == source_id,
            *market(CollectionRun, settings))).all()
    run_ids = [run_id for run_id, _ in lineage]
    # Match scheduler/manual-run ordering: market -> plans -> task. A retry
    # cannot race a newer run past the active-work check for the same plan.
    plans = [get_plan(session, settings, plan_id, lock=True) for plan_id in sorted({plan_id for _, plan_id in lineage})]
    original = session.scalar(select(CrawlTask).where(*market(CrawlTask, settings), CrawlTask.task_id == task_id)
                              .with_for_update().execution_options(populate_existing=True))
    if original is None:
        raise ManagementError("task_not_found", 404)
    if original.task_type not in RETRY_TYPES:
        raise ManagementError("unsupported_task_retry")
    if original.task_type not in settings.enabled_task_types:
        raise ManagementError("collector_not_enabled")
    if original.status not in {TaskStatus.DEAD, TaskStatus.CANCELLED} or original.lease_token is not None:
        raise ManagementError("invalid_task_transition")
    key = fingerprint({"retry_of": original.task_id, "version": 1, "platform": settings.platform, "region": settings.region})
    existing = session.scalar(select(CrawlTask).where(CrawlTask.idempotency_key == key).with_for_update())
    if existing is not None:
        return existing, False
    for plan in plans:
        newer_active = session.scalar(select(CrawlTask.id).join(CollectionRunTask, CollectionRunTask.task_id == CrawlTask.id)
            .join(CollectionRun, CollectionRun.id == CollectionRunTask.run_id).where(CollectionRun.plan_id == plan.id,
                CollectionRun.id.not_in(run_ids), CrawlTask.status.in_(ACTIVE_STATES)).limit(1).with_for_update(read=True))
        if newer_active is not None:
            raise ManagementError("collection_plan_busy")
    payload = deepcopy(original.payload)
    payload["retry_of_task_id"] = original.task_id
    if original.task_type == TaskType.PRODUCT_DISCOVERY:
        payload.setdefault("root_task_id", original.task_id)
    task = CrawlTask(platform=original.platform, region=original.region, task_type=original.task_type,
        resource_type=original.resource_type, resource_id=original.resource_id, idempotency_key=key,
        payload=payload, checkpoint=deepcopy(original.checkpoint), priority=original.priority,
        max_attempts=3, scheduled_for=original.scheduled_for, available_at=database_now(session))
    session.add(task)
    session.flush()
    session.add_all(CollectionRunTask(run_id=run_id, task_id=task.id, member_role="retry") for run_id in run_ids)
    audit(session, settings, "task_retried", "task", original.task_id, retry_task_id=task.task_id,
          history_preserved=True, max_attempts=task.max_attempts)
    return task, True
