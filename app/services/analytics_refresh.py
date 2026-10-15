"""Transactional invalidation, coalescing and fenced single-worker refreshes."""

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import case, event, func, or_, select
from sqlalchemy.orm import Session

from app.models.analytics_read import AnalyticsRefreshRequest as Job, AnalyticsSnapshot


VERSION = 2
LEASE_SECONDS = 60
SCOPE_FIELDS = ("platform", "region", "currency", "pricing_context")
STAT_KINDS = ("overview", "workbench", "catalog_stats", "events_summary", "task_counts", "matching_candidates")


def enabled(session=None):
    return bool(session.info.get("analytics_enabled")) if session is not None and "analytics_enabled" in session.info else os.getenv("ANALYTICS_ENABLED", "").lower() in {"1", "true", "yes"}


def now(db):
    if db.bind.dialect.name == "mysql":
        value = db.scalar(select(func.utc_timestamp(6)))
        return value.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def scope(config):
    return {name: getattr(config, name) for name in SCOPE_FIELDS}


def key_for(kind, parameters):
    return hashlib.sha256(json.dumps([VERSION, kind, parameters], sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), default=str).encode()).hexdigest()


def job_row(db, kind, parameters):
    key = key_for(kind, parameters)
    stamp = now(db)
    values = dict(schema_version=VERSION, kind=kind, key_hash=key, parameters=parameters,
                  requested_revision=0, completed_revision=0, claimed_revision=0, is_pending=False, pending_since=None,
                  available_at=stamp, last_requested_at=stamp, attempts=0)
    if db.bind.dialect.name == "mysql":
        from sqlalchemy.dialects.mysql import insert
        statement = insert(Job).values(**values)
        db.execute(statement.on_duplicate_key_update(key_hash=statement.inserted.key_hash))
    else:
        from sqlalchemy.dialects.sqlite import insert
        db.execute(insert(Job).values(**values).on_conflict_do_nothing())
    return db.scalar(select(Job).where(Job.schema_version == VERSION, Job.kind == kind, Job.key_hash == key)
                     .with_for_update().execution_options(populate_existing=True))


def enqueue(db, kind, parameters, *, force=False, available_at=None):
    row = job_row(db, kind, parameters)
    stamp = now(db)
    pending = row.requested_revision > row.completed_revision
    if force or not pending:
        row.requested_revision += 1
        row.is_pending = True
        if not pending:
            row.pending_since = stamp
            row.available_at = available_at or stamp
    row.last_requested_at = stamp
    db.flush()
    return row


def mark_products(db, product_ids):
    if not enabled(db):
        return
    seen = db.info.setdefault("analytics_dirty", set())
    for identifier in sorted(set(product_ids)):
        if ("product", identifier) not in seen:
            enqueue(db, "product_projection", {"product_id": str(identifier)}, force=True)
            seen.add(("product", identifier))


def mark_events(db, event_ids):
    if not enabled(db):
        return
    for identifier in sorted(set(event_ids)):
        enqueue(db, "event_projection", {"event_id": str(identifier)}, force=True)


@event.listens_for(Session, "after_transaction_end")
def clear_transaction_marks(session, transaction):
    if transaction.parent is None:
        session.info.pop("analytics_dirty", None)


@event.listens_for(Session, "after_soft_rollback")
def clear_rolled_back_marks(session, previous_transaction):
    session.info.pop("analytics_dirty", None)


def invalidate_statistics(db, config):
    stamp = now(db)
    rows = db.scalars(select(Job).where(Job.schema_version == VERSION, Job.kind.in_(STAT_KINDS),
        Job.last_requested_at >= stamp - timedelta(days=1)).with_for_update()).all()
    for row in rows:
        if any(row.parameters.get(k) != v for k, v in scope(config).items()):
            continue
        if row.requested_revision == row.completed_revision:
            row.requested_revision += 1
            row.is_pending = True
            row.pending_since = stamp
            snapshot = db.get(AnalyticsSnapshot, (VERSION, row.key_hash))
            row.available_at = max(stamp, snapshot.fresh_until) if snapshot else stamp


def mark_market(db, platform, region, *, immediate=False):
    if not enabled(db):
        return
    stamp = now(db)
    for row in db.scalars(select(Job).where(Job.schema_version == VERSION, Job.kind.in_(STAT_KINDS),
        Job.parameters["platform"].as_string() == platform, Job.parameters["region"].as_string() == region).with_for_update()).all():
        if row.requested_revision == row.completed_revision:
            row.requested_revision += 1
            row.is_pending = True
            row.pending_since = stamp
        snapshot = db.get(AnalyticsSnapshot, (VERSION, row.key_hash))
        row.available_at = stamp if immediate or snapshot is None else max(stamp, snapshot.fresh_until)
        if immediate and snapshot:
            snapshot.fresh_until = stamp


@contextmanager
def heavy_slot(factory, config):
    """Reports and UI aggregates share one expiring, renewable heavy-work slot."""
    if not enabled():
        yield
        return
    parameters = {**scope(config), "name": "heavy_job_slot"}
    token = str(uuid4())
    with factory() as db, db.begin():
        row, stamp = job_row(db, "control", parameters), now(db)
        if row.lease_until and row.lease_until > stamp:
            from app.queue.retry_policy import Failure, TaskFailure
            raise TaskFailure(Failure("analytics_heavy_busy", "已有统计或报告正在生成，稍后重试。", retryable=True, delay_seconds=15))
        row.lease_token, row.lease_until = token, stamp + timedelta(seconds=LEASE_SECONDS)
    stopped, lost = threading.Event(), threading.Event()
    def renew():
        while not stopped.wait(10):
            try:
                with factory() as db, db.begin():
                    row = job_row(db, "control", parameters)
                    if row.lease_token != token or row.lease_until <= now(db):
                        lost.set()
                        return
                    row.lease_until = now(db) + timedelta(seconds=LEASE_SECONDS)
            except Exception:
                lost.set()
                return
    worker = threading.Thread(target=renew, daemon=True)
    worker.start()
    try:
        yield
        with factory() as db, db.begin():
            row = job_row(db, "control", parameters)
            if lost.is_set() or row.lease_token != token or row.lease_until is None or row.lease_until <= now(db):
                raise LeaseLost("heavy_slot_lost")
    finally:
        stopped.set()
        worker.join(timeout=2)
        with factory() as db, db.begin():
            row = job_row(db, "control", parameters)
            if row.lease_token == token:
                row.lease_token = row.lease_until = None


class LeaseLost(RuntimeError):
    pass




@dataclass(frozen=True)
class RefreshLease:
    kind: str
    key: str
    parameters: dict
    revision: int
    token: str
    slot_key: str


def claim(db, config, *, prefer_statistics=False, slot_name="analysis_slot"):
    slot = job_row(db, "control", {**scope(config), "name": slot_name})
    stamp = now(db)
    if slot.lease_until and slot.lease_until > stamp:
        return None
    row = None
    for statistics in (prefer_statistics, not prefer_statistics):
        query = select(Job).where(Job.schema_version == VERSION, Job.is_pending.is_(True),
            Job.requested_revision > Job.completed_revision, Job.available_at <= stamp,
            or_(Job.lease_until.is_(None), Job.lease_until <= stamp))
        if statistics:
            query = query.where(Job.kind.in_(STAT_KINDS), *(Job.parameters[k].as_string() == v for k, v in scope(config).items()))
        else:
            query = query.where(Job.kind.in_(("product_projection", "event_projection")))
        row = db.scalar(query.order_by(Job.available_at, Job.key_hash).limit(1).with_for_update(skip_locked=True))
        if row is not None:
            break
    if row is None:
        return None
    token = str(uuid4())
    for item in (slot, row):
        item.lease_token, item.lease_until = token, stamp + timedelta(seconds=LEASE_SECONDS)
    row.claimed_revision = row.requested_revision
    db.flush()
    return RefreshLease(row.kind, row.key_hash, dict(row.parameters), row.claimed_revision, token, slot.key_hash)


def fenced_rows(db, lease):
    stamp = now(db)
    slot = db.scalar(select(Job).where(Job.schema_version == VERSION, Job.kind == "control",
                                      Job.key_hash == lease.slot_key).with_for_update())
    row = db.scalar(select(Job).where(Job.schema_version == VERSION, Job.kind == lease.kind,
                                     Job.key_hash == lease.key).with_for_update())
    if any(item is None or item.lease_token != lease.token or item.lease_until is None or
           item.lease_until <= stamp for item in (slot, row)):
        raise LeaseLost("analytics_lease_lost")
    return slot, row


def heartbeat(db, lease):
    for item in fenced_rows(db, lease):
        item.lease_until = now(db) + timedelta(seconds=LEASE_SECONDS)


def complete(db, lease, publish):
    slot, row = fenced_rows(db, lease)
    publish(db)
    row.completed_revision = lease.revision
    row.is_pending = row.requested_revision > lease.revision
    row.pending_since = None if row.requested_revision == lease.revision else row.pending_since
    row.available_at = now(db)
    row.attempts, row.last_error_code = 0, None
    for item in (row, slot):
        item.lease_token = item.lease_until = None


def failed(db, lease, error):
    slot, row = fenced_rows(db, lease)
    row.attempts += 1
    row.last_error_code = type(error).__name__[:80]
    row.available_at = now(db) + timedelta(seconds=min(300, 5 * 2 ** min(row.attempts, 6)))
    for item in (row, slot):
        item.lease_token = item.lease_until = None
