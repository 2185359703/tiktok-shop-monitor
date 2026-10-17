"""Full-window statistics, publication boundaries and completion durability."""

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from threading import Barrier

import pytest
from sqlalchemy import delete, event, func, select
from sqlalchemy.orm import Session

from app.models import CrawlAttempt, CrawlTask, ResourceType, TaskStatus, TaskType
from app.models.analytics_read import AnalyticsSnapshot
from app.models.collection_monitor import CollectionAttemptFact as Fact, CollectionMinuteRollup as Minute, CollectionMonitorState as State
from app.services.collection_monitor import monitor_alerts, read_monitor
from app.services.collection_rollups import (
    activate, advance_rollups, cleanup, histogram_key, histogram_p95,
    record_completed_attempt, reconcile, snapshot_params, scoped,
)
from app.services.analytics_refresh import VERSION as SNAPSHOT_VERSION, key_for
from app.settings import Settings
from test_analytics import NOW, analytics_engine
from test_collection_monitor import task, attempt, publish_statistics


def test_unactivated_aggregator_does_not_claim_a_complete_baseline(analytics_engine):
    with Session(analytics_engine) as db:
        task(db, 1)
        attempt(db, 1, 1)
        result = advance_rollups(db, Settings(), stamp=NOW)
        assert result == {"activated": False, "processed": 0, "ready": False}
        view = read_monitor(db, Settings(), stamp=NOW)
        assert view["statistics"]["ready"] is False
        assert view["attempts"]["total"] is None and view["trend"] == []


def test_backfill_restart_overlapping_outbox_and_late_low_id_complete_exactly_once(analytics_engine):
    config = Settings()
    with Session(analytics_engine) as db:
        for identifier in range(1, 5):
            task(db, identifier)
        attempt(db, 1, 1, finish=None, success=None)
        attempt(db, 2, 2, finish=NOW - timedelta(minutes=20))
        attempt(db, 3, 3, finish=NOW - timedelta(minutes=5))
        attempt(db, 4, 4, finish=NOW - timedelta(minutes=4))
        record_completed_attempt(db, db.get(CrawlTask, 3), db.get(CrawlAttempt, 3))
        state = activate(db, config, stamp=NOW)
        boundary = state.backfill_until
        result = advance_rollups(db, config, stamp=NOW, batch_size=1)
        assert not result["ready"]
        db.commit()
    with Session(analytics_engine) as db:
        assert activate(db, config, stamp=NOW + timedelta(minutes=1)).backfill_until == boundary
        for _ in range(8):
            result = advance_rollups(db, config, stamp=NOW, batch_size=1)
            db.commit()
            if result["ready"]:
                break
        assert result["ready"]
        assert read_monitor(db, config, stamp=NOW)["attempts"]["total"] == 3
        late = db.get(CrawlAttempt, 1)
        late.finished_at, late.succeeded = NOW + timedelta(seconds=20), False
        late.error_type = "timeout"
        record_completed_attempt(db, db.get(CrawlTask, 1), late)
        record_completed_attempt(db, db.get(CrawlTask, 1), late)
        db.commit()
        advance_rollups(db, config, stamp=NOW + timedelta(minutes=1), force_publish=True)
        view = read_monitor(db, config, stamp=NOW + timedelta(minutes=1))
        assert view["attempts"]["total"] == 4 and view["attempts"]["failed"] == 1
        assert db.scalar(select(func.count()).select_from(Fact)) == 4


def test_completion_and_projection_rollbacks_leave_no_lost_or_duplicate_counts(analytics_engine):
    config = Settings()
    with Session(analytics_engine) as db:
        task(db, 1)
        attempt(db, 1, 1, finish=None, success=None)
        activate(db, config, stamp=NOW)
        db.commit()
        row = db.get(CrawlAttempt, 1)
        row.finished_at, row.succeeded = NOW - timedelta(seconds=1), True
        record_completed_attempt(db, db.get(CrawlTask, 1), row)
        db.rollback()
        assert db.get(CrawlAttempt, 1).finished_at is None
        assert db.scalar(select(func.count()).select_from(Fact)) == 0
        row = db.get(CrawlAttempt, 1)
        row.finished_at, row.succeeded = NOW - timedelta(seconds=1), True
        record_completed_attempt(db, db.get(CrawlTask, 1), row)
        db.commit()
        advance_rollups(db, config, stamp=NOW)
        db.rollback()
        assert not db.get(Fact, (1, 1)).processed
        assert db.scalar(select(func.count()).select_from(Minute)) == 0
        advance_rollups(db, config, stamp=NOW)
        db.commit()
        advance_rollups(db, config, stamp=NOW, force_publish=True)
        assert read_monitor(db, config, stamp=NOW)["attempts"]["total"] == 1


def test_reconcile_repairs_legacy_writer_gap_and_hides_incomplete_snapshot_without_double_counting(analytics_engine):
    config = Settings()
    with Session(analytics_engine) as db:
        task(db, 1)
        attempt(db, 1, 1, finish=NOW - timedelta(minutes=3))
        publish_statistics(db)
        assert read_monitor(db, config, stamp=NOW)["attempts"]["total"] == 1
        # An older writer committed during rollback, without creating its fact.
        task(db, 2)
        attempt(db, 2, 2, finish=NOW - timedelta(minutes=2), success=False, error_type="timeout")
        repair_at = NOW + timedelta(minutes=1)
        reconcile(db, config, stamp=repair_at)
        view = read_monitor(db, config, stamp=repair_at)
        assert view["statistics"]["status"] == "warming" and view["attempts"]["total"] is None
        assert db.scalar(select(func.sum(Minute.completed))) == 1
        assert not advance_rollups(db, config, stamp=repair_at, batch_size=1)["ready"]
        db.commit()
    with Session(analytics_engine) as db:
        for _ in range(4):
            if advance_rollups(db, config, stamp=repair_at, batch_size=1)["ready"]:
                break
        view = read_monitor(db, config, stamp=repair_at)
        assert view["statistics"]["ready"] and view["attempts"]["total"] == 2
        assert view["attempts"]["failed"] == 1
        reconcile(db, config, stamp=repair_at)
        advance_rollups(db, config, stamp=repair_at)
        assert read_monitor(db, config, stamp=repair_at)["attempts"]["total"] == 2


def test_source_local_groups_do_not_hide_collection_failure_or_cross_market_data(analytics_engine):
    with Session(analytics_engine) as db:
        task(db, 1)
        task(db, 2, task_type=TaskType.CHANGE_DETECTION)
        task(db, 3, task_type=TaskType.REPORT_GENERATION)
        task(db, 4, region="GB")
        for identifier in range(1, 5):
            attempt(db, identifier, identifier, success=identifier != 1, error_type="timeout" if identifier == 1 else None)
        publish_statistics(db)
        source = read_monitor(db, Settings(), stamp=NOW)
        local = read_monitor(db, Settings(), stamp=NOW, task_group="local")
        all_tasks = read_monitor(db, Settings(), stamp=NOW, task_group="all")
        assert source["task_group"] == "source" and source["attempts"]["success_rate"] == 0
        assert local["attempts"]["total"] == 2 and local["attempts"]["success_rate"] == 100
        assert all_tasks["attempts"]["total"] == 3 and all_tasks["attempts"]["success_rate"] == 66.67
        assert source["task_groups"]["local"] == local["attempts"]
        assert local["recent_failures"] == [] and len(source["recent_failures"]) == 1
        assert all_tasks["statistics"]["p95_approximate"]


def test_completed_minute_boundary_advances_without_new_attempts_and_stale_keeps_own_window(analytics_engine):
    with Session(analytics_engine) as db:
        task(db, 1)
        attempt(db, 1, 1, finish=NOW - timedelta(minutes=15))
        publish_statistics(db)
        old = read_monitor(db, Settings(), 15, stamp=NOW + timedelta(minutes=2))
        assert old["statistics"]["status"] == "stale"
        assert old["attempts"]["total"] == 1 and old["window"]["until"] == NOW.isoformat()
        advance_rollups(db, Settings(), stamp=NOW + timedelta(minutes=2))
        new = read_monitor(db, Settings(), 15, stamp=NOW + timedelta(minutes=2))
        assert new["statistics"]["status"] == "ready" and new["attempts"]["total"] == 0


def test_more_than_twenty_thousand_completions_are_complete_and_request_never_reads_source_attempts(analytics_engine):
    config = Settings()
    with Session(analytics_engine) as db:
        task(db, 1)
        rows = [dict(id=identifier, task_id=1, attempt_no=identifier, started_at=NOW - timedelta(minutes=identifier % 1440 + 1, seconds=1),
            finished_at=NOW - timedelta(minutes=identifier % 1440 + 1), succeeded=identifier % 10 != 0,
            created_at=NOW, request_method="GET") for identifier in range(1, 20_026)]
        db.bulk_insert_mappings(CrawlAttempt, rows)
        publish_statistics(db)
        statements = []
        def capture(conn, cursor, statement, *args):
            statements.append(statement.lower())
        event.listen(analytics_engine, "before_cursor_execute", capture)
        try:
            result = read_monitor(db, config, 1440, stamp=NOW)
        finally:
            event.remove(analytics_engine, "before_cursor_execute", capture)
        assert result["attempts"]["total"] == 20_025
        assert result["attempts"]["failed"] == 2002
        assert result["attempts"]["avg_duration_ms"] == 1000
        assert sum(item["completed"] for item in result["trend"]) == 20_025
        assert all(item["complete"] for item in result["trend"])
        assert not result["attempts"]["sampled"]
        assert all("crawl_attempts" not in sql and "collection_minute_rollups" not in sql for sql in statements)
        assert len(result["recent_failures"]) == 12


def test_histogram_p95_is_upper_bound_without_averaging_percentiles():
    values = [0, 1, 2, 1000, 1_000_000, 9_000_000, 10_000_000, 100_000_000] * 3
    histogram = Counter(histogram_key(value) for value in values)
    actual = sorted(values)[-2]
    result_us = histogram_p95(histogram, len(values)) * 1000
    assert actual <= result_us <= actual * 1.1
    for value in (1, 2, 7, 100, 10_000_000):
        upper = histogram_p95({histogram_key(value): 1}, 1) * 1000
        assert value <= upper <= value * 1.1


def test_retention_only_removes_processed_old_facts_and_is_bounded(analytics_engine):
    config = Settings()
    with Session(analytics_engine) as db:
        for identifier in range(1, 5):
            task(db, identifier)
            attempt(db, identifier, identifier, finish=NOW - timedelta(days=9))
            record_completed_attempt(db, db.get(CrawlTask, identifier), db.get(CrawlAttempt, identifier))
        for identifier in (1, 2, 3):
            db.get(Fact, (1, identifier)).processed = True
        db.flush()
        cleanup(db, config, NOW, batch_size=2)
        assert db.scalar(select(func.count()).select_from(Fact)) == 2
        assert not db.get(Fact, (1, 4)).processed


def test_monitor_fleet_expected_capacity_ignores_stopped_replacement_but_not_unready_worker(analytics_engine):
    with Session(analytics_engine) as db:
        view = read_monitor(db, Settings(), stamp=NOW)
        view["runtime"] = {"status": "available", "source_cooldown_seconds": 0,
            "components": [{"role": "worker", "state": "running", "status": "online"},
                {"role": "worker", "state": "connecting", "status": "online"},
                {"role": "worker", "state": "stopped", "status": "offline"},
                {"role": "scheduler", "state": "running", "status": "online", "last_success_at": NOW.isoformat()}]}
        codes = {item["code"] for item in monitor_alerts(view, expected_workers=2)}
        assert {"worker_capacity_missing", "runtime_attention"}.issubset(codes)
        view["runtime"]["components"][1]["state"] = "running"
        codes = {item["code"] for item in monitor_alerts(view, expected_workers=2)}
        assert "worker_capacity_missing" not in codes and "runtime_attention" not in codes


@pytest.mark.parametrize("value", [0, -1, 65, True, 1.5])
def test_monitor_expected_capacity_validation(value):
    with pytest.raises(ValueError):
        Settings(monitor_expected_workers=value)


def test_monitor_rollup_migration_matches_models():
    from scripts.migrate_collection_monitor_rollups import additive_sql
    assert Path("migrations/016_collection_monitor_rollups.sql").read_text(encoding="utf-8") == additive_sql()


def test_mysql_concurrent_aggregators_and_late_completion_preserve_exact_counts(mysql_engine):
    config = SimpleNamespace(platform="tiktok_shop", region="ZZ", currency="USD", pricing_context="public_listed")
    identifiers, attempt_ids = [], []
    try:
        with Session(mysql_engine) as db:
            for index in range(5):
                source = CrawlTask(platform=config.platform, region=config.region, task_type=TaskType.PRODUCT_DETAIL,
                    resource_type=ResourceType.PRODUCT, resource_id=str(index), payload={}, status=TaskStatus.RUNNING,
                    scheduled_for=NOW, available_at=NOW)
                db.add(source)
                db.flush()
                identifiers.append(source.id)
                row = CrawlAttempt(task_id=source.id, attempt_no=1, started_at=NOW - timedelta(minutes=2),
                    finished_at=NOW - timedelta(minutes=1) if index else None, succeeded=True if index else None)
                db.add(row)
                db.flush()
                attempt_ids.append(row.id)
                if index:
                    record_completed_attempt(db, source, row)
            activate(db, config, stamp=NOW)
            db.commit()
        barrier = Barrier(2)
        def advance():
            with Session(mysql_engine) as db:
                barrier.wait(timeout=5)
                result = advance_rollups(db, config, stamp=NOW)
                db.commit()
                return result
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(advance) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        assert any(result["ready"] for result in results)
        with Session(mysql_engine) as db:
            assert read_monitor(db, config, stamp=NOW)["attempts"]["total"] == 4
            assert db.scalar(select(func.sum(Minute.completed)).where(*scoped(Minute, config))) == 4
            late = db.get(CrawlAttempt, attempt_ids[0])
            late.finished_at, late.succeeded = NOW + timedelta(seconds=10), True
            record_completed_attempt(db, db.get(CrawlTask, identifiers[0]), late)
            db.commit()
            advance_rollups(db, config, stamp=NOW + timedelta(minutes=1))
            db.commit()
            assert read_monitor(db, config, stamp=NOW + timedelta(minutes=1))["attempts"]["total"] == 5
    finally:
        with Session(mysql_engine) as db:
            for model in (Fact, Minute, State):
                db.execute(delete(model).where(*scoped(model, config)))
            keys = [key_for("collection_monitor", snapshot_params(config, minutes, group))
                    for minutes in (15, 60, 360, 1440) for group in ("source", "local", "all")]
            db.execute(delete(AnalyticsSnapshot).where(AnalyticsSnapshot.schema_version == SNAPSHOT_VERSION,
                                                       AnalyticsSnapshot.key_hash.in_(keys)))
            db.execute(delete(CrawlAttempt).where(CrawlAttempt.id.in_(attempt_ids)))
            db.execute(delete(CrawlTask).where(CrawlTask.id.in_(identifiers)))
            db.commit()
