"""Read deployment progress without exposing source payloads or credentials."""

import json

from sqlalchemy import func, select

from app.models.analytics_read import AnalyticsRefreshRequest as Job, AnalyticsSnapshot, EventReadModel, ProductReadModel
from app.services.analytics_refresh import VERSION, key_for, now, scope
from app.settings import Settings
from app.storage.database import make_engine, make_session_factory


def status(db, config):
    control = db.get(Job, (VERSION, "control", key_for("control", {**scope(config), "name": "backfill"})))
    progress = dict(control.parameters) if control else {}
    states = db.execute(select(Job.kind, Job.is_pending, func.count()).where(Job.schema_version == VERSION)
        .group_by(Job.kind, Job.is_pending)).all()
    failures = db.execute(select(Job.kind, Job.last_error_code, func.count()).where(Job.schema_version == VERSION,
        Job.is_pending.is_(True), Job.last_error_code.is_not(None)).group_by(Job.kind, Job.last_error_code)).all()
    return {"observed_at": now(db).isoformat(), "backfill": progress,
        "projections": {basis: db.scalar(select(func.count()).select_from(ProductReadModel).where(
            ProductReadModel.schema_version == VERSION, ProductReadModel.basis == basis)) for basis in ("current", "latest_valid_snapshot")},
        "events": db.scalar(select(func.count()).select_from(EventReadModel).where(EventReadModel.schema_version == VERSION)),
        "jobs": [{"kind": kind, "pending": bool(pending), "count": count} for kind, pending, count in states],
        "failures": [{"kind": kind, "error": error, "count": count} for kind, error, count in failures],
        "snapshots": [{"kind": row.kind, "lookback_hours": row.parameters.get("lookback_hours"),
                       "generation": str(row.generation), "generated_at": row.generated_at.isoformat()}
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
