"""API contracts for source counts, immutable history, and scoped task creation."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.routes as routes
from app.models import CrawlAttempt, CrawlTask, ResourceType, Shop, ShopProfileSnapshot, ShopStatus, TaskStatus, TaskType
from app.settings import Settings


NOW = datetime.now(timezone.utc)
URL = "https://shop.tiktok.com/us/store/chouyatou/7495552971774921097"
TOKEN = "profile-api-test-token-1234567890"


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class ReadSession:
    def __init__(self, scalars=(), rows=()):
        self.values, self.rows, self.queries = iter(scalars), rows, []

    def scalar(self, query):
        self.queries.append(query)
        return next(self.values)

    def scalars(self, query):
        self.queries.append(query)
        return Rows(self.rows)


def shop_row(**overrides):
    values = dict(id=7, platform="tiktok_shop", region="US", platform_shop_id="7495552971774921097",
                  canonical_name="CHOUYATOU", canonical_url=URL, currency="USD", status=ShopStatus.ACTIVE,
                  first_seen_at=NOW - timedelta(days=7), last_seen_at=NOW,
                  follower_count=2 ** 64 - 1, follower_count_text="34.8K+", sales_count=0,
                  sales_count_text="0", video_count=None, video_count_text="1.3K+",
                  profile_captured_at=NOW - timedelta(days=2), profile_source_url=URL,
                  profile_observation_status="partial", profile_parser_version="shop_profile_ssr_1",
                  profile_evidence_path="private/profile.json", profile_response_hash="a" * 64,
                  profile_field_state={"follower_count": {"state": "exact"}, "sales_count": {"state": "exact"},
                                       "video_count": {"state": "display_only"}})
    values.update(overrides)
    return Shop(**values)


def task_row():
    return CrawlTask(id=31, task_id="00000000-0000-0000-0000-000000000031", platform="tiktok_shop", region="US",
                     task_type=TaskType.SHOP_PROFILE, resource_type=ResourceType.SHOP, resource_id="7",
                     status=TaskStatus.RETRY_WAITING, attempt_count=1, max_attempts=3, priority=0,
                     scheduled_for=NOW, available_at=NOW, last_error="private raw error", payload={},
                     checkpoint={"profile_updated": False, "observation_status": "partial", "private": "never expose"})


def snapshot_row(identifier=11):
    shop = shop_row()
    return ShopProfileSnapshot(id=identifier, shop_id=shop.id, task_id=31, platform=shop.platform,
        region=shop.region, platform_shop_id=shop.platform_shop_id, canonical_name=shop.canonical_name,
        canonical_url=URL, scheduled_for=NOW, captured_at=NOW, source_url=URL,
        evidence_path="private/history.json", response_hash="b" * 64, parser_version="shop_profile_ssr_1",
        field_state=shop.profile_field_state, observation_status="partial", **routes.profile_values(shop))


def client_for(db, *, settings=None):
    app = FastAPI()
    app.state.settings = settings or Settings()
    app.state.factory = object()
    app.include_router(routes.router)
    app.dependency_overrides[routes.session_dependency] = lambda: db
    return TestClient(app)


def test_shop_list_preserves_display_text_zero_and_large_counts_without_claiming_freshness():
    db = ReadSession(rows=[shop_row()])
    with client_for(db) as client:
        response = client.get("/api/v1/shops")
    assert response.status_code == 200
    value = response.json()["items"][0]
    assert value["follower_count"] == "18446744073709551615" and value["follower_count_text"] == "34.8K+"
    assert value["sales_count"] == "0" and value["video_count"] is None and value["video_count_text"] == "1.3K+"
    assert value["profile_stale"] and value["profile_status"] == "stale"
    assert value["id"] == "7" and value["platform_shop_id"] == "7495552971774921097"
    assert "private" not in response.text and "response_hash" not in response.text


def test_current_shop_missing_profile_is_not_zero_or_a_fresh_observation():
    shop = shop_row(profile_captured_at=None, follower_count=None, follower_count_text=None, sales_count=None,
                    sales_count_text=None, video_count_text=None, profile_observation_status=None)
    with client_for(ReadSession(scalars=[shop, None])) as client:
        value = client.get("/api/v1/shops/7").json()
    assert value["profile_status"] == "not_captured" and not value["profile_stale"]
    assert value["follower_count"] is None and value["latest_profile_task"] is None


def test_live_coverage_uses_current_market_and_profile_interval(monkeypatch):
    db = ReadSession(rows=[shop_row()])
    seen = {}

    def coverage(session, shops, *, interval_seconds):
        seen.update(shops=shops, interval_seconds=interval_seconds)
        return {"target_shops": 1, "profile_complete_shops": 0, "profile_partial_shops": 1,
                "profile_stale_shops": 1, "field_coverage": {"video_count": {"exact": 0, "display_only": 1}}}

    monkeypatch.setattr(routes, "shop_profile_coverage", coverage)
    settings = Settings(shop_profile_interval_seconds=172800)
    with client_for(db, settings=settings) as client:
        response = client.get("/api/v1/shop-profiles/coverage")
    assert response.status_code == 200 and response.json()["region"] == "US"
    assert response.json()["field_coverage"]["video_count"]["display_only"] == 1
    assert seen["interval_seconds"] == 172800 and len(seen["shops"]) == 1
    sql = str(db.queries[0])
    assert "shops.platform =" in sql and "shops.region =" in sql and "shops.canonical_url IS NOT NULL" in sql


def test_detail_exposes_attempt_code_but_no_raw_error_payload_or_evidence_path():
    attempt = CrawlAttempt(attempt_no=1, succeeded=False, response_status=429, error_type="rate_limited",
        error_message="private cookies", evidence_path="private/attempt.json", request_url="https://private.invalid/",
        started_at=NOW, finished_at=NOW)
    db = ReadSession(scalars=[shop_row(), task_row(), attempt])
    with client_for(db) as client:
        response = client.get("/api/v1/shops/7")
    value = response.json()
    assert value["latest_profile_task"]["status"] == "retry_waiting"
    assert value["latest_profile_task"]["last_attempt"]["error_type"] == "rate_limited"
    assert value["latest_profile_task"]["last_attempt"]["response_status"] == 429
    assert value["latest_profile_task"]["progress"]["observation_status"] == "partial"
    assert "private" not in response.text and "evidence_path" not in response.text and "last_error" not in response.text
    query = str(db.queries[1])
    assert "crawl_tasks.platform =" in query and "crawl_tasks.region =" in query and "crawl_tasks.resource_id =" in query


def test_history_has_bounded_cursor_utc_filters_and_market_scope():
    db = ReadSession(scalars=[shop_row()], rows=[snapshot_row(11), snapshot_row(12)])
    with client_for(db) as client:
        response = client.get("/api/v1/shops/7/snapshots", params={"limit": 1, "after_id": "10",
            "captured_from": "2026-10-15T08:00:00+08:00", "captured_to": "2026-10-16T08:00:00+08:00"})
    value = response.json()
    assert response.status_code == 200 and len(value["items"]) == 1 and value["next_after_id"] == "11"
    assert value["items"][0]["follower_count"] == "18446744073709551615"
    assert "private" not in response.text and "evidence_path" not in response.text
    query = db.queries[-1]
    sql = str(query)
    assert "shop_profile_snapshots.platform =" in sql and "shop_profile_snapshots.region =" in sql
    assert "shop_profile_snapshots.shop_id =" in sql and "shop_profile_snapshots.id >" in sql
    params = query.compile().params
    dates = [value for value in params.values() if isinstance(value, datetime)]
    assert dates == [datetime(2026, 10, 15, tzinfo=timezone.utc), datetime(2026, 10, 16, tzinfo=timezone.utc)]


@pytest.mark.parametrize("params", [
    {"limit": 101}, {"after_id": -1}, {"after_id": str(2 ** 64)},
    {"captured_from": "2026-10-15T00:00:00"},
    {"captured_from": "2026-10-16T00:00:00Z", "captured_to": "2026-10-15T00:00:00Z"},
])
def test_history_rejects_unbounded_or_ambiguous_requests_before_database_access(params):
    db = ReadSession()
    with client_for(db) as client:
        assert client.get("/api/v1/shops/7/snapshots", params=params).status_code == 422
    assert not db.queries


@pytest.mark.parametrize("suffix", ["", "/snapshots"])
def test_unknown_or_other_market_shop_is_not_exposed(suffix):
    db = ReadSession(scalars=[None])
    with client_for(db) as client:
        assert client.get(f"/api/v1/shops/7{suffix}").status_code == 404
    sql = str(db.queries[0])
    assert "shops.platform =" in sql and "shops.region =" in sql


def test_shop_profile_task_creation_keeps_explicit_window_and_idempotent_task(monkeypatch):
    shop, task = shop_row(), task_row()
    db = ReadSession(scalars=[shop, task, shop, task])
    seen, windows = set(), []

    @contextmanager
    def transaction(factory):
        yield db

    def ensure(session, row, window):
        windows.append(window)
        created = window not in seen
        seen.add(window)
        return created

    monkeypatch.setattr(routes, "session_scope", transaction)
    monkeypatch.setattr(routes, "handler_registry", lambda: {TaskType.SHOP_PROFILE: object()})
    monkeypatch.setattr(routes, "ensure_shop_profile_task", ensure)
    settings = Settings(api_token=TOKEN, enabled_task_types=(TaskType.SHOP_PROFILE,))
    body = {"task_type": "shop_profile", "resource_id": "7", "scheduled_for": "2026-10-15T08:00:00+08:00"}
    with client_for(db, settings=settings) as client:
        first = client.post("/api/v1/tasks", headers={"Authorization": f"Bearer {TOKEN}"}, json=body)
        second = client.post("/api/v1/tasks", headers={"Authorization": f"Bearer {TOKEN}"}, json=body)
    assert first.status_code == second.status_code == 200
    assert first.json()["created"] and not second.json()["created"]
    assert first.json()["task"]["task_id"] == second.json()["task"]["task_id"]
    assert windows == [datetime(2026, 10, 15, tzinfo=timezone.utc)] * 2


def test_profile_task_rejects_unverified_url(monkeypatch):
    db = ReadSession(scalars=[shop_row(canonical_url="https://example.com/store/7495552971774921097")])

    @contextmanager
    def transaction(factory):
        yield db

    monkeypatch.setattr(routes, "session_scope", transaction)
    monkeypatch.setattr(routes, "handler_registry", lambda: {TaskType.SHOP_PROFILE: object()})
    settings = Settings(api_token=TOKEN, enabled_task_types=(TaskType.SHOP_PROFILE,))
    with client_for(db, settings=settings) as client:
        response = client.post("/api/v1/tasks", headers={"Authorization": f"Bearer {TOKEN}"},
                               json={"task_type": "shop_profile", "resource_id": "7"})
    assert response.status_code == 409 and response.json()["detail"] == "shop_url_not_verified"


def test_mysql_history_reads_only_current_market_and_paginates_without_duplicates(session):
    """Exercise real SQL scope and cursor behavior in the disposable MySQL fixture."""
    local = Shop(platform="tiktok_shop", region="US", platform_shop_id="profile-api-us", canonical_name="US shop",
                 canonical_url=URL, currency="USD", status=ShopStatus.ACTIVE)
    foreign = Shop(platform="tiktok_shop", region="GB", platform_shop_id="profile-api-gb", canonical_name="GB shop",
                   canonical_url="https://shop.tiktok.com/gb/store/example/12345", currency="GBP", status=ShopStatus.ACTIVE)
    session.add_all([local, foreign])
    session.flush()
    for offset in range(3):
        captured = NOW - timedelta(days=offset)
        task = CrawlTask(platform="tiktok_shop", region="US", task_type=TaskType.SHOP_PROFILE,
                         resource_type=ResourceType.SHOP, resource_id=str(local.id), payload={},
                         scheduled_for=captured, available_at=captured)
        session.add(task)
        session.flush()
        session.add(ShopProfileSnapshot(shop_id=local.id, task_id=task.id, platform="tiktok_shop", region="US",
            platform_shop_id=local.platform_shop_id, canonical_name=local.canonical_name, canonical_url=URL,
            scheduled_for=captured, captured_at=captured, source_url=URL, response_hash="b" * 64,
            parser_version="shop_profile_ssr_1", field_state={}, observation_status="partial"))
    session.flush()
    with client_for(session) as client:
        assert client.get(f"/api/v1/shops/{foreign.id}").status_code == 404
        assert client.get(f"/api/v1/shops/{foreign.id}/snapshots").status_code == 404
        first = client.get(f"/api/v1/shops/{local.id}/snapshots", params={"limit": 2}).json()
        second = client.get(f"/api/v1/shops/{local.id}/snapshots", params={"limit": 2, "after_id": first["next_after_id"]}).json()
    assert len(first["items"]) == 2 and len(second["items"]) == 1 and second["next_after_id"] is None
    assert len({row["id"] for row in first["items"] + second["items"]}) == 3
