import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from app.api.routes_collection_monitor import MonitorCache, router
from app.models import CrawlAttempt, CrawlTask, ResourceType, TaskStatus, TaskType
from app.services import collection_monitor
from app.services.analytics_refresh import scope
from app.services.analytics_snapshot import publish_snapshot
from app.services.collection_heartbeat import RuntimeHeartbeat, prefix, read_runtime
from app.services.collection_monitor import monitor_alerts, read_monitor
from app.settings import Settings
from test_analytics import NOW, analytics_engine


def task(db, identifier, *, region="US", **values):
    defaults = dict(id=identifier, task_id=f"task-{identifier}", platform="tiktok_shop", region=region,
        task_type=TaskType.PRODUCT_DETAIL, resource_type=ResourceType.PRODUCT, resource_id=str(identifier),
        payload={"private": "do-not-return-secret"}, checkpoint={"private": "do-not-return-secret"},
        priority=0, status=TaskStatus.SUCCEEDED, attempt_count=1, max_attempts=3,
        scheduled_for=NOW, available_at=NOW, created_at=NOW, updated_at=NOW,
        last_error="do-not-return-secret")
    db.add(CrawlTask(**{**defaults, **values}))
    db.flush()


def attempt(db, identifier, task_id, *, finish=NOW, success=True, duration=2, **values):
    defaults = dict(id=identifier, task_id=task_id, attempt_no=1, worker_id="test-worker",
        started_at=finish - timedelta(seconds=duration) if finish else NOW,
        finished_at=finish, succeeded=success, created_at=NOW,
        request_url="https://example.invalid/?token=do-not-return-secret",
        error_message="do-not-return-secret", evidence_path="do-not-return-secret", request_method="GET")
    db.add(CrawlAttempt(**{**defaults, **values}))
    db.flush()


def test_window_is_scoped_by_finished_attempts_and_includes_boundaries(analytics_engine):
    with Session(analytics_engine) as db:
        for identifier in range(1, 8):
            task(db, identifier, region="GB" if identifier == 6 else "US")
        attempt(db, 1, 1, finish=NOW - timedelta(minutes=60))
        attempt(db, 2, 2, finish=NOW)
        attempt(db, 3, 3, finish=NOW - timedelta(minutes=60, microseconds=1))
        attempt(db, 4, 4, finish=NOW + timedelta(microseconds=1))
        attempt(db, 5, 5, finish=None, success=None)
        attempt(db, 6, 6)
        attempt(db, 7, 7, success=False, duration=4, error_type="timeout")
        result = read_monitor(db, Settings(), 60, stamp=NOW)
        assert result["attempts"]["total"] == 3
        assert result["attempts"]["success_rate"] == 66.67
        assert result["attempts"]["avg_duration_ms"] == 2666.7
        assert result["attempts"]["p95_duration_ms"] == 4000
        assert len(result["trend"]) == 12
        assert sum(bucket["completed"] for bucket in result["trend"]) == 3
        assert result["trend"][0]["completed"] == 1
        assert result["trend"][-1]["completed"] == 2
        assert result["errors"] == [{"error_type": "timeout", "count": 1}]


def test_attempt_retry_semantics_and_private_data_are_not_exposed(analytics_engine):
    with Session(analytics_engine) as db:
        task(db, 1)
        attempt(db, 1, 1, success=False, error_type="token=do-not-return-secret")
        attempt(db, 2, 1, attempt_no=2, success=True)
        result = read_monitor(db, Settings(), stamp=NOW)
        assert result["attempts"]["total"] == 2
        assert result["attempts"]["success_rate"] == 50
        assert result["recent_failures"][0]["error_type"] == "unknown"
        assert "do-not-return-secret" not in json.dumps(result)


def test_empty_window_and_missing_summary_stay_unknown(analytics_engine):
    with Session(analytics_engine) as db:
        result = read_monitor(db, Settings(), stamp=NOW)
        assert result["attempts"]["total"] == 0
        assert result["attempts"]["success_rate"] is None
        assert result["attempts"]["p95_duration_ms"] is None
        assert result["queue"]["status"] == "pending"
        assert result["queue"]["total"] is None
        assert result["leases"]["items"] == []


def test_queue_reuses_published_snapshot_and_preserves_its_age(analytics_engine):
    config = Settings()
    with Session(analytics_engine) as db:
        publish_snapshot(db, "task_counts", scope(config), 1, {"tasks": [
            {"task_type": "product_detail", "status": "pending", "count": 8},
            {"task_type": "product_detail", "status": "succeeded", "count": 12},
            {"task_type": "shop_profile", "status": "dead", "count": 2}]}, NOW - timedelta(seconds=70))
        db.commit()
        statements = []
        def capture(conn, cursor, statement, *args):
            statements.append(statement.lower())
        event.listen(analytics_engine, "before_cursor_execute", capture)
        try:
            result = read_monitor(db, config, stamp=NOW)
        finally:
            event.remove(analytics_engine, "before_cursor_execute", capture)
        assert result["queue"]["status"] == "stale"
        assert result["queue"]["age_seconds"] == 70
        assert result["queue"]["total"] == 22
        assert result["queue"]["active"] == 8 and result["queue"]["failed"] == 2
        assert len(statements) == 3
        assert not any("count(" in sql or "group by" in sql for sql in statements)
        assert all("crawl_tasks.payload" not in sql and "crawl_tasks.checkpoint" not in sql for sql in statements)


def test_attempt_and_lease_overflows_are_explicit(analytics_engine, monkeypatch):
    monkeypatch.setattr(collection_monitor, "MAX_ATTEMPTS", 2)
    monkeypatch.setattr(collection_monitor, "MAX_LEASES", 2)
    with Session(analytics_engine) as db:
        for identifier in range(1, 5):
            task(db, identifier, status=TaskStatus.RUNNING,
                 lease_expires_at=NOW + timedelta(seconds=identifier - 2))
            attempt(db, identifier, identifier, finish=NOW - timedelta(seconds=identifier))
        result = read_monitor(db, Settings(), stamp=NOW)
        assert result["attempts"]["truncated"] and result["attempts"]["total"] == 2
        assert result["leases"]["truncated"] and result["leases"]["expired"] == 2
        assert result["leases"]["active"] == 0


def test_monitor_route_validates_window_and_coalesces_repeated_reads(analytics_engine, monkeypatch):
    app = FastAPI()
    app.state.settings, app.state.factory = Settings(), sessionmaker(analytics_engine)
    app.include_router(router)
    calls = []
    from app.api import routes_collection_monitor
    original = routes_collection_monitor.read_monitor
    def record(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(routes_collection_monitor, "read_monitor", record)
    with TestClient(app) as client:
        assert client.get("/api/v1/collections/monitor?window_minutes=5").status_code == 422
        assert client.get("/api/v1/collections/monitor?window_minutes=16").status_code == 422
        first = client.get("/api/v1/collections/monitor?window_minutes=60")
        second = client.get("/api/v1/collections/monitor?window_minutes=60")
        assert first.status_code == 200
        assert first.json() == second.json()
        assert first.headers["cache-control"] == "no-store"
        assert first.json()["runtime"]["status"] == "not_configured"
        assert len(calls) == 1


def test_monitor_cache_cancellation_does_not_cancel_other_waiters():
    async def run():
        cache, ready = MonitorCache(), asyncio.Event()
        async def build():
            await ready.wait()
            return {"ok": True}
        first = asyncio.create_task(cache.get(60, build))
        second = asyncio.create_task(cache.get(60, build))
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        ready.set()
        assert await second == {"ok": True}
        await cache.close()
    asyncio.run(run())


class MemoryRedis:
    def __init__(self, *, broken=False):
        self.values, self.scores, self.commands = {}, {}, []
        self.broken = broken

    def pipeline(self, **kwargs):
        return MemoryPipeline(self)

    async def zrevrangebyscore(self, key, high, low, **kwargs):
        if self.broken:
            raise OSError("redis://secret:password@private.invalid")
        return [identifier for identifier, score in sorted(self.scores.items(), key=lambda item: -item[1]) if score >= low][:64]


class MemoryPipeline:
    def __init__(self, client):
        self.client, self.commands = client, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def __getattr__(self, method):
        def call(*args, **kwargs):
            self.commands.append((method, args, kwargs))
            return self
        return call

    async def execute(self):
        if self.client.broken:
            raise OSError("redis://secret:password@private.invalid")
        result = []
        self.client.commands.extend(self.commands)
        for method, args, kwargs in self.commands:
            if method == "set":
                self.client.values[args[0]] = args[1]
            elif method == "zadd":
                self.client.scores.update(args[1])
            elif method == "get":
                result.append(self.client.values.get(args[0]))
            elif method == "pttl":
                result.append(1400)
        return result


def test_idle_worker_heartbeat_is_real_presence_and_whitelists_state():
    async def run():
        client, config = MemoryRedis(), Settings(enabled_task_types=(TaskType.PRODUCT_DETAIL,))
        heartbeat = RuntimeHeartbeat(config, "worker", "worker-1", {"state": "running",
            "concurrency": 4, "active_tasks": 0, "connection": "do-not-return-secret"}, client=client)
        assert await heartbeat.publish()
        result = await read_runtime(config, client=client)
        assert result["status"] == "available"
        assert result["components"][0]["status"] == "online"
        assert result["components"][0]["active_tasks"] == 0
        assert result["source_cooldown_seconds"] == 1.4
        assert "do-not-return-secret" not in json.dumps(client.values)
        assert any(command[0] == "zremrangebyrank" for command in client.commands)
        assert any(command[0] == "set" and command[2]["ex"] == 600 for command in client.commands)
    asyncio.run(run())


def test_runtime_staleness_and_unavailable_are_different():
    async def run():
        config, client = Settings(), MemoryRedis()
        for identifier, age in (("worker-1", 0), ("worker-2", 35), ("worker-3", 90)):
            seen = NOW - timedelta(seconds=age)
            client.scores[identifier] = seen.timestamp()
            client.values[f"{prefix(config)}:component:{identifier}"] = json.dumps({"id": identifier,
                "role": "worker", "seen_at": seen.isoformat(), "state": "running"})
        result = await read_runtime(config, client=client, stamp=NOW)
        assert [item["status"] for item in result["components"]] == ["online", "stale", "offline"]
        client.broken = True
        result = await read_runtime(config, client=client, stamp=NOW)
        assert result["status"] == "unavailable" and result["components"] == []
        assert "password" not in json.dumps(result)
        heartbeat = RuntimeHeartbeat(config, "scheduler", "scheduler-1", {}, client=client)
        assert await heartbeat.publish() is False
    asyncio.run(run())


def test_bad_heartbeat_cannot_crash_collection():
    async def run():
        def broken_state():
            raise RuntimeError("private connection details")
        heartbeat = RuntimeHeartbeat(Settings(), "worker", "worker-1", broken_state, client=MemoryRedis())
        assert await heartbeat.publish() is False
    asyncio.run(run())


def test_graceful_shutdown_marks_retained_heartbeat_offline():
    async def run():
        client, config = MemoryRedis(), Settings()
        heartbeat = RuntimeHeartbeat(config, "worker", "worker-1", {"state": "running"}, client=client)
        heartbeat.start()
        await asyncio.sleep(0)
        await heartbeat.close()
        result = await read_runtime(config, client=client)
        assert result["components"][0]["state"] == "stopped"
        assert result["components"][0]["status"] == "offline"
    asyncio.run(run())


def test_available_redis_without_heartbeats_is_not_a_healthy_fleet(analytics_engine):
    with Session(analytics_engine) as db:
        result = read_monitor(db, Settings(), stamp=NOW)
        result["runtime"] = {"status": "available", "components": [], "source_cooldown_seconds": 0}
        codes = {alert["code"] for alert in monitor_alerts(result)}
        assert {"worker_heartbeat_missing", "scheduler_heartbeat_missing"}.issubset(codes)
        result["runtime"]["components"] = [{"role": "worker", "status": "online", "state": "running"}]
        codes = {alert["code"] for alert in monitor_alerts(result)}
        assert "worker_heartbeat_missing" not in codes and "scheduler_heartbeat_missing" in codes


@pytest.mark.parametrize("seconds_ago,poll_seconds,expected", [
    (0, 5, None), (120, 5, None), (121, 5, "scheduler_tick_stale"),
    (500, 300, None), (901, 300, "scheduler_tick_stale"),
    (None, 5, "scheduler_tick_unknown"), (-60, 5, "scheduler_tick_unknown"),
])
def test_scheduler_progress_uses_sample_time_separately_from_live_heartbeat(seconds_ago, poll_seconds, expected):
    component = {"role": "scheduler", "status": "online", "state": "running",
                 "seen_at": NOW.isoformat()}
    if seconds_ago is not None:
        component["last_success_at"] = (NOW - timedelta(seconds=seconds_ago)).isoformat()
    snapshot = {"generated_at": (NOW - timedelta(hours=1)).isoformat(),
        "queue": {"status": "fresh", "age_seconds": 0},
        "attempts": {"total": 0, "truncated": False}, "leases": {"expired": 0},
        "runtime": {"status": "available", "observed_at": NOW.isoformat(),
                    "components": [component], "source_cooldown_seconds": 0}}
    codes = {alert["code"] for alert in monitor_alerts(snapshot, scheduler_poll_seconds=poll_seconds)}
    assert codes.intersection({"scheduler_tick_stale", "scheduler_tick_unknown"}) == ({expected} if expected else set())
    assert component["status"] == "online"


@pytest.mark.parametrize("last_success_at", ["not-a-timestamp", "2026-10-17T00:00:00"])
def test_invalid_scheduler_progress_is_unknown_without_crashing_or_claiming_stall(last_success_at):
    snapshot = {"generated_at": NOW.isoformat(), "queue": {"status": "fresh", "age_seconds": 0},
        "attempts": {"total": 0, "truncated": False}, "leases": {"expired": 0},
        "runtime": {"status": "available", "observed_at": NOW.isoformat(), "source_cooldown_seconds": 0,
            "components": [{"role": "scheduler", "status": "online", "state": "running", "last_success_at": last_success_at}]}}
    codes = {alert["code"] for alert in monitor_alerts(snapshot)}
    assert "scheduler_tick_unknown" in codes and "scheduler_tick_stale" not in codes


def test_monitor_migration_matches_model():
    from scripts.migrate_collection_monitor_indexes import additive_sql
    assert Path("migrations/013_collection_monitor_indexes.sql").read_text(encoding="utf-8") == additive_sql()


def test_mysql_recent_attempts_use_timestamp_index_without_filesort(session):
    task(session, 1)
    attempt(session, 1, 1)
    statements = []
    engine = session.get_bind()
    def capture(conn, cursor, statement, parameters, *args):
        if "ix_crawl_attempts_monitor" in statement and not statement.startswith("EXPLAIN"):
            statements.append((statement, parameters))
    event.listen(engine, "before_cursor_execute", capture)
    try:
        result = read_monitor(session, Settings(), stamp=NOW)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert result["attempts"]["total"] == 1
    sql, parameters = statements[0]
    plan = session.connection().exec_driver_sql("EXPLAIN " + sql, parameters).mappings().all()
    assert plan[0]["table"] == "crawl_attempts"
    assert plan[0]["key"] == "ix_crawl_attempts_monitor"
    assert all("filesort" not in (row["Extra"] or "").lower() for row in plan)


def test_monitor_native_async_api_has_no_event_loop_or_greenlet_mismatch(mysql_engine):
    import httpx
    from app.storage.async_database import AsyncDatabase, make_async_engine
    async def run():
        database = AsyncDatabase(make_async_engine(mysql_engine.url))
        app = FastAPI()
        app.state.settings, app.state.database, app.state.factory = Settings(), database, database.sync_factory
        app.include_router(router)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
                response = await client.get("/api/v1/collections/monitor?window_minutes=15")
                assert response.status_code == 200
                assert response.json()["window"]["minutes"] == 15
        finally:
            if hasattr(app.state, "collection_monitor_cache"):
                await app.state.collection_monitor_cache.close()
            await database.close()
    asyncio.run(run())
