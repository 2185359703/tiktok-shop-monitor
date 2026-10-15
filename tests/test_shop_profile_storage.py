from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.crawlers.shop_profile import PARSER_VERSION, ShopProfilePageSnapshot, parse_shop_profile
from app.models import CrawlTask, ResourceType, Shop, ShopProfileSnapshot, TaskStatus, TaskType
from app.queue.retry_policy import TaskFailure
from app.services.shop_profile import (
    enqueue_due_shop_profiles, ensure_shop_profile_task, persist_shop_profile, shop_profile_task_key,
    shop_profile_window,
)
from app.services.shop_profile_reporting import shop_profile_coverage
from test_shop_profile import SHOP_ID, URL, html, profile_info, router


NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


def page(info=None, *, captured_at=NOW):
    return ShopProfilePageSnapshot(URL, URL, 200, 1000, "a" * 64, 10, 1,
        captured_at - timedelta(seconds=1), captured_at,
        parse_shop_profile(html(router(info)), URL))


def create_shop(session, identifier=SHOP_ID, *, url=None, region="US"):
    shop = Shop(platform="tiktok_shop", region=region, platform_shop_id=identifier,
                canonical_name="Original name", canonical_url=url or f"https://shop.tiktok.com/us/store/shop/{identifier}",
                first_seen_at=NOW - timedelta(days=2), last_seen_at=NOW - timedelta(days=2))
    session.add(shop)
    session.flush()
    return shop


def task_for(session, shop, window=NOW):
    ensure_shop_profile_task(session, shop, window)
    return session.scalar(select(CrawlTask).where(CrawlTask.idempotency_key == shop_profile_task_key(shop, window)))


def saved_count(session):
    return session.scalar(select(func.count()).select_from(ShopProfileSnapshot))


def settings(*identifiers):
    return SimpleNamespace(platform="tiktok_shop", region="US", shop_profiles_enabled=True,
        enabled_task_types=(TaskType.SHOP_PROFILE,), shop_profile_shop_ids=tuple(identifiers),
        shop_profile_interval_seconds=86400, shop_profile_jitter_seconds=300)


def test_current_and_immutable_snapshot_preserve_all_profile_values(session):
    shop = create_shop(session)
    task, observation = task_for(session, shop), page()
    result = persist_shop_profile(session, task, observation, "public/profile.json")
    saved = session.scalar(select(ShopProfileSnapshot))
    assert result["profile_updated"] and result["product_count_updated"]
    assert result["snapshots_written"] == 1 and saved.task_id == task.id
    assert shop.canonical_name == saved.canonical_name == "CHOUYATOU"
    for key, value in observation.profile.fields.items():
        assert getattr(shop, key) == getattr(saved, key) == value
    assert shop.rating == Decimal("4.5") and shop.profile_observation_status == "complete"
    assert shop.profile_captured_at == shop.product_count_captured_at == saved.captured_at == NOW
    assert shop.profile_parser_version == saved.parser_version == PARSER_VERSION
    assert saved.field_state == observation.profile.field_state
    assert shop.profile_field_state["follower_count"] == saved.field_state["follower_count"]
    assert shop.profile_field_state["product_count"]["captured_at"] == NOW.isoformat()


def test_same_task_and_same_window_are_idempotent_but_next_window_is_new_history(session):
    shop = create_shop(session)
    task = task_for(session, shop)
    first = persist_shop_profile(session, task, page(), None)
    repeated = persist_shop_profile(session, task, page(captured_at=NOW + timedelta(seconds=5)), None)
    duplicate = CrawlTask(platform=shop.platform, region=shop.region, task_type=TaskType.SHOP_PROFILE,
        resource_type=ResourceType.SHOP, resource_id=str(shop.id), scheduled_for=NOW,
        payload=dict(task.payload))
    session.add(duplicate)
    session.flush()
    same_window = persist_shop_profile(session, duplicate, page(), None)
    assert first["snapshot_id"] == repeated["snapshot_id"] == same_window["snapshot_id"]
    assert repeated["window_already_saved"] and same_window["snapshots_written"] == 0
    next_task = task_for(session, shop, NOW + timedelta(days=1))
    persist_shop_profile(session, next_task, page(captured_at=NOW + timedelta(days=1)), None)
    assert saved_count(session) == 2


def test_new_partial_observation_clears_missing_fields_and_keeps_history(session):
    shop = create_shop(session)
    persist_shop_profile(session, task_for(session, shop), page(), None)
    info = profile_info()
    for key in ("followers_count", "format_followers_count", "shop_rating", "shop_logo", "store_brand_show", "shop_identity_label"):
        info.pop(key)
    info.update(sold_count=0, format_sold_count="0", video_count=None, format_video_count="1.3K+")
    later = NOW + timedelta(days=1)
    result = persist_shop_profile(session, task_for(session, shop, later), page(info, captured_at=later), None)
    old = session.scalar(select(ShopProfileSnapshot).order_by(ShopProfileSnapshot.id))
    assert result["observation_status"] == "partial"
    assert shop.follower_count is None and shop.follower_count_text is None and shop.rating is None
    assert shop.logo_url is None and shop.verification_type is None and shop.identity_label_key is None
    assert shop.sales_count == 0 and shop.sales_count_text == "0"
    assert shop.video_count is None and shop.video_count_text == "1.3K+"
    assert shop.profile_field_state["video_count"]["state"] == "display_only"
    assert old.follower_count == 34872 and old.sales_count == 498223


def test_late_history_cannot_revert_current_profile_identity_or_independent_count(session):
    shop = create_shop(session)
    later = NOW + timedelta(days=1)
    newest = profile_info()
    newest.update(shop_name="New merchant name", followers_count=40000)
    persist_shop_profile(session, task_for(session, shop, later), page(newest, captured_at=later), None)
    shop.product_count, shop.product_count_text = 300, "300"
    shop.product_count_captured_at = later + timedelta(hours=1)
    shop.canonical_name, shop.last_seen_at = "Most recent identity", later + timedelta(hours=1)
    result = persist_shop_profile(session, task_for(session, shop), page(), None)
    assert result["stale_observation_skipped"] and not result["product_count_updated"]
    assert saved_count(session) == 2
    assert shop.follower_count == 40000 and shop.profile_captured_at == later
    assert shop.product_count == 300 and shop.product_count_text == "300"
    assert shop.canonical_name == "Most recent identity"


def test_new_profile_respects_newer_product_discovery_and_identity(session):
    shop = create_shop(session)
    shop.product_count, shop.product_count_text = 400, "400"
    shop.product_count_captured_at = NOW + timedelta(hours=1)
    shop.canonical_name, shop.last_seen_at = "More recent discovery", NOW + timedelta(hours=1)
    result = persist_shop_profile(session, task_for(session, shop), page(), None)
    assert result["profile_updated"] and not result["product_count_updated"]
    assert shop.follower_count == 34872 and shop.product_count == 400
    assert shop.canonical_name == "More recent discovery" and shop.profile_captured_at == NOW
    assert session.scalar(select(ShopProfileSnapshot)).product_count == 205
    assert shop.profile_field_state["product_count"]["captured_at"] == (NOW + timedelta(hours=1)).isoformat()
    assert shop.profile_field_state["product_count"]["source"] == "shops.product_count"


def test_new_profile_keeps_newer_product_count_state_in_current_only(session):
    shop = create_shop(session)
    persist_shop_profile(session, task_for(session, shop), page(), None)
    later = NOW + timedelta(hours=2)
    shop.product_count, shop.product_count_text, shop.product_count_captured_at = 0, "0", later
    count_state = {"state": "exact", "source": "newer_discovery.on_sell_product_count",
                   "scope": "platform_on_sell_product_count", "captured_at": later.isoformat()}
    shop.profile_field_state = {**shop.profile_field_state, "product_count": count_state}
    captured = NOW + timedelta(hours=1)
    info = profile_info()
    info.pop("on_sell_product_count")
    info.pop("display_on_sell_product_count")
    persist_shop_profile(session, task_for(session, shop, captured), page(info, captured_at=captured), None)
    assert shop.product_count == 0 and shop.profile_field_state["product_count"] == count_state
    saved = session.scalar(select(ShopProfileSnapshot).order_by(ShopProfileSnapshot.id.desc()))
    assert saved.product_count is None and saved.field_state["product_count"]["state"] == "not_provided"


@pytest.mark.parametrize("invalid", ["profile_id", "source_url", "payload_id", "market", "task_kind", "boolean_count", "hash", "time"])
def test_scope_or_unverified_evidence_cannot_write_a_snapshot(session, invalid):
    shop = create_shop(session)
    task, observation = task_for(session, shop), page()
    if invalid == "profile_id":
        observation = replace(observation, profile=replace(observation.profile, platform_shop_id="999"))
    elif invalid == "source_url":
        observation = replace(observation, final_url="https://shop.tiktok.com/us/store/wrong/999")
    elif invalid == "payload_id":
        task.payload = {**task.payload, "platform_shop_id": "999"}
    elif invalid == "market":
        observation = replace(observation, profile=replace(observation.profile, region="GB"))
    elif invalid == "task_kind":
        task.task_type = TaskType.PRODUCT_DISCOVERY
    elif invalid == "boolean_count":
        observation = replace(observation, profile=replace(observation.profile,
            fields={**observation.profile.fields, "follower_count": True}))
    elif invalid == "hash":
        observation = replace(observation, response_hash="unverified")
    else:
        observation = replace(observation, captured_at=NOW.replace(tzinfo=None))
    with pytest.raises(TaskFailure):
        persist_shop_profile(session, task, observation, None)
    assert saved_count(session) == 0 and shop.profile_captured_at is None


def test_snapshot_and_current_values_roll_back_together(session):
    shop = create_shop(session)
    task = task_for(session, shop)
    with pytest.raises(RuntimeError), session.begin_nested():
        persist_shop_profile(session, task, page(), None)
        raise RuntimeError("parent transaction failed")
    assert saved_count(session) == 0
    session.refresh(shop)
    assert shop.profile_captured_at is None and shop.follower_count is None


def test_snapshot_database_constraints_restrict_market_mismatch_and_deletion(session):
    shop = create_shop(session)
    persist_shop_profile(session, task_for(session, shop), page(), None)
    saved = session.scalar(select(ShopProfileSnapshot))
    with pytest.raises(IntegrityError), session.begin_nested():
        saved.region = "GB"
        session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(delete(Shop).where(Shop.id == shop.id))
    with pytest.raises(IntegrityError), session.begin_nested():
        session.execute(delete(CrawlTask).where(CrawlTask.id == saved.task_id))
    assert saved_count(session) == 1


def test_initial_task_is_idempotent_and_keeps_utc_window_and_safe_url(session):
    shop = create_shop(session, url=URL + "?tracking=discard")
    local = NOW.astimezone(timezone(timedelta(hours=8)))
    assert ensure_shop_profile_task(session, shop, local)
    assert not ensure_shop_profile_task(session, shop, NOW)
    task = session.scalar(select(CrawlTask))
    assert task.scheduled_for == NOW and task.payload["shop_url"] == URL
    assert task.payload["scheduled_for"] == NOW.isoformat()
    assert task.resource_id == str(shop.id) and task.resource_type == ResourceType.SHOP


@pytest.mark.parametrize("url,region", [("https://example.org/store/999", "US"),
    ("https://shop.tiktok.com/us/store/wrong/999", "US"), (URL, "GB")])
def test_initial_task_rejects_unverified_shop_entry(session, url, region):
    shop = create_shop(session, url=url, region=region)
    with pytest.raises(ValueError):
        ensure_shop_profile_task(session, shop, NOW)
    assert session.scalar(select(func.count()).select_from(CrawlTask)) == 0


def test_periodic_allowlist_limit_and_jitter_make_progress_without_backfill(session, monkeypatch):
    for identifier in ("1", "2", "3", "4"):
        create_shop(session, identifier)
    config = settings("1", "2", "3")
    monkeypatch.setattr("app.services.shop_profile.database_now", lambda _: NOW)
    assert [enqueue_due_shop_profiles(session, config, limit=1) for _ in range(4)] == [1, 1, 1, 0]
    tasks = session.scalars(select(CrawlTask).order_by(CrawlTask.id)).all()
    window = shop_profile_window(NOW, config.shop_profile_interval_seconds)
    assert {task.payload["platform_shop_id"] for task in tasks} == {"1", "2", "3"}
    assert all(task.scheduled_for == window and window <= task.available_at <= window + timedelta(seconds=300) for task in tasks)
    # A long gap creates only today's window; it does not invent missed observations.
    next_time = NOW + timedelta(days=30)
    monkeypatch.setattr("app.services.shop_profile.database_now", lambda _: next_time)
    assert enqueue_due_shop_profiles(session, config, limit=100) == 3
    assert session.scalar(select(func.count()).select_from(CrawlTask)) == 6


def test_invalid_early_shop_does_not_starve_later_verified_entries(session, monkeypatch):
    create_shop(session, "1", url="https://example.org/not-a-store")
    create_shop(session, "2")
    monkeypatch.setattr("app.services.shop_profile.database_now", lambda _: NOW)
    assert enqueue_due_shop_profiles(session, settings("1", "2"), limit=1) == 1
    assert session.scalar(select(CrawlTask)).payload["platform_shop_id"] == "2"


@pytest.mark.parametrize("disabled", ["switch", "types", "allowlist"])
def test_periodic_collection_requires_explicit_enablement_and_allowlist(session, disabled):
    create_shop(session)
    config = settings(SHOP_ID)
    if disabled == "switch":
        config.shop_profiles_enabled = False
    elif disabled == "types":
        config.enabled_task_types = ()
    else:
        config.shop_profile_shop_ids = ()
    assert enqueue_due_shop_profiles(session, config) == 0
    assert session.scalar(select(func.count()).select_from(CrawlTask)) == 0


def test_window_uses_utc_and_rejects_naive_or_invalid_interval():
    local = NOW.astimezone(timezone(timedelta(hours=8)))
    assert shop_profile_window(local, 86400) == NOW.replace(hour=0)
    with pytest.raises(ValueError):
        shop_profile_window(NOW.replace(tzinfo=None), 86400)
    with pytest.raises(ValueError):
        shop_profile_window(NOW, 0)


def test_profile_coverage_separates_observations_tasks_staleness_and_precision(session, monkeypatch):
    shops = [create_shop(session, str(number)) for number in range(1, 6)]
    complete = parse_shop_profile(html(router({**profile_info(), "store_brand_show": None,
                                              "shop_identity_label": None})), URL)
    partial = parse_shop_profile(html(router({**profile_info(), "shop_logo": None,
        "followers_count": None, "format_followers_count": "34.8K+", "video_count": 0})), URL)
    shops[0].profile_captured_at = NOW
    shops[0].profile_observation_status, shops[0].profile_field_state = "complete", complete.field_state
    shops[1].profile_captured_at = NOW - timedelta(days=2)
    shops[1].profile_observation_status, shops[1].profile_field_state = "partial", partial.field_state
    for shop, status in zip(shops[:4], (TaskStatus.SUCCEEDED, TaskStatus.RETRY_WAITING, TaskStatus.DEAD, TaskStatus.PENDING)):
        task_for(session, shop).status = status
    # Same internal resource text in another market cannot affect this report.
    session.add(CrawlTask(platform="tiktok_shop", region="GB", task_type=TaskType.SHOP_PROFILE,
        resource_type=ResourceType.SHOP, resource_id=str(shops[0].id), status=TaskStatus.DEAD))
    session.flush()
    monkeypatch.setattr("app.services.shop_profile_reporting.database_now", lambda _: NOW)
    result = shop_profile_coverage(session, shops)
    assert result["target_shops"] == 5 and result["profile_observed_shops"] == 2
    assert result["profile_complete_shops"] == result["profile_partial_shops"] == 1
    assert result["profile_failed_shops"] == result["profile_stale_shops"] == 1
    assert result["profile_pending_shops"] == 3 and not result["full_coverage"]
    followers = result["field_coverage"]["follower_count"]
    assert followers == {"observed": 2, "exact": 1, "display_only": 1, "checked": 2,
                         "coverage_ratio": 2 / 5, "exact_ratio": 1 / 5}
    badge = result["field_coverage"]["verification_type"]
    assert badge["checked"] == 2 and badge["observed"] == 1 and badge["exact"] == 0
    assert result["field_coverage"]["logo_url"]["checked"] == 1
    assert result["field_coverage"]["video_count"]["exact"] == 2


def test_observed_profile_can_independently_have_a_newer_failed_refresh(session, monkeypatch):
    shop = create_shop(session)
    first = task_for(session, shop)
    persist_shop_profile(session, first, page(), None)
    first.status = TaskStatus.SUCCEEDED
    latest = task_for(session, shop, NOW + timedelta(hours=1))
    latest.status = TaskStatus.DEAD
    session.flush()
    monkeypatch.setattr("app.services.shop_profile_reporting.database_now", lambda _: NOW + timedelta(hours=2))
    result = shop_profile_coverage(session, [shop])
    assert result["profile_observed_shops"] == result["profile_complete_shops"] == result["profile_failed_shops"] == 1
    assert result["profile_pending_shops"] == result["profile_stale_shops"] == 0
    assert not result["full_coverage"]


def test_empty_profile_scope_never_claims_full_coverage(session, monkeypatch):
    monkeypatch.setattr("app.services.shop_profile_reporting.database_now", lambda _: NOW)
    result = shop_profile_coverage(session, [])
    assert result["target_shops"] == 0 and not result["full_coverage"]
    assert all(value["coverage_ratio"] is None and value["exact_ratio"] is None
               for value in result["field_coverage"].values())


def test_late_created_old_window_does_not_replace_latest_refresh_status(session, monkeypatch):
    shop = create_shop(session)
    current = task_for(session, shop)
    persist_shop_profile(session, current, page(), None)
    current.status = TaskStatus.SUCCEEDED
    task_for(session, shop, NOW - timedelta(days=1)).status = TaskStatus.DEAD
    session.flush()
    monkeypatch.setattr("app.services.shop_profile_reporting.database_now", lambda _: NOW + timedelta(hours=1))
    result = shop_profile_coverage(session, [shop])
    assert result["profile_failed_shops"] == 0 and result["full_coverage"]


def test_profile_staleness_starts_at_the_refresh_interval_boundary(session, monkeypatch):
    shop = create_shop(session)
    shop.profile_captured_at = NOW - timedelta(days=1)
    shop.profile_observation_status = "complete"
    task_for(session, shop).status = TaskStatus.SUCCEEDED
    session.flush()
    monkeypatch.setattr("app.services.shop_profile_reporting.database_now", lambda _: NOW)
    result = shop_profile_coverage(session, [shop], interval_seconds=86400)
    assert result["profile_stale_shops"] == 1 and not result["full_coverage"]


def test_collection_progress_exposes_profiles_without_changing_product_completion(session):
    from app.services.collection_service import collection_progress
    from test_collection import scope_fixture
    batch, _ = scope_fixture(session)
    for task in session.scalars(select(CrawlTask)):
        task.status = TaskStatus.SUCCEEDED
    session.flush()
    before = collection_progress(session, batch)
    assert before["full_coverage"] and before["source_complete_shops"] == 1
    assert before["shop_profiles"]["target_shops"] == 1
    assert before["shop_profiles"]["profile_pending_shops"] == 1
    assert not before["shop_profiles"]["full_coverage"]
    shop = session.scalar(select(Shop))
    task_for(session, shop).status = TaskStatus.DEAD
    session.flush()
    after = collection_progress(session, batch)
    assert after["full_coverage"] and after["failed_tasks"] == 0
    assert after["shop_profiles"]["profile_failed_shops"] == 1
