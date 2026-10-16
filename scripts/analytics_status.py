"""Read deployment progress without exposing source payloads or credentials."""

import json

from sqlalchemy import func, select

from app.models.analytics_read import AnalyticsRefreshRequest as Job, AnalyticsSnapshot, EventReadModel, ProductReadModel
from app.services.analytics_refresh import VERSION, key_for, now, scope
from app.services.analytics_rollup import control_params
from app.settings import Settings
from app.storage.database import make_engine, make_session_factory


def status(db, config):
    control = db.get(Job, (VERSION, "control", key_for("control", {**scope(config), "name": "backfill"})))
    progress = dict(control.parameters) if control else {}
    rollup = db.get(Job, (VERSION, "control", key_for("control", control_params(config))))
    stamp = now(db)
    pending = db.execute(select(Job.kind, func.count(), func.min(Job.pending_since)).where(
        Job.schema_version == VERSION, Job.is_pending.is_(True)).group_by(Job.kind)).all()
    states = db.execute(select(Job.kind, Job.is_pending, func.count()).where(Job.schema_version == VERSION)
        .group_by(Job.kind, Job.is_pending)).all()
    failures = db.execute(select(Job.kind, Job.last_error_code, func.count()).where(Job.schema_version == VERSION,
        Job.is_pending.is_(True), Job.last_error_code.is_not(None)).group_by(Job.kind, Job.last_error_code)).all()
    return {"observed_at": stamp.isoformat(), "backfill": progress,
        "rollup_backfill": dict(rollup.parameters) if rollup else {},
        "pending_ages": [{"kind": kind, "count": count,
            "oldest_seconds": max(0, int((stamp - oldest).total_seconds())) if oldest else 0} for kind, count, oldest in pending],
        "projections": {basis: db.scalar(select(func.count()).select_from(ProductReadModel).where(
            ProductReadModel.schema_version == VERSION, ProductReadModel.basis == basis)) for basis in ("current", "latest_valid_snapshot")},
        "events": db.scalar(select(func.count()).select_from(EventReadModel).where(EventReadModel.schema_version == VERSION)),
        "jobs": [{"kind": kind, "pending": bool(pending), "count": count} for kind, pending, count in states],
        "failures": [{"kind": kind, "error": error, "count": count} for kind, error, count in failures],
        "snapshots": [{"kind": row.kind, "lookback_hours": row.parameters.get("lookback_hours"),
                       "generation": str(row.generation), "generated_at": row.generated_at.isoformat(),
                       "source_age_seconds": max(0, int((stamp - row.snapshot_at).total_seconds())),
                       "build_seconds": round((row.generated_at - row.snapshot_at).total_seconds(), 3)}
                      for row in db.scalars(select(AnalyticsSnapshot).where(AnalyticsSnapshot.schema_version == VERSION))]}


def main():
    config = Settings.load()
    engine = make_engine(config.database_url)
    try:
        with make_session_factory(engine)() as db:
            print(json.dumps(status(db, config), ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
