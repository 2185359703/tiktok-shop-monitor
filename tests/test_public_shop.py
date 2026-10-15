import json
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import func, select

from app.crawlers.public_shop import (
    PublicPageError, PublicPageSnapshot, PublicShopClient, ShopCandidate,
    parse_shop_candidates, public_url,
)
from app.models import (
    CrawlAttempt, CrawlTask, ResolutionStatus, SeedShopObservation, Shop,
    ShopSeedLink, TaskStatus, TaskType,
)
from app.services.seed_importer import SeedBatch, import_seed_batch, normalize_seed, seed_task_key
from app.services.shop_discovery import resolve_seed_from_snapshot


SOURCE = "https://shop.tiktok.com/us/k/clothing-for-women"
SHOP_URL = "https://www.tiktok.com/shop/store/chouyatou/7495552971774921097"
NOW = datetime(2026, 10, 13, 11, 0, 0, tzinfo=timezone.utc)


def business_html(shops, *, region="us", mapping=False):
    components = [{"component_data": {"shops": shops}}]
    if mapping:
        components = {"4": components[0]}
    data = {"loaderData": {
        "layout": {"private_token": "not-persisted"},
        "(region)/(route_page_name)/(route_keyword)/page": {
            "route_info": {"path_region": region},
            "page_config": {"components_map": components},
        },
    }}
    return '<html><script id="__MODERN_ROUTER_DATA__" type="application/json">' + json.dumps(data) + '</script></html>'


def shop_record(identifier="7495552971774921097", name="CHOUYATOU", **overrides):
    return {"seller_id": identifier, "shop_name": name, "region": "US", **overrides}


def snapshot(candidates=None):
    candidates = candidates or parse_shop_candidates(business_html([
        shop_record(shop_link=SHOP_URL + "?source=tracking&token=sensitive"),
    ]), SOURCE)
    return PublicPageSnapshot(SOURCE, SOURCE, 200, 1024, "a" * 64, 12, 1, NOW, NOW, candidates)


def imported_seed(session, *, name="CHOUYATOU", category="women_clothing"):
    raw = {"region": "US", "category": category, "seed_type": "shop_name", "seed_value": name,
           "shop_url": "", "source_url": SOURCE, "verification_status": "observed_public_category_page",
           "observed_at": "2026-09-25"}
    row = normalize_seed(raw, 2)
    import_seed_batch(session, SeedBatch((row,), 1))
    return session.scalar(select(SeedShopObservation).where(SeedShopObservation.row_hash == row.row_hash))


@pytest.mark.parametrize("mapping", [False, True])
def test_real_component_shapes_deduplicate_seller_references(mapping):
    candidates = parse_shop_candidates(business_html([
        shop_record(shop_link=SHOP_URL + "?token=sensitive"),
        {"product_id": "1729443883935109513", "seller_info": shop_record()},
    ], mapping=mapping), SOURCE)
    assert len(candidates) == 1
    assert candidates[0].platform_shop_id == "7495552971774921097"
    assert candidates[0].canonical_url == SHOP_URL
    assert len(candidates[0].evidence_paths) == 2


def test_large_integer_ids_are_exact_and_float_or_foreign_market_ids_are_rejected():
    candidates = parse_shop_candidates(business_html([
        shop_record(identifier=7495552971774921097),
        shop_record(identifier=7495552971774921097.0),
        shop_record(identifier="7", region="GB"),
    ]), SOURCE)
    assert [item.platform_shop_id for item in candidates] == ["7495552971774921097"]


@pytest.mark.parametrize("url", [
    "http://shop.tiktok.com/us/k/clothing", "https://example.test/us/k/clothing",
    "https://user:secret@shop.tiktok.com/us/k/clothing", "https://shop.tiktok.com/uk/k/clothing",
    "https://shop.tiktok.com:8443/us/k/clothing", "https://shop.tiktok.com/api/private",
])
def test_public_client_refuses_unapproved_routes_and_secrets(url):
    with pytest.raises(PublicPageError) as error:
        public_url(url)
    assert error.value.code == "unsupported_source" and "secret" not in str(error.value)


def test_missing_or_wrong_market_ssr_is_not_an_empty_valid_shop_directory():
    with pytest.raises(PublicPageError, match="SSR"):
        parse_shop_candidates("<html>verify you're human</html>", SOURCE)
    with pytest.raises(PublicPageError) as error:
        parse_shop_candidates(business_html([], region="gb"), SOURCE)
    assert error.value.code == "market_mismatch"


def test_200_security_check_is_an_access_limit_not_missing_data():
    transport = httpx.MockTransport(lambda request: httpx.Response(
        200, text='<html><title>Security Check</title><script id="captcha-config">{}</script></html>',
    ))
    with PublicShopClient(interval=0, transport=transport) as client:
        with pytest.raises(PublicPageError) as error:
            client.fetch(SOURCE)
    assert error.value.code == "access_limited" and error.value.status == 200


def test_fetch_with_mock_response_saves_only_safe_business_evidence(tmp_path):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=business_html([
        shop_record(shop_link=SHOP_URL + "?token=must-not-persist"),
    ])))
    with PublicShopClient(interval=0, transport=transport) as client:
        result = client.fetch(SOURCE)
    evidence = result.save_evidence(tmp_path)
    saved = json.loads(evidence.read_text(encoding="utf-8"))
    assert saved["response_bytes"] > 0 and saved["candidates"][0]["canonical_url"] == SHOP_URL
    assert "token" not in evidence.read_text(encoding="utf-8")


@pytest.mark.parametrize("status", [401, 403, 429])
def test_access_limited_responses_do_not_retry(status):
    calls = []
    def respond(request):
        calls.append(request.url)
        return httpx.Response(status)
    with PublicShopClient(interval=0, transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(PublicPageError) as error:
            client.fetch(SOURCE)
    assert error.value.code == "access_limited" and len(calls) == 1


def test_transient_503_retries_but_external_redirect_is_never_followed(monkeypatch):
    monkeypatch.setattr("app.crawlers.public_shop.time.sleep", lambda _: None)
    calls = []
    def respond(request):
        calls.append(request.url)
        return httpx.Response(503 if len(calls) == 1 else 200, text=business_html([shop_record()]))
    with PublicShopClient(interval=0, transport=httpx.MockTransport(respond)) as client:
        result = client.fetch(SOURCE)
    assert result.network_attempts == len(calls) == 2
    calls.clear()
    def redirect(request):
        calls.append(request.url)
        return httpx.Response(302, headers={"location": "https://example.test/private"})
    with PublicShopClient(interval=0, transport=httpx.MockTransport(redirect)) as client:
        with pytest.raises(PublicPageError):
            client.fetch(SOURCE)
    assert len(calls) == 1


def test_unique_source_identity_writes_links_attempt_and_next_task_atomically(session):
    seed = imported_seed(session)
    outcome, created = resolve_seed_from_snapshot(session, seed.id, snapshot(), "data/samples/evidence.json")
    assert outcome == "resolved" and created
    assert seed.resolution_status == ResolutionStatus.RESOLVED
    shop = session.scalar(select(Shop))
    assert shop.platform_shop_id == "7495552971774921097" and shop.canonical_url == SHOP_URL
    assert session.scalar(select(ShopSeedLink)).shop_id == shop.id
    task = session.scalar(select(CrawlTask).where(CrawlTask.task_type == TaskType.SHOP_DISCOVERY))
    attempt = session.scalar(select(CrawlAttempt))
    assert task.status == TaskStatus.SUCCEEDED and task.attempt_count == 1 and attempt.succeeded
    assert attempt.response_hash == "a" * 64
    assert resolve_seed_from_snapshot(session, seed.id, snapshot(), "same.json") == ("already_processed", False)
    assert session.scalar(select(func.count()).select_from(Shop)) == 1
    assert session.scalar(select(func.count()).select_from(CrawlAttempt)) == 1


def test_same_shop_across_observations_has_one_identity_and_next_task(session):
    first = imported_seed(session)
    second = imported_seed(session, category="sportswear")
    assert resolve_seed_from_snapshot(session, first.id, snapshot(), "first.json") == ("resolved", True)
    assert resolve_seed_from_snapshot(session, second.id, snapshot(), "second.json") == ("resolved", False)
    assert session.scalar(select(func.count()).select_from(Shop)) == 1
    assert session.scalar(select(func.count()).select_from(ShopSeedLink)) == 2
    assert session.scalar(select(func.count()).select_from(CrawlTask).where(
        CrawlTask.task_type == TaskType.PRODUCT_DISCOVERY
    )) == 1


def test_absent_name_stays_pending_without_fake_identity_or_nonexistent_verdict(session):
    seed = imported_seed(session, name="Not included in current recommendations")
    assert resolve_seed_from_snapshot(session, seed.id, snapshot(), "source.json") == ("not_in_source", False)
    assert seed.resolution_status == ResolutionStatus.PENDING
    task = session.scalar(select(CrawlTask))
    assert task.status == TaskStatus.PENDING and task.attempt_count == 0
    assert task.checkpoint["outcome"] == "not_in_source_snapshot"
    assert session.scalar(select(func.count()).select_from(Shop)) == 0


def test_same_name_different_shop_ids_requires_manual_review(session):
    seed = imported_seed(session)
    candidates = parse_shop_candidates(business_html([
        shop_record(), shop_record(identifier="7495552971774921098"),
    ]), SOURCE)
    assert resolve_seed_from_snapshot(session, seed.id, snapshot(candidates), "source.json") == ("ambiguous", False)
    assert seed.resolution_status == ResolutionStatus.AMBIGUOUS
    assert session.scalar(select(CrawlTask)).status == TaskStatus.DEAD
    assert session.scalar(select(CrawlAttempt)).succeeded is False
    assert session.scalar(select(func.count()).select_from(ShopSeedLink)) == 0


def test_running_task_is_not_overwritten_by_bootstrap(session):
    seed = imported_seed(session)
    task = session.scalar(select(CrawlTask))
    task.status = TaskStatus.RUNNING
    session.flush()
    assert resolve_seed_from_snapshot(session, seed.id, snapshot(), "source.json") == ("task_not_pending", False)
    assert seed.resolution_status == ResolutionStatus.PENDING and task.status == TaskStatus.RUNNING


def test_wrong_source_or_exhausted_task_is_not_completed(session):
    seed = imported_seed(session)
    wrong = replace(snapshot(), source_url="https://shop.tiktok.com/us/k/sportswear-shop")
    assert resolve_seed_from_snapshot(session, seed.id, wrong, "wrong.json") == ("source_mismatch", False)
    task = session.scalar(select(CrawlTask))
    task.attempt_count = task.max_attempts
    session.flush()
    assert resolve_seed_from_snapshot(session, seed.id, snapshot(), "source.json") == ("attempts_exhausted", False)
    assert seed.resolution_status == ResolutionStatus.PENDING
    assert session.scalar(select(func.count()).select_from(Shop)) == 0


def test_resolution_can_roll_back_shop_link_and_downstream_task(session):
    seed = imported_seed(session)
    with pytest.raises(RuntimeError):
        with session.begin_nested():
            resolve_seed_from_snapshot(session, seed.id, snapshot(), "source.json")
            raise RuntimeError("simulate failed transaction")
    session.refresh(seed)
    assert seed.resolution_status == ResolutionStatus.PENDING
    assert session.scalar(select(func.count()).select_from(Shop)) == 0
    assert session.scalar(select(func.count()).select_from(ShopSeedLink)) == 0
    assert session.scalar(select(func.count()).select_from(CrawlAttempt)) == 0


def test_older_observation_does_not_erase_newer_confirmed_url_or_name(session):
    first = imported_seed(session)
    second = imported_seed(session, category="sportswear")
    resolve_seed_from_snapshot(session, first.id, snapshot(), "newer.json")
    older = replace(snapshot(), captured_at=datetime(2026, 9, 1, tzinfo=timezone.utc), candidates=(
        ShopCandidate("7495552971774921097", "Old name", ("CHOUYATOU",), None, "US", ("source",)),
    ))
    resolve_seed_from_snapshot(session, second.id, older, "older.json")
    shop = session.scalar(select(Shop))
    assert shop.canonical_name == "CHOUYATOU" and shop.canonical_url == SHOP_URL
