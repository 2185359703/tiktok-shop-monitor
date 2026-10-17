from dataclasses import replace
from pathlib import Path
from uuid import uuid4
from datetime import datetime, timezone
from threading import Event

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.main import create_app
from app.models import CollectionLaunch, CollectionLaunchTask, CollectionOperation, CrawlTask, Product, SeedShopObservation, Shop, TaskStatus, TaskType
from app.services.collection_intake import create_launch, launch_summary
from app.services.collection_management import ManagementError, set_control, retry_task
from app.queue.retry_policy import Failure
from app.services.product_discovery import persist_product_page
from app.services.task_runtime import ExecutionResult, claim_task, database_now, finalize_task
from app.settings import Settings
from scripts.migrate_collection_launches import additive_sql
from test_product_list import SHOP_ID, URL, snapshot
from app.crawlers.public_shop import PublicPageSnapshot, ShopCandidate
from app.workers.contracts import HandlerContext
from app.workers.handlers import ShopDiscoveryHandler


CONFIG = Settings(enabled_task_types=tuple(TaskType), api_token="collection-launch-test-only-token")
PREFIX = "/api/v1/collection-management"
AUTH = {"Authorization": f"Bearer {CONFIG.api_token}"}


def store(db, *, platform_id=SHOP_ID, region="US"):
    row = Shop(platform="tiktok_shop", region=region, platform_shop_id=platform_id,
        canonical_name="Intake fixture", canonical_url=URL if platform_id == SHOP_ID else f"https://shop.tiktok.com/us/store/fixture/{platform_id}")
    db.add(row)
    db.flush()
    return row


def product(db, shop):
    pid = str(uuid4().int)[:18]
    row = Product(platform="tiktok_shop", region=shop.region, shop_id=shop.id, platform_product_id=pid,
        title="Intake product", product_url=f"https://shop.tiktok.com/us/pdp/{pid}", currency="USD", pricing_context="public_listed")
    db.add(row)
    db.flush()
    return row


def definition(ids=None, *, kind=TaskType.PRODUCT_DISCOVERY, urls=None, pages=None):
    return {"name": "明确范围的测试批次", "task_type": kind.value,
        "resource_ids": [str(value) for value in ids] if ids is not None else None,
        "shop_urls": urls, "max_pages": pages}


def roots(db, launch):
    return db.scalars(select(CrawlTask).join(CollectionLaunchTask, CollectionLaunchTask.task_id == CrawlTask.id)
        .where(CollectionLaunchTask.launch_id == launch.id, CollectionLaunchTask.member_role == "root")).all()


def test_launch_replay_preserves_exact_work_even_after_limits_change(session):
    shop = store(session)
    request = uuid4()
    body = definition([shop.id])
    launch, created = create_launch(session, CONFIG, body, request)
    assert created and launch.created_task_count == launch.requested_count == 1
    task = roots(session, launch)[0]
    assert task.payload["root_task_id"] == task.task_id and task.payload["max_pages"] == CONFIG.product_discovery_max_pages
    assert task.payload["collection_tracked"]
    constrained = replace(CONFIG, enabled_task_types=(), product_discovery_max_pages=1, dispatch_batch_size=1)
    same, created = create_launch(session, constrained, body, request)
    assert not created and same.id == launch.id and roots(session, same)[0].id == task.id
    with pytest.raises(ManagementError, match="idempotency_conflict"):
        create_launch(session, CONFIG, {**body, "name": "changed"}, request)
    assert session.scalar(select(func.count()).select_from(CollectionOperation).where(CollectionOperation.action == "collection_launched")) == 1


def test_new_discovery_tracks_real_pages_details_and_does_not_claim_root_is_complete_batch(session):
    shop = store(session)
    launch, _ = create_launch(session, CONFIG, definition([shop.id], pages=2), uuid4())
    task = roots(session, launch)[0]
    lease = claim_task(session, task.task_id, task.task_type, "intake-fixture", CONFIG)
    result = ExecutionResult(apply=lambda db, current: persist_product_page(
        db, current, snapshot(), page_number=1, root_task_id=current.task_id, max_pages=2))
    finalize_task(session, lease, CONFIG, result=result)
    detail = launch_summary(session, launch)
    assert detail["root_status_counts"] == {"succeeded": 1}
    assert detail["descendant_task_count"] == 2 and detail["descendant_status_counts"] == {"pending": 2}
    assert detail["status"] == "queued"
    children = session.scalars(select(CrawlTask).join(CollectionLaunchTask, CollectionLaunchTask.task_id == CrawlTask.id)
        .where(CollectionLaunchTask.launch_id == launch.id, CollectionLaunchTask.member_role == "descendant")).all()
    assert {task.task_type for task in children} == {TaskType.PRODUCT_DISCOVERY, TaskType.PRODUCT_DETAIL}
    assert all(task.payload["collection_tracked"] for task in children)


def test_seed_import_cleans_public_links_and_reports_reused_roots(session):
    url = "https://shop.tiktok.com/us/store/fixture/7490000000000000001"
    body = definition(kind=TaskType.SHOP_DISCOVERY, urls=[url + "?tracking=do-not-store", url])
    first, _ = create_launch(session, CONFIG, body, uuid4())
    assert first.requested_count == first.created_task_count == 1
    seed = session.scalar(select(SeedShopObservation))
    assert seed.source_url == seed.shop_url == seed.seed_value == url
    assert "do-not-store" not in str(seed.raw_record)
    again, created = create_launch(session, CONFIG, body, uuid4())
    assert created and again.created_task_count == 0 and again.reused_task_count == 1
    assert roots(session, first)[0].id == roots(session, again)[0].id


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/us/store/f/1", "https://shop.tiktok.com.evil.test/us/store/f/1",
    "https://user:password@shop.tiktok.com/us/store/f/1", "https://shop.tiktok.com/us/k/category",
    "http://shop.tiktok.com/us/store/f/1", "https://shop.tiktok.com:8443/us/store/f/1",
])
def test_import_rejects_non_store_or_untrusted_entries_without_echoing_input(session, url):
    with pytest.raises(ManagementError, match="invalid_shop_entry") as error:
        create_launch(session, CONFIG, definition(kind=TaskType.SHOP_DISCOVERY, urls=[url]), uuid4())
    assert url not in str(error.value)
    assert session.scalar(select(func.count()).select_from(CollectionLaunch)) == 0


def test_only_explicit_eligible_scopes_and_budgets_can_launch(session):
    shop = store(session)
    foreign = store(session, platform_id="7490000000000000009", region="GB")
    for body, code in [
        (definition([foreign.id]), "invalid_launch_resources"),
        (definition([shop.id], pages=CONFIG.product_discovery_max_pages + 1), "invalid_launch_budget"),
        (definition([shop.id], urls=[URL]), "invalid_launch_scope"),
    ]:
        with pytest.raises(ManagementError, match=code):
            with session.begin_nested():
                create_launch(session, CONFIG, body, uuid4())
    assert session.scalar(select(func.count()).select_from(CollectionLaunch)) == 0
    set_control(session, CONFIG, True)
    with pytest.raises(ManagementError, match="collection_paused"):
        create_launch(session, CONFIG, definition([shop.id]), uuid4())


def test_another_active_launch_for_same_resource_is_rejected_atomically(session):
    shop = store(session)
    create_launch(session, CONFIG, definition([shop.id]), uuid4())
    with pytest.raises(ManagementError, match="collection_resource_busy"):
        with session.begin_nested():
            create_launch(session, CONFIG, definition([shop.id]), uuid4())
    assert session.scalar(select(func.count()).select_from(CollectionLaunch)) == 1
    assert session.scalar(select(func.count()).select_from(CrawlTask)) == 1


@pytest.mark.parametrize("kind", [TaskType.PRODUCT_DETAIL, TaskType.PRODUCT_REVIEWS])
def test_product_intake_does_not_require_a_previous_successful_capture(session, kind):
    shop = store(session)
    item = product(session, shop)
    assert item.last_success_at is None
    launch, _ = create_launch(session, CONFIG, definition([item.id], kind=kind), uuid4())
    task = roots(session, launch)[0]
    assert task.resource_id == str(item.id) and task.payload["platform_shop_id"] == shop.platform_shop_id
    assert task.payload["product_id"] == str(item.id)
    if kind == TaskType.PRODUCT_REVIEWS:
        assert task.payload["page_size"] == 5 and task.payload["seen_page_hashes"] == []


def test_seed_retry_uses_its_own_lease_and_preserves_root_failure(session, monkeypatch, tmp_path):
    url = "https://shop.tiktok.com/us/store/retry-fixture/7490000000000000666"
    launch, _ = create_launch(session, CONFIG, definition(kind=TaskType.SHOP_DISCOVERY, urls=[url]), uuid4())
    original = roots(session, launch)[0]
    lease = claim_task(session, original.task_id, original.task_type, "first-fixture", CONFIG)
    finalize_task(session, lease, CONFIG, failure=Failure("source_unavailable", "Synthetic failure"))
    child, created = retry_task(session, CONFIG, original.task_id)
    assert created and original.status == TaskStatus.DEAD
    lease = claim_task(session, child.task_id, child.task_type, "retry-fixture", CONFIG)
    stamp = datetime.now(timezone.utc)
    page = PublicPageSnapshot(url, url, 200, 100, "b" * 64, 10, 1, stamp, stamp,
        (ShopCandidate("7490000000000000666", "Retry fixture", ("Retry fixture",), url, "US", ("fixture",)),))
    monkeypatch.setattr("app.workers.handlers.TikTokShopAdapter.discover_shops", lambda *args: page)
    config = replace(CONFIG, evidence_directory=tmp_path)
    result = ShopDiscoveryHandler().prepare(lease, HandlerContext(config, None, Event()))
    finalize_task(session, lease, config, result=result)
    assert original.status == TaskStatus.DEAD and child.status == TaskStatus.SUCCEEDED
    assert session.scalar(select(SeedShopObservation)).resolution_status == "resolved"
    summary = launch_summary(session, launch)
    assert summary["root_status_counts"] == {"dead": 1}
    assert summary["descendant_status_counts"] == {"pending": 2, "succeeded": 1}
    assert summary["status"] == "queued"
    same, created = retry_task(session, CONFIG, original.task_id)
    assert not created and same.id == child.id


def test_launch_api_authorization_bounds_history_pagination_and_market_isolation(session, mysql_engine):
    shops = [store(session, platform_id="749000000000000000" + str(i)) for i in range(1, 3)]
    foreign = CollectionLaunch(platform="tiktok_shop", region="GB", name="Isolated fixture", task_type=TaskType.PRODUCT_DISCOVERY,
        request_key="a" * 64, request_hash="b" * 64, requested_count=1)
    session.add(foreign)
    session.flush()
    factory = sessionmaker(bind=session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint")
    with TestClient(create_app(CONFIG, mysql_engine)) as client:
        client.app.state.factory = factory
        body = {**definition([shop.id for shop in shops]), "request_id": str(uuid4())}
        assert client.post(PREFIX + "/launches", json=body).status_code == 401
        invalid = client.post(PREFIX + "/launches", json={**body, "resource_ids": ["18446744073709551616"]}, headers=AUTH)
        assert invalid.status_code == 422
        response = client.post(PREFIX + "/launches", json=body, headers=AUTH)
        assert response.status_code == 200, response.text
        launch_id = response.json()["launch"]["id"]
        assert response.json()["created"]
        assert not client.post(PREFIX + "/launches", json=body, headers=AUTH).json()["created"]
        page = client.get(PREFIX + f"/launches/{launch_id}?limit=1").json()
        assert len(page["tasks"]) == 1 and page["next_before_id"]
        second = client.get(PREFIX + f"/launches/{launch_id}", params={"limit": 1, "before_id": page["next_before_id"]}).json()
        assert second["tasks"][0]["id"] != page["tasks"][0]["id"] and second["next_before_id"] is None
        assert client.get(PREFIX + "/launches?task_type=product_discovery").json()["items"][0]["id"] == launch_id
        assert client.get(PREFIX + "/launches/18446744073709551616").status_code == 422
        assert client.get(PREFIX + "/launches/9999999999").status_code == 404
        assert client.post(PREFIX + f"/launches/{launch_id}/cancel", json={}).status_code == 401
        cancelled = client.post(PREFIX + f"/launches/{launch_id}/cancel", json={}, headers=AUTH)
        assert cancelled.status_code == 200 and cancelled.json()["cancelled_task_count"] == 2
        assert cancelled.json()["launch"]["status"] == "cancelled"
        assert all(row["id"] != str(foreign.id) for row in client.get(PREFIX + "/launches").json()["items"])
        assert client.get(PREFIX + f"/launches/{foreign.id}").status_code == 404
        audit_response = client.get(PREFIX + "/audit").text
        assert CONFIG.api_token not in audit_response


def test_launch_migration_is_additive_and_matches_models():
    path = Path(__file__).resolve().parents[1] / "migrations/017_collection_launches.sql"
    assert path.read_text(encoding="utf-8") == additive_sql()
    assert "DROP TABLE" not in additive_sql() and "ALTER TABLE" not in additive_sql()
