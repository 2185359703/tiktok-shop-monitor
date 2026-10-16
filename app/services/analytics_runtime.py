"""Restartable backfill, bounded maintenance and the analytics worker loop."""

import logging
import threading
from contextlib import contextmanager, nullcontext
from datetime import timedelta

from sqlalchemy import delete, exists, func, inspect, select, text, true
from sqlalchemy.orm import Session

from app.models import ChangeEvent, Product
from app.models.analytics_read import ANALYTICS_TABLE_NAMES, AnalyticsRefreshRequest as Job, AnalyticsSnapshot, EventReadModel, ProductReadModel
from app.services.analytics_projection import build_event, build_product, publish_event, publish_product
from app.services.analytics_refresh import VERSION, STAT_KINDS, LeaseLost, claim, complete, enqueue, enqueue_many, failed, heartbeat, heavy_slot, job_row, key_for, now, scope
from app.services.analytics_snapshot import build_snapshot, publish_snapshot


def tables_ready(engine):
    return set(ANALYTICS_TABLE_NAMES).issubset(inspect(engine).get_table_names())


def ready(db, config):
    row = db.get(Job, (VERSION, "control", key_for("control", {**scope(config), "name": "backfill"})))
    return bool(row and row.parameters.get("ready"))


def backfill_batch(db, config, *, batch_size=64):
    row = job_row(db, "control", {**scope(config), "name": "backfill"})
    state = dict(row.parameters)
    if state.get("ready"):
        return True
    queued = db.scalar(select(func.count()).select_from(Job).where(Job.schema_version == VERSION,
        Job.is_pending == true(), Job.kind.in_(("product_projection", "event_projection"))))
    if queued >= 512:
        return False
    products = select(Product.id).where(Product.platform == config.platform, Product.region == config.region)
    events = select(ChangeEvent.id).join(Product, Product.id == ChangeEvent.product_id).where(
        Product.platform == config.platform, Product.region == config.region)
    if "product_upper" not in state:
        state.update(product_upper=db.scalar(products.with_only_columns(func.max(Product.id))) or 0,
            event_upper=db.scalar(events.with_only_columns(func.max(ChangeEvent.id))) or 0, product_after=0, event_after=0)
    for name, query, model, key in (("product", products, Product, "product_id"), ("event", events, ChangeEvent, "event_id")):
        if state.get(name + "_scan_done"):
            continue
        identifiers = db.scalars(query.where(model.id > state[name + "_after"], model.id <= state[name + "_upper"])
                                 .order_by(model.id).limit(batch_size)).all()
        enqueue_many(db, name + "_projection", [{key: str(identifier)} for identifier in identifiers])
        if identifiers:
            state[name + "_after"] = identifiers[-1]
        state[name + "_scan_done"] = len(identifiers) < batch_size
        row.parameters = state
        return False
    # Validate ID coverage, not just counts or MAX(id). Commit-order gaps are also
    # covered by transactional dirty markers installed before this scan begins.
    missing = []
    for basis in ("current", "latest_valid_snapshot"):
        missing.extend(db.scalars(products.where(~exists(select(ProductReadModel.product_id).where(
            ProductReadModel.schema_version == VERSION, ProductReadModel.basis == basis,
            ProductReadModel.product_id == Product.id))).limit(batch_size)).all())
    missing_events = db.scalars(events.where(~exists(select(EventReadModel.event_id).where(
        EventReadModel.schema_version == VERSION, EventReadModel.event_id == ChangeEvent.id))).limit(batch_size)).all()
    enqueue_many(db, "product_projection", [{"product_id": str(identifier)} for identifier in set(missing)])
    enqueue_many(db, "event_projection", [{"event_id": str(identifier)} for identifier in missing_events])
    pending = db.scalar(select(func.count()).select_from(Job).where(Job.schema_version == VERSION,
        Job.kind.in_(("product_projection", "event_projection")), Job.is_pending == true(),
        Job.requested_revision > Job.completed_revision))
    state["ready"] = not missing and not missing_events and pending == 0
    row.parameters = state
    return state["ready"]


def maintain(db, config):
    stamp = now(db)
    # Bound custom cache keys across restarts; cleanup only derived, idle entries.
    old = db.scalars(select(Job).where(Job.schema_version == VERSION, Job.kind.in_(STAT_KINDS),
        Job.last_requested_at < stamp - timedelta(days=1),
        Job.lease_token.is_(None)).limit(32)).all()
    for row in old:
        db.execute(delete(AnalyticsSnapshot).where(AnalyticsSnapshot.schema_version == VERSION, AnalyticsSnapshot.key_hash == row.key_hash))
        db.delete(row)
    if not ready(db, config):
        return
    for kind in ("overview", "workbench", "catalog_stats", "task_counts", "matching_candidates"):
        windows = (24, 168, 720) if kind in ("overview", "workbench") else (None,)
        for hours in windows:
            params = {**scope(config), **({"lookback_hours": hours} if hours is not None else {})}
            key = key_for(kind, params)
            snapshot = db.get(AnalyticsSnapshot, (VERSION, key))
            if snapshot is None or snapshot.fresh_until <= stamp:
                enqueue(db, kind, params)
    # Rolling windows and freshness buckets expire without new source writes.
    for row in db.scalars(select(Job).where(Job.schema_version == VERSION, Job.kind.in_(STAT_KINDS),
        Job.last_requested_at >= stamp - timedelta(minutes=30), Job.requested_revision == Job.completed_revision)).all():
        snapshot = db.get(AnalyticsSnapshot, (VERSION, row.key_hash))
        if snapshot and snapshot.fresh_until <= stamp:
            enqueue(db, row.kind, row.parameters)


@contextmanager
def source_reader(factory, *, write=False):
    with factory() as db:
        if db.bind.dialect.name == "mysql":
            connection = db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            # All queries are bounded, including a transaction waiting for a lock.
            old_select = db.scalar(text("SELECT @@SESSION.max_execution_time"))
            old_lock = db.scalar(text("SELECT @@SESSION.innodb_lock_wait_timeout"))
            db.execute(text("SET SESSION MAX_EXECUTION_TIME=3000"))
            db.execute(text("SET SESSION innodb_lock_wait_timeout=1"))
            try:
                yield db
                db.flush()
            finally:
                try:
                    connection.execute(text("SET SESSION MAX_EXECUTION_TIME=:value"), {"value": old_select})
                    connection.execute(text("SET SESSION innodb_lock_wait_timeout=:value"), {"value": old_lock})
                except Exception:
                    connection.invalidate()
                    raise
        else:
            yield db
        if write:
            db.commit()


class AnalyticsRunner:
    def __init__(self, factory, config):
        self.factory, self.config, self.steps = factory, config, 0

    def run_once(self):
        with self.factory.begin() as db:
            lease = claim(db, self.config, prefer_statistics=self.steps % 8 == 7)
        if lease is None:
            return False
        stopped, lost = threading.Event(), threading.Event()
        def renew():
            while not stopped.wait(10):
                try:
                    with self.factory.begin() as db:
                        heartbeat(db, lease)
                except Exception:
                    lost.set()
                    return
        thread = threading.Thread(target=renew, daemon=True)
        thread.start()
        try:
            with (heavy_slot(self.factory, self.config) if lease.kind in STAT_KINDS else nullcontext()), source_reader(self.factory) as db:
                if lease.kind == "product_projection":
                    data = build_product(db, self.config, int(lease.parameters["product_id"]), lease.revision)
                    publish = lambda session: publish_product(session, self.config, data)
                elif lease.kind == "event_projection":
                    data = build_event(db, self.config, int(lease.parameters["event_id"]))
                    publish = lambda session: publish_event(session, self.config, data)
                else:
                    data, stamp = build_snapshot(db, self.config, lease.kind, lease.parameters)
                    publish = lambda session: publish_snapshot(session, lease.kind, lease.parameters, lease.revision, data, stamp)
            if lost.is_set():
                raise LeaseLost("analytics_heartbeat_lost")
            with self.factory.begin() as db:
                complete(db, lease, publish)
        except Exception as error:
            logging.getLogger(__name__).warning("analytics_refresh_failed", extra={"kind": lease.kind, "error_type": type(error).__name__})
            try:
                with self.factory.begin() as db:
                    failed(db, lease, error)
            except LeaseLost:
                pass
        finally:
            stopped.set()
            thread.join(timeout=2)
            self.steps += 1
        return True
