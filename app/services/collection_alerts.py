"""Background incident state transitions, serialized with operator acknowledgments."""

import asyncio
from datetime import datetime

from sqlalchemy import select

from app.models.collection_alerts import CollectionAlert as Alert, CollectionAlertEvent as Event, CollectionAlertEvaluation as Evaluation
from app.services.analytics_refresh import now
from app.services.collection_heartbeat import read_runtime
from app.services.collection_monitor import monitor_alerts, read_monitor


EVALUATION_INTERVAL_SECONDS = 10
EVALUATION_STALE_SECONDS = 45
RULES = {"queue_summary_pending", "queue_summary_stale", "expired_leases", "statistics_warming", "statistics_stale",
    "high_failure_rate", "runtime_unknown", "worker_heartbeat_missing", "scheduler_heartbeat_missing",
    "worker_capacity_missing", "scheduler_capacity_missing", "runtime_attention", "scheduler_tick_stale",
    "scheduler_tick_unknown", "source_cooldown"}
RUNTIME_RULES = {"worker_heartbeat_missing", "scheduler_heartbeat_missing", "worker_capacity_missing",
                 "scheduler_capacity_missing", "runtime_attention", "scheduler_tick_stale", "scheduler_tick_unknown"}


class AlertError(ValueError):
    def __init__(self, code, status=404):
        self.code, self.status = code, status
        super().__init__(code)


def scope(model, settings):
    return model.platform == settings.platform, model.region == settings.region


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def fresh(value, stamp):
    parsed = timestamp(value)
    return parsed is not None and -30 <= (stamp - parsed).total_seconds() <= EVALUATION_STALE_SECONDS


def decisions(snapshot, settings, stamp):
    """Positive evidence may open; unknown data must never clear an incident."""
    emitted = {item["code"]: item for item in monitor_alerts(snapshot, scheduler_poll_seconds=settings.poll_seconds,
        expected_workers=settings.monitor_expected_workers, expected_schedulers=settings.monitor_expected_schedulers)
        if item["code"] in RULES}
    queue, statistics, runtime = snapshot["queue"], snapshot["statistics"], snapshot["runtime"]
    runtime_ready = runtime["status"] == "available" and fresh(runtime.get("observed_at"), stamp)
    if not runtime_ready:
        emitted["runtime_unknown"] = {"code": "runtime_unknown", "severity": "warning",
            "message": "运行心跳暂不可用或观测过期，相关告警保持待确认。"}
    known = {code: True for code in RULES}
    known["queue_summary_stale"] = queue.get("age_seconds") is not None
    known["statistics_stale"] = statistics.get("status") in {"ready", "stale"}
    known["high_failure_rate"] = bool(statistics.get("ready") and statistics.get("status") == "ready"
        and snapshot.get("task_group") == "source" and snapshot["window"]["minutes"] == 60
        and (snapshot["attempts"].get("total") or 0) >= 10)
    for code in RUNTIME_RULES:
        known[code] = runtime_ready
    known["source_cooldown"] = runtime_ready and runtime.get("source_cooldown_seconds") is not None
    schedulers = [item for item in runtime.get("components", [])
                  if item["role"] == "scheduler" and item["status"] == "online" and item["state"] != "stopped"]
    tick_times = [timestamp(item.get("last_success_at")) for item in schedulers]
    valid_ticks = bool(schedulers) and all(value is not None and (value - stamp).total_seconds() <= 30 for value in tick_times)
    # Missing schedulers or invalid tick times cannot prove a previous stall was fixed.
    known["scheduler_tick_stale"] = runtime_ready and ("scheduler_tick_stale" in emitted or valid_ticks)
    known["scheduler_tick_unknown"] = runtime_ready and bool(schedulers)
    sources = {"statistics": statistics.get("status", "warming"),
        "runtime": "available" if runtime_ready else "unavailable", "queue": queue["status"], "leases": "available"}
    return {code: {"known": known[code], "active": known[code] and code in emitted, "data": emitted.get(code)}
            for code in RULES}, sources


def lock_evaluation(db, settings):
    values = dict(platform=settings.platform, region=settings.region, last_observed_at=None,
                  last_evaluated_at=None, sources={})
    if db.bind.dialect.name == "mysql":
        from sqlalchemy.dialects.mysql import insert
        statement = insert(Evaluation).values(**values)
        db.execute(statement.on_duplicate_key_update(platform=statement.inserted.platform))
    else:
        from sqlalchemy.dialects.sqlite import insert
        db.execute(insert(Evaluation).values(**values).on_conflict_do_nothing())
    return db.scalar(select(Evaluation).where(*scope(Evaluation, settings)).with_for_update()
                     .execution_options(populate_existing=True))


def add_event(db, alert, kind, stamp):
    db.flush()
    db.add(Event(alert_id=alert.id, event_type=kind, status=alert.status, occurred_at=stamp,
                 occurrence=alert.occurrences, message="操作员已确认告警。" if kind == "acknowledged" else alert.message))


def evaluate_snapshot(db, settings, snapshot, *, stamp=None):
    stamp = stamp or now(db)
    if snapshot.get("scope") != {"platform": settings.platform, "region": settings.region}:
        return {"applied": False, "reason": "observation_scope_mismatch"}
    observed_at = timestamp(snapshot.get("generated_at"))
    if observed_at is None or not fresh(snapshot.get("generated_at"), stamp):
        return {"applied": False, "reason": "observation_stale"}
    state = lock_evaluation(db, settings)
    if state.last_observed_at is not None and observed_at <= state.last_observed_at:
        return {"applied": False, "reason": "observation_not_newer"}
    rules, sources = decisions(snapshot, settings, stamp)
    alerts = {row.code: row for row in db.scalars(select(Alert).where(*scope(Alert, settings))
        .order_by(Alert.id).with_for_update().execution_options(populate_existing=True))}
    transitions = 0
    for code in sorted(RULES):
        rule, row = rules[code], alerts.get(code)
        if row is None:
            if not rule["active"]:
                continue
            row = Alert(platform=settings.platform, region=settings.region, code=code,
                severity=rule["data"]["severity"], message=rule["data"]["message"], status="open",
                first_seen_at=observed_at, opened_at=observed_at, last_seen_at=observed_at,
                last_evaluated_at=observed_at, condition_known=True, acknowledged_at=None,
                resolved_at=None, occurrences=1)
            db.add(row)
            add_event(db, row, "opened", stamp)
            transitions += 1
            continue
        row.condition_known = rule["known"]
        if not rule["known"]:
            continue
        row.last_evaluated_at = observed_at
        if rule["active"]:
            row.last_seen_at = observed_at
            row.severity, row.message = rule["data"]["severity"], rule["data"]["message"]
            if row.status == "resolved":
                row.status, row.opened_at, row.occurrences = "open", observed_at, row.occurrences + 1
                row.acknowledged_at = row.resolved_at = None
                add_event(db, row, "reopened", stamp)
                transitions += 1
        elif row.status != "resolved":
            row.status, row.resolved_at = "resolved", observed_at
            add_event(db, row, "resolved", stamp)
            transitions += 1
    state.last_observed_at, state.last_evaluated_at, state.sources = observed_at, stamp, sources
    db.flush()
    return {"applied": True, "transitions": transitions}


async def evaluate_runtime_once(database, settings):
    """Own background reads; a browser request never drives incident transitions."""
    snapshot, runtime = await asyncio.gather(database.read(read_monitor, settings, 60), read_runtime(settings))
    snapshot["runtime"] = runtime
    return await database.transaction(evaluate_snapshot, settings, snapshot)


def alert_summary(row):
    return {"id": str(row.id), "code": row.code, "severity": row.severity, "message": row.message,
        "status": row.status, "first_seen_at": row.first_seen_at.isoformat(), "opened_at": row.opened_at.isoformat(),
        "last_seen_at": row.last_seen_at.isoformat(), "last_evaluated_at": row.last_evaluated_at.isoformat(),
        "condition_known": row.condition_known, "acknowledged_at": row.acknowledged_at.isoformat() if row.acknowledged_at else None,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None, "occurrences": row.occurrences}


def evaluation_summary(db, settings, *, stamp=None):
    stamp = stamp or now(db)
    state = db.get(Evaluation, (settings.platform, settings.region))
    age = max(0, (stamp - state.last_evaluated_at).total_seconds()) if state and state.last_evaluated_at else None
    return {"status": "never" if age is None else "fresh" if age <= EVALUATION_STALE_SECONDS else "stale",
        "last_evaluated_at": state.last_evaluated_at.isoformat() if state and state.last_evaluated_at else None,
        "age_seconds": round(age, 1) if age is not None else None,
        "sources": state.sources if state else {}, "stale_after_seconds": EVALUATION_STALE_SECONDS,
        "window_minutes": 60, "task_group": "source"}


def get_alert(db, settings, identifier, *, lock=False):
    query = select(Alert).where(Alert.id == identifier, *scope(Alert, settings))
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = db.scalar(query)
    if row is None:
        raise AlertError("collection_alert_not_found")
    return row


def acknowledge(db, settings, identifier, *, stamp=None):
    # Same market -> alert lock order as evaluate_snapshot prevents lost ACKs.
    lock_evaluation(db, settings)
    row = get_alert(db, settings, identifier, lock=True)
    changed = row.status == "open"
    if changed:
        stamp = stamp or now(db)
        row.status, row.acknowledged_at = "acknowledged", stamp
        add_event(db, row, "acknowledged", stamp)
        db.flush()
    return {"changed": changed, "alert": alert_summary(row)}


def list_alerts(db, settings, *, status="active", limit=20, before_id=None):
    query = select(Alert).where(*scope(Alert, settings))
    if status == "active":
        query = query.where(Alert.status.in_(("open", "acknowledged")))
    elif status != "all":
        query = query.where(Alert.status == status)
    if before_id is not None:
        query = query.where(Alert.id < before_id)
    rows = db.scalars(query.order_by(Alert.id.desc()).limit(limit + 1)).all()
    return {"items": [alert_summary(row) for row in rows[:limit]],
            "next_before_id": str(rows[limit - 1].id) if len(rows) > limit else None,
            "evaluation": evaluation_summary(db, settings)}


def list_events(db, settings, identifier, *, limit=20, before_id=None):
    alert = get_alert(db, settings, identifier)
    query = select(Event).where(Event.alert_id == alert.id)
    if before_id is not None:
        query = query.where(Event.id < before_id)
    rows = db.scalars(query.order_by(Event.id.desc()).limit(limit + 1)).all()
    return {"alert": alert_summary(alert), "items": [{"id": str(row.id), "event_type": row.event_type,
        "status": row.status, "occurred_at": row.occurred_at.isoformat(), "occurrence": row.occurrence,
        "message": row.message} for row in rows[:limit]],
        "next_before_id": str(rows[limit - 1].id) if len(rows) > limit else None,
        "evaluation": evaluation_summary(db, settings)}
