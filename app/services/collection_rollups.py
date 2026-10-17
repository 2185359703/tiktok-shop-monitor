"""Transactional completion outbox, resumable backfill and compact monitor views.

Activate only after every attempt writer runs the outbox hook. Attempt IDs are
allocated at claim time, so neither live processing nor backfill uses MAX(id) as
a completion watermark. A processed fact and all its counters commit together.
"""

import math
import re
from collections import Counter, defaultdict
from datetime import timedelta

from sqlalchemy import delete, inspect, select, tuple_

from app.models import CrawlAttempt, CrawlTask
from app.models.analytics_read import AnalyticsSnapshot
from app.models.collection_monitor import (
    MONITOR_TABLE_NAMES, CollectionAttemptFact as Fact, CollectionMinuteRollup as Minute,
    CollectionMonitorState as State,
)
from app.services.analytics_refresh import VERSION as SNAPSHOT_VERSION, key_for, now


VERSION = 1
WINDOWS = (15, 60, 360, 1440)
GROUPS = ("source", "local", "all")
LOCAL_TYPES = {"change_detection", "report_generation"}
RETENTION_DAYS = 7
BATCH_SIZE = 500
FRESH_SECONDS = 45
ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


def safe_error(value):
    return value if isinstance(value, str) and ERROR_CODE.fullmatch(value) else "unknown"


def minute(value):
    return value.replace(second=0, microsecond=0)


def market(config):
    return {"platform": config.platform, "region": config.region}


def scoped(model, config):
    return (model.schema_version == VERSION, model.platform == config.platform, model.region == config.region)


def tables_ready(engine):
    return set(MONITOR_TABLE_NAMES).issubset(inspect(engine).get_table_names())


def snapshot_params(config, window_minutes, task_group):
    return {**market(config), "window_minutes": window_minutes, "task_group": task_group, "monitor_version": VERSION}


def insert_missing(db, model, values):
    if not values:
        return
    if db.bind.dialect.name == "mysql":
        from sqlalchemy.dialects.mysql import insert
        statement = insert(model).values(values)
        db.execute(statement.on_duplicate_key_update(schema_version=statement.inserted.schema_version))
    else:
        from sqlalchemy.dialects.sqlite import insert
        db.execute(insert(model).values(values).on_conflict_do_nothing())


def duration_us(started_at, finished_at):
    value = finished_at - started_at
    return max(0, (value.days * 86400 + value.seconds) * 1_000_000 + value.microseconds)


def fact_values(task, attempt):
    return dict(schema_version=VERSION, attempt_id=attempt.id, platform=task.platform, region=task.region,
        task_id=task.task_id, task_type=str(task.task_type), attempt_no=attempt.attempt_no,
        task_group="local" if str(task.task_type) in LOCAL_TYPES else "source",
        worker_id=attempt.worker_id, finished_at=attempt.finished_at, succeeded=attempt.succeeded,
        error_type=None if attempt.succeeded else safe_error(attempt.error_type), response_status=attempt.response_status,
        duration_us=duration_us(attempt.started_at, attempt.finished_at), processed=False)


def record_completed_attempt(db, task, attempt):
    """Called by every completion path inside its source transaction; never commit."""
    if attempt.finished_at is None or attempt.succeeded is None:
        raise ValueError("monitor_attempt_not_completed")
    db.flush()
    insert_missing(db, Fact, [fact_values(task, attempt)])


def activate(db, config, *, stamp=None):
    """Idempotently establish a fixed 25h baseline after all writers have switched."""
    stamp = stamp or now(db)
    insert_missing(db, State, [dict(schema_version=VERSION, **market(config), activated_at=stamp,
        coverage_since=minute(stamp) - timedelta(hours=25), backfill_until=stamp,
        cursor_finished_at=None, cursor_attempt_id=None, backfill_complete=False,
        generation=0, last_progress_at=stamp, published_at=None)])
    return db.scalar(select(State).where(*scoped(State, config)).with_for_update())


def reconcile(db, config, *, stamp=None):
    """Repair a writer rollback/gap without deleting facts or published counters.

    All writers must again have the hook before this transaction starts. Every
    previously processed fact is retained throughout the 25h repair interval,
    so overlap with real-time completion and older scans cannot count twice.
    """
    stamp = stamp or now(db)
    state = activate(db, config, stamp=stamp)
    state.coverage_since, state.backfill_until = minute(stamp) - timedelta(hours=25), stamp
    state.cursor_finished_at = state.cursor_attempt_id = None
    state.backfill_complete, state.published_at, state.last_progress_at = False, None, stamp
    for minutes in WINDOWS:
        for group in GROUPS:
            key = key_for("collection_monitor", snapshot_params(config, minutes, group))
            snapshot = db.get(AnalyticsSnapshot, (SNAPSHOT_VERSION, key))
            if snapshot is not None:
                snapshot.payload = {**snapshot.payload, "statistics": {**snapshot.payload["statistics"], "ready": False}}
                snapshot.fresh_until = stamp
    return state


def backfill(db, config, state, batch_size):
    if state.backfill_complete:
        return 0
    query = select(CrawlAttempt.id, CrawlAttempt.attempt_no, CrawlAttempt.worker_id,
        CrawlAttempt.finished_at, CrawlAttempt.started_at, CrawlAttempt.succeeded,
        CrawlAttempt.error_type, CrawlAttempt.response_status,
        CrawlTask.task_id, CrawlTask.task_type, CrawlTask.platform, CrawlTask.region).select_from(CrawlAttempt).join(
            CrawlTask, CrawlTask.id == CrawlAttempt.task_id).where(
            CrawlTask.platform == config.platform, CrawlTask.region == config.region,
            CrawlAttempt.finished_at >= state.coverage_since, CrawlAttempt.finished_at < state.backfill_until,
            CrawlAttempt.succeeded.is_not(None))
    if state.cursor_finished_at is not None:
        query = query.where(tuple_(CrawlAttempt.finished_at, CrawlAttempt.id) >
                            (state.cursor_finished_at, state.cursor_attempt_id))
    rows = db.execute(query.with_hint(CrawlAttempt, "FORCE INDEX (ix_crawl_attempts_monitor)", dialect_name="mysql")
        .prefix_with("STRAIGHT_JOIN", dialect="mysql")
        .order_by(CrawlAttempt.finished_at, CrawlAttempt.id).limit(batch_size)).all()
    insert_missing(db, Fact, [fact_values(row, row) for row in rows])
    if rows:
        state.cursor_finished_at, state.cursor_attempt_id = rows[-1].finished_at, rows[-1].id
    state.backfill_complete = len(rows) < batch_size
    return len(rows)


def histogram_key(value):
    # Zero has a dedicated bucket. Positive buckets have at most 10% relative
    # width; storing integer microseconds avoids mean/quantile rounding drift.
    return "zero" if value == 0 else str(math.ceil(math.log(value, 1.1)))


def histogram_p95(histogram, count):
    if not count:
        return None
    target, accumulated = math.ceil(count * .95), 0
    for key in sorted(histogram, key=lambda item: -1 if item == "zero" else int(item)):
        accumulated += histogram[key]
        if accumulated >= target:
            upper_ms = 0 if key == "zero" else 1.1 ** int(key) / 1000
            return upper_ms


def project_pending(db, config, batch_size):
    facts = db.scalars(select(Fact).where(*scoped(Fact, config), Fact.processed.is_(False))
        .order_by(Fact.finished_at, Fact.attempt_id).limit(batch_size).with_for_update()).all()
    groups = defaultdict(list)
    for fact in facts:
        groups[(minute(fact.finished_at), fact.task_type)].append(fact)
    keys = sorted(groups)
    rows = { (row.minute_at, row.task_type): row for row in db.scalars(select(Minute).where(
        *scoped(Minute, config), tuple_(Minute.minute_at, Minute.task_type).in_(keys)).with_for_update())} if keys else {}
    for key in keys:
        row = rows.get(key)
        if row is None:
            row = Minute(schema_version=VERSION, **market(config), minute_at=key[0], task_type=key[1],
                         completed=0, succeeded=0, duration_us=0, histogram={}, errors={})
            db.add(row)
        histogram, errors = Counter(row.histogram), Counter(row.errors)
        for fact in groups[key]:
            row.completed += 1
            row.succeeded += fact.succeeded
            row.duration_us += fact.duration_us
            histogram[histogram_key(fact.duration_us)] += 1
            if not fact.succeeded:
                errors[fact.error_type] += 1
            fact.processed = True
        row.histogram, row.errors = dict(histogram), dict(errors)
    db.flush()
    pending = db.scalar(select(Fact.attempt_id).where(*scoped(Fact, config), Fact.processed.is_(False)).limit(1))
    return len(facts), pending is not None


def empty_metrics(*, unknown=False):
    return {"total": None if unknown else 0, "succeeded": None if unknown else 0,
        "failed": None if unknown else 0, "success_rate": None, "avg_duration_ms": None,
        "p95_duration_ms": None, "throughput_per_minute": None if unknown else 0,
        "sampled": False, "truncated": False, "limit": None}


def metrics(rows, minutes):
    total = sum(row.completed for row in rows)
    succeeded = sum(row.succeeded for row in rows)
    histogram = Counter()
    for row in rows:
        histogram.update(row.histogram)
    return {**empty_metrics(), "total": total, "succeeded": succeeded, "failed": total - succeeded,
        "success_rate": round(100 * succeeded / total, 2) if total else None,
        "avg_duration_ms": round(sum(row.duration_us for row in rows) / 1000 / total, 3) if total else None,
        "p95_duration_ms": histogram_p95(histogram, total), "throughput_per_minute": round(total / minutes, 2)}


def belongs(task_type, task_group):
    return task_group == "all" or ((task_type in LOCAL_TYPES) == (task_group == "local"))


def window_payload(rows, minutes, group, until, state, task_groups):
    start = until - timedelta(minutes=minutes)
    seconds = {15: 60, 60: 300, 360: 1800, 1440: 7200}[minutes]
    selected = [row for row in rows if belongs(row.task_type, group)]
    trend = [{"time": (start + timedelta(seconds=index * seconds)).isoformat(),
              "completed": 0, "succeeded": 0, "failed": 0, "complete": True}
             for index in range(minutes * 60 // seconds)]
    errors = Counter()
    for row in selected:
        bucket = trend[int((row.minute_at - start).total_seconds() // seconds)]
        bucket["completed"] += row.completed
        bucket["succeeded"] += row.succeeded
        bucket["failed"] += row.completed - row.succeeded
        errors.update(row.errors)
    return {"window": {"minutes": minutes, "since": start.isoformat(), "until": until.isoformat(),
                       "bucket_seconds": seconds, "alignment": "completed_minutes", "end_exclusive": True},
        "task_group": group, "attempts": task_groups[group], "trend": trend, "task_groups": task_groups,
        "errors": [{"error_type": code, "count": count} for code, count in errors.most_common(12)],
        "statistics": {"method": "minute_rollup", "ready": True, "coverage_since": state.coverage_since.isoformat(),
            "coverage_until": until.isoformat(), "p95_method": "histogram_upper_bound", "p95_approximate": True,
            "p95_max_relative_error": .1, "count_method": "exact", "mean_method": "exact"}}


def publish(db, config, state, stamp):
    until = minute(stamp)
    rows = db.scalars(select(Minute).where(*scoped(Minute, config),
        Minute.minute_at >= until - timedelta(hours=24), Minute.minute_at < until)).all()
    state.generation += 1
    for minutes in WINDOWS:
        window_rows = [row for row in rows if row.minute_at >= until - timedelta(minutes=minutes)]
        task_groups = {kind: metrics([row for row in window_rows if belongs(row.task_type, kind)], minutes)
                       for kind in GROUPS}
        for group in GROUPS:
            params = snapshot_params(config, minutes, group)
            key = key_for("collection_monitor", params)
            snapshot = db.get(AnalyticsSnapshot, (SNAPSHOT_VERSION, key))
            values = dict(schema_version=SNAPSHOT_VERSION, key_hash=key, kind="collection_monitor", parameters=params,
                payload=window_payload(window_rows, minutes, group, until, state, task_groups), generation=state.generation,
                snapshot_at=stamp, generated_at=stamp, fresh_until=stamp + timedelta(seconds=FRESH_SECONDS),
                stale_until=stamp + timedelta(days=RETENTION_DAYS))
            if snapshot is None:
                db.add(AnalyticsSnapshot(**values))
            else:
                for name, value in values.items():
                    setattr(snapshot, name, value)
    state.published_at = stamp


def cleanup(db, config, stamp, batch_size):
    cutoff = minute(stamp) - timedelta(days=RETENTION_DAYS)
    identifiers = db.scalars(select(Fact.attempt_id).where(*scoped(Fact, config), Fact.processed.is_(True),
        Fact.finished_at < cutoff).order_by(Fact.finished_at, Fact.attempt_id).limit(batch_size)).all()
    if identifiers:
        db.execute(delete(Fact).where(*scoped(Fact, config), Fact.attempt_id.in_(identifiers)))
    keys = db.execute(select(Minute.minute_at, Minute.task_type).where(*scoped(Minute, config),
        Minute.minute_at < cutoff).order_by(Minute.minute_at, Minute.task_type).limit(batch_size)).all()
    if keys:
        db.execute(delete(Minute).where(*scoped(Minute, config), tuple_(Minute.minute_at, Minute.task_type).in_(keys)))


def advance_rollups(db, config, *, stamp=None, batch_size=BATCH_SIZE, force_publish=False):
    """One bounded transaction; a per-market state lock serializes aggregators."""
    if not 1 <= batch_size <= 1000:
        raise ValueError("monitor_batch_size")
    stamp = stamp or now(db)
    state = db.scalar(select(State).where(*scoped(State, config)).with_for_update(skip_locked=True)
                      .execution_options(populate_existing=True))
    if state is None:
        return {"activated": False, "processed": 0, "ready": False}
    scanned = backfill(db, config, state, batch_size)
    processed, pending = project_pending(db, config, batch_size)
    state.last_progress_at = stamp
    # A late commit is captured by its transaction's fact and reflected in the
    # next publication. Never publish a truncated backfill or a known backlog.
    ready = state.backfill_complete and not pending
    if ready and (force_publish or state.published_at is None or (stamp - state.published_at).total_seconds() >= 10):
        publish(db, config, state, stamp)
    cleanup(db, config, stamp, batch_size)
    return {"activated": True, "processed": processed, "scanned": scanned, "ready": ready}


def read_statistics(db, config, minutes, group, stamp):
    if minutes not in WINDOWS or group not in GROUPS:
        raise ValueError("invalid_monitor_window_or_group")
    row = db.get(AnalyticsSnapshot, (SNAPSHOT_VERSION, key_for("collection_monitor", snapshot_params(config, minutes, group))))
    if row is None or not row.payload.get("statistics", {}).get("ready"):
        until = minute(stamp)
        payload = {"task_group": group, "window": {"minutes": minutes, "since": (until - timedelta(minutes=minutes)).isoformat(),
            "until": until.isoformat(), "bucket_seconds": {15: 60, 60: 300, 360: 1800, 1440: 7200}[minutes],
            "alignment": "completed_minutes", "end_exclusive": True},
            "attempts": empty_metrics(unknown=True), "task_groups": {group: empty_metrics(unknown=True) for group in GROUPS},
            "trend": [], "errors": [], "statistics": {"method": "minute_rollup", "ready": False,
            "status": "warming", "snapshot_at": None, "age_seconds": None, "coverage_since": None,
            "coverage_until": None, "p95_method": "histogram_upper_bound", "p95_approximate": True,
            "p95_max_relative_error": .1, "count_method": "exact", "mean_method": "exact"}}
    else:
        payload = {**row.payload, "statistics": {**row.payload["statistics"],
            "status": "ready" if stamp < row.fresh_until else "stale", "snapshot_at": row.snapshot_at.isoformat(),
            "age_seconds": round(max(0, (stamp - row.snapshot_at).total_seconds()), 1)}}
    return payload


def recent_failures(db, config, start, until, group):
    query = select(Fact).where(*scoped(Fact, config), Fact.succeeded.is_(False), Fact.finished_at >= start,
                               Fact.finished_at < until)
    if group != "all":
        query = query.where(Fact.task_group == group)
    rows = db.scalars(query.order_by(Fact.finished_at.desc(), Fact.attempt_id.desc()).limit(12))
    return [{"task_id": row.task_id, "task_type": row.task_type, "attempt_no": row.attempt_no,
        "worker_id": row.worker_id, "error_type": row.error_type, "response_status": row.response_status,
        "finished_at": row.finished_at.isoformat()} for row in rows]
