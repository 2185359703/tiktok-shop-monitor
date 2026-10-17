"""Durable batch cancellation with shared-task and in-flight completion rules."""

from sqlalchemy import exists, func, select
from sqlalchemy.orm import aliased

from app.models import CrawlTask, TaskStatus
from app.models.collection_launch import CollectionLaunch as Launch, CollectionLaunchTask as Member
from app.services.collection_management import ACTIVE_STATES, ManagementError, audit, market
from app.services.task_runtime import database_now


def lock_launch_scope(session, settings, task_uuid):
    """A shared live owner may allow work; cancellation serializes at this gate.

    Lock a live launch before locking its task. When all owners have cancelled,
    an existing membership distinguishes cancelled work from untracked work.
    A concurrent new owner can retain the cancelled task as historical evidence
    and explicitly retry it; it never silently restarts the old task.
    """
    task_id = session.scalar(select(CrawlTask.id).where(*market(CrawlTask, settings), CrawlTask.task_id == task_uuid))
    if task_id is None:
        return True
    # Keep crawl_tasks out of the locking join. Two duplicate deliveries must
    # not both hold shared task locks and then deadlock upgrading to a lease.
    query = select(Launch.id).join(Member, Member.launch_id == Launch.id).where(
        *market(Launch, settings), Member.task_id == task_id)
    if session.scalar(query.where(Launch.cancel_requested_at.is_(None)).order_by(Launch.id).limit(1).with_for_update(read=True)) is not None:
        return True
    return session.scalar(query.order_by(Launch.id).limit(1).with_for_update(read=True)) is None


def live_owner(settings):
    member, launch = aliased(Member), aliased(Launch)
    return exists(select(launch.id).join(member, member.launch_id == launch.id).where(
        member.task_id == CrawlTask.id, *market(launch, settings), launch.cancel_requested_at.is_(None)))


def owned_tasks(settings, launch_id, *, exclusive=False):
    query = select(CrawlTask).join(Member, Member.task_id == CrawlTask.id).where(
        Member.launch_id == launch_id, *market(CrawlTask, settings))
    return query.where(~live_owner(settings)) if exclusive else query


def finish_cancellation(session, settings, launch, *, limit=100):
    query = owned_tasks(settings, launch.id, exclusive=True)
    tasks = session.scalars(query.where(CrawlTask.status.in_((TaskStatus.PENDING, TaskStatus.RETRY_WAITING, TaskStatus.ABANDONED)),
        CrawlTask.lease_token.is_(None)).order_by(CrawlTask.id).limit(limit).with_for_update(skip_locked=True)).all()
    stamp = database_now(session)
    for task in tasks:
        task.status, task.finished_at = TaskStatus.CANCELLED, stamp
    session.flush()
    counts = dict(session.execute(query.where(CrawlTask.status.in_(ACTIVE_STATES)).with_only_columns(
        CrawlTask.status, func.count()).group_by(CrawlTask.status)).all())
    running = counts.get(TaskStatus.RUNNING, 0)
    pending = sum(counts.values()) - running
    shared = session.scalar(owned_tasks(settings, launch.id).where(
        CrawlTask.status.in_(ACTIVE_STATES), live_owner(settings)).with_only_columns(func.count()))
    launch.cancel_checked_at = stamp
    if not running and not pending and not shared:
        launch.cancel_completed_at = launch.cancel_completed_at or stamp
        launch.cancel_pending = False
    else:
        # Two concurrent shared owners may each still observe the other live.
        # Keep both eligible for another sweep instead of declaring completion
        # from a snapshot that cannot prove all shared work has ended.
        launch.cancel_completed_at = None
        launch.cancel_pending = True
    return {"cancelled_task_count": len(tasks), "still_running_count": running,
            "pending_remaining_count": pending, "shared_task_count": shared}


def cancel_launch(session, settings, launch_id):
    launch = session.scalar(select(Launch).where(Launch.id == launch_id, *market(Launch, settings))
        .with_for_update().execution_options(populate_existing=True))
    if launch is None:
        raise ManagementError("collection_launch_not_found", 404)
    changed = False
    active = session.scalar(owned_tasks(settings, launch.id).where(CrawlTask.status.in_(ACTIVE_STATES)).with_only_columns(func.count()))
    if launch.cancel_requested_at is None and active:
        launch.cancel_requested_at = database_now(session)
        changed = True
        audit(session, settings, "launch_cancelled", "launch", launch.id, inflight_policy="finish", shared_policy="retain_live_owner")
        session.flush()
    progress = finish_cancellation(session, settings, launch) if launch.cancel_requested_at is not None else {
        "cancelled_task_count": 0, "still_running_count": 0, "pending_remaining_count": 0, "shared_task_count": 0}
    from app.services.collection_intake import launch_summary
    return {"changed": changed, "launch": launch_summary(session, launch), **progress}


def sweep_cancelled_launches(session, settings, *, limit=100):
    """Scheduler owns bounded cleanup; claims independently enforce the flag."""
    launches = session.scalars(select(Launch).where(*market(Launch, settings),
        Launch.cancel_pending.is_(True))
        .order_by(Launch.cancel_checked_at, Launch.id).limit(10).with_for_update(skip_locked=True)).all()
    cancelled = 0
    for launch in launches:
        result = finish_cancellation(session, settings, launch, limit=limit - cancelled)
        cancelled += result["cancelled_task_count"]
        if cancelled >= limit:
            break
    return cancelled
