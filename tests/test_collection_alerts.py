"""Incident correctness uses real transactional MySQL and no external services."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.orm import sessionmaker

from app.api.routes_collection_alerts import router
from app.models.collection_alerts import CollectionAlert as Alert, CollectionAlertEvent as Event, CollectionAlertEvaluation as Evaluation
from app.services import collection_alerts
from app.services.collection_alerts import (
    AlertError, acknowledge, evaluate_runtime_once, evaluate_snapshot, evaluation_summary,
    get_alert, list_alerts, list_events, scope,
)
from app.settings import Settings
from app.storage.async_database import AsyncDatabase, make_async_engine
from test_analytics import NOW


CONFIG = Settings(monitor_expected_workers=2, api_token="collection-alert-tests-only-token")
AUTH = {"Authorization": f"Bearer {CONFIG.api_token}"}


def observation(stamp=NOW, *, failing=False, workers=2):
    return {"generated_at": stamp.isoformat(), "scope": {"platform": "tiktok_shop", "region": "US"},
        "task_group": "source", "window": {"minutes": 60},
        "queue": {"status": "fresh", "age_seconds": 0}, "leases": {"expired": 0},
        "statistics": {"ready": True, "status": "ready"},
        "attempts": {"total": 20, "success_rate": 50 if failing else 100},
        "runtime": {"status": "available", "observed_at": stamp.isoformat(), "source_cooldown_seconds": 0,
            "components": [{"id": f"worker-{index}", "role": "worker", "state": "running", "status": "online"}
                for index in range(workers)] + [{"id": "scheduler", "role": "scheduler", "state": "running",
                    "status": "online", "last_success_at": stamp.isoformat()}]}}


def by_code(db, code):
    return db.scalar(select(Alert).where(*scope(Alert, CONFIG), Alert.code == code))


def evaluate(db, *, seconds=0, **kwargs):
    stamp = NOW + timedelta(seconds=seconds)
    return evaluate_snapshot(db, CONFIG, observation(stamp, **kwargs), stamp=stamp)


@pytest.fixture
def alert_factory(mysql_engine):
    factory = sessionmaker(mysql_engine, expire_on_commit=False)
    yield factory
    with factory.begin() as db:
        identifiers = select(Alert.id).where(*scope(Alert, CONFIG))
        db.execute(delete(Event).where(Event.alert_id.in_(identifiers)))
        db.execute(delete(Alert).where(*scope(Alert, CONFIG)))
        db.execute(delete(Evaluation).where(*scope(Evaluation, CONFIG)))


def test_open_acknowledge_resolve_reopen_keeps_history_without_repeat_events(session):
    assert evaluate(session, failing=True)["transitions"] == 1
    row = by_code(session, "high_failure_rate")
    assert row.status == "open" and row.occurrences == 1
    assert evaluate(session, seconds=10, failing=True)["transitions"] == 0
    assert row.last_seen_at == NOW + timedelta(seconds=10)
    assert acknowledge(session, CONFIG, row.id, stamp=NOW + timedelta(seconds=12))["changed"]
    assert not acknowledge(session, CONFIG, row.id)["changed"]
    assert evaluate(session, seconds=20, failing=True)["transitions"] == 0 and row.status == "acknowledged"
    assert evaluate(session, seconds=30)["transitions"] == 1 and row.status == "resolved"
    assert not acknowledge(session, CONFIG, row.id)["changed"]
    assert evaluate(session, seconds=40, failing=True)["transitions"] == 1
    assert row.status == "open" and row.occurrences == 2
    assert row.acknowledged_at is None and row.resolved_at is None and row.first_seen_at == NOW
    history = list_events(session, CONFIG, row.id)["items"]
    assert [item["event_type"] for item in reversed(history)] == ["opened", "acknowledged", "resolved", "reopened"]


def test_unknown_sources_preserve_existing_incidents_and_last_valid_rule_time(session):
    evaluate(session, failing=True, workers=1)
    failure, capacity = by_code(session, "high_failure_rate"), by_code(session, "worker_capacity_missing")
    stamp = NOW + timedelta(seconds=10)
    snapshot = observation(stamp)
    snapshot["statistics"] = {"ready": False, "status": "warming"}
    snapshot["runtime"] = {"status": "unavailable", "observed_at": stamp.isoformat(),
                           "components": [], "source_cooldown_seconds": None}
    evaluate_snapshot(session, CONFIG, snapshot, stamp=stamp)
    assert failure.status == capacity.status == "open"
    assert failure.condition_known is False and capacity.condition_known is False
    assert failure.last_evaluated_at == capacity.last_evaluated_at == NOW
    assert by_code(session, "runtime_unknown").status == "open"
    assert by_code(session, "statistics_warming").status == "open"
    evaluate(session, seconds=20)
    assert failure.status == capacity.status == "resolved"
    assert failure.condition_known and capacity.condition_known


def test_stale_statistics_cannot_open_or_clear_a_failure_incident(session):
    evaluate(session, failing=True)
    row = by_code(session, "high_failure_rate")
    snapshot = observation(NOW + timedelta(seconds=10))
    snapshot["statistics"]["status"] = "stale"
    evaluate_snapshot(session, CONFIG, snapshot, stamp=NOW + timedelta(seconds=10))
    assert row.status == "open" and not row.condition_known
    assert by_code(session, "statistics_stale").status == "open"


def test_failure_window_aging_into_insufficient_samples_is_not_evidence_of_recovery(session):
    evaluate(session, failing=True)
    row = by_code(session, "high_failure_rate")
    snapshot = observation(NOW + timedelta(seconds=10))
    snapshot["attempts"] = {"total": 0, "success_rate": None}
    evaluate_snapshot(session, CONFIG, snapshot, stamp=NOW + timedelta(seconds=10))
    assert row.status == "open" and not row.condition_known
    evaluate(session, seconds=20)
    assert row.status == "resolved"


def test_missing_scheduler_tick_or_missing_process_does_not_resolve_known_stall(session):
    snapshot = observation()
    snapshot["runtime"]["components"][-1]["last_success_at"] = (NOW - timedelta(minutes=10)).isoformat()
    evaluate_snapshot(session, CONFIG, snapshot, stamp=NOW)
    row = by_code(session, "scheduler_tick_stale")
    for second, absent in ((10, False), (20, True)):
        snapshot = observation(NOW + timedelta(seconds=second))
        if absent:
            snapshot["runtime"]["components"].pop()
        else:
            snapshot["runtime"]["components"][-1].pop("last_success_at")
        evaluate_snapshot(session, CONFIG, snapshot, stamp=NOW + timedelta(seconds=second))
        assert row.status == "open" and not row.condition_known
    evaluate(session, seconds=30)
    assert row.status == "resolved"


def test_old_or_stale_evaluations_cannot_overwrite_newer_incidents(session):
    evaluate(session, seconds=20, failing=True)
    row = by_code(session, "high_failure_rate")
    assert evaluate(session, seconds=10)["reason"] == "observation_not_newer"
    assert evaluate_snapshot(session, CONFIG, observation(), stamp=NOW + timedelta(seconds=90))["reason"] == "observation_stale"
    assert row.status == "open"
    assert evaluation_summary(session, CONFIG, stamp=NOW + timedelta(seconds=70))["status"] == "stale"
    assert evaluation_summary(session, CONFIG, stamp=NOW + timedelta(seconds=25))["status"] == "fresh"
    foreign = observation(NOW + timedelta(seconds=30))
    foreign["scope"]["region"] = "GB"
    assert evaluate_snapshot(session, CONFIG, foreign, stamp=NOW + timedelta(seconds=30))["reason"] == "observation_scope_mismatch"
    assert row.status == "open"


def test_metadata_is_never_before_background_evaluation_and_unknown_runtime_is_explicit(session):
    assert evaluation_summary(session, CONFIG, stamp=NOW)["status"] == "never"
    snapshot = observation()
    snapshot["runtime"]["observed_at"] = (NOW - timedelta(seconds=90)).isoformat()
    evaluate_snapshot(session, CONFIG, snapshot, stamp=NOW)
    assert by_code(session, "runtime_unknown").status == "open"
    summary = evaluation_summary(session, CONFIG, stamp=NOW)
    assert summary["sources"]["runtime"] == "unavailable" and summary["status"] == "fresh"


def test_market_scope_and_both_pagination_cursors_are_enforced(session):
    evaluate(session, failing=True, workers=1)
    rows = list_alerts(session, CONFIG, status="all", limit=1)
    assert len(rows["items"]) == 1 and rows["next_before_id"] is not None
    next_page = list_alerts(session, CONFIG, status="all", limit=1, before_id=int(rows["next_before_id"]))
    assert next_page["items"][0]["id"] != rows["items"][0]["id"]
    row = by_code(session, "high_failure_rate")
    acknowledge(session, CONFIG, row.id, stamp=NOW)
    history = list_events(session, CONFIG, row.id, limit=1)
    older = list_events(session, CONFIG, row.id, limit=1, before_id=int(history["next_before_id"]))
    assert older["items"][0]["event_type"] == "opened"
    foreign = SimpleNamespace(platform=CONFIG.platform, region="GB")
    assert list_alerts(session, foreign)["items"] == []
    with pytest.raises(AlertError):
        list_events(session, foreign, row.id)
    with pytest.raises(AlertError):
        acknowledge(session, foreign, row.id)


@pytest.mark.parametrize("healthy", [True, False])
def test_mysql_ack_concurrent_with_evaluation_cannot_lose_or_duplicate_transition(alert_factory, healthy):
    with alert_factory.begin() as db:
        evaluate(db, failing=True)
        identifier = by_code(db, "high_failure_rate").id
    barrier = Barrier(2)
    def ack():
        with alert_factory.begin() as db:
            barrier.wait(timeout=5)
            return acknowledge(db, CONFIG, identifier, stamp=NOW + timedelta(seconds=1))
    def assess():
        with alert_factory.begin() as db:
            barrier.wait(timeout=5)
            return evaluate(db, seconds=2, failing=not healthy)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(ack), pool.submit(assess)]
        for future in futures:
            future.result(timeout=15)
    with alert_factory() as db:
        row = get_alert(db, CONFIG, identifier)
        assert row.status == ("resolved" if healthy else "acknowledged")
        events = list_events(db, CONFIG, identifier)["items"]
        assert sum(item["event_type"] == "opened" for item in events) == 1
        assert sum(item["event_type"] == "acknowledged" for item in events) <= 1
        assert sum(item["event_type"] == "resolved" for item in events) == int(healthy)


def test_mysql_concurrent_repeated_evaluation_creates_one_incident_and_event(alert_factory):
    barrier = Barrier(2)
    def assess():
        with alert_factory.begin() as db:
            barrier.wait(timeout=5)
            return evaluate(db, failing=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(assess) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]
    with alert_factory() as db:
        assert sum(result["applied"] for result in results) == 1
        assert db.scalar(select(func.count()).select_from(Alert).where(*scope(Alert, CONFIG))) == 1
        assert len(list_events(db, CONFIG, by_code(db, "high_failure_rate").id)["items"]) == 1


def test_api_ack_authentication_empty_body_validation_idempotence_and_read_pagination(alert_factory):
    with alert_factory.begin() as db:
        evaluate(db, failing=True, workers=1)
        identifier = by_code(db, "high_failure_rate").id
    app = FastAPI()
    app.state.settings, app.state.factory = CONFIG, alert_factory
    app.include_router(router)
    path = f"/api/v1/collections/alerts/{identifier}/ack"
    with TestClient(app) as client:
        assert client.post(path, json={}).status_code == 401
        assert client.post(path, json={"ignored": True}, headers=AUTH).status_code == 422
        first = client.post(path, json={}, headers=AUTH)
        assert first.status_code == 200 and first.json()["changed"]
        assert not client.post(path, json={}, headers=AUTH).json()["changed"]
        response = client.get("/api/v1/collections/alerts?status=acknowledged&limit=1")
        assert response.status_code == 200 and response.json()["items"][0]["id"] == str(identifier)
        assert client.get("/api/v1/collections/alerts?status=bad").status_code == 422
        assert client.get("/api/v1/collections/alerts?before_id=18446744073709551616").status_code == 422
        history = client.get(f"/api/v1/collections/alerts/{identifier}/events")
        assert history.status_code == 200
        assert CONFIG.api_token not in history.text and "Authorization" not in history.text
        assert client.get("/api/v1/collections/alerts/18446744073709551616/events").status_code == 422
        assert client.get("/api/v1/collections/alerts/18446744073709551615/events").status_code == 404


def test_native_async_background_persists_incidents_without_any_http_request(alert_factory, mysql_engine, monkeypatch):
    async def runtime(_):
        return observation(datetime.now(timezone.utc))["runtime"]
    monkeypatch.setattr(collection_alerts, "read_runtime", runtime)
    async def run():
        database = AsyncDatabase(make_async_engine(mysql_engine.url))
        try:
            result = await evaluate_runtime_once(database, CONFIG)
            assert result["applied"]
        finally:
            await database.close()
    asyncio.run(run())
    with alert_factory() as db:
        assert by_code(db, "statistics_warming").status == "open"
        assert evaluation_summary(db, CONFIG)["status"] == "fresh"


def test_alert_migration_matches_models():
    from scripts.migrate_collection_alerts import additive_sql
    assert Path("migrations/018_collection_alerts.sql").read_text(encoding="utf-8") == additive_sql()
