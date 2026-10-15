from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.crawlers.public_shop import PublicPageError, PublicPageSnapshot, ShopCandidate
from app.models import CrawlTask, ResolutionStatus, SeedShopObservation, Shop, TaskStatus, TaskType
from app.services.seed_importer import SeedBatch, import_seed_batch, normalize_seed
from app.services.seed_link_verification import sync_verified_store_link, verify_store_link


SHOP_URL = "https://www.tiktok.com/shop/store/chouyatou/7495552971774921097"
SOURCE = "https://shop.tiktok.com/us/k/clothing-for-women"
NOW = datetime(2026, 10, 13, 11, tzinfo=timezone.utc)


def raw_seed():
    return {"region": "US", "category": "women_clothing", "seed_type": "shop_name",
            "seed_value": "CHOUYATOU", "shop_url": "", "source_url": SOURCE,
            "verification_status": "observed_public_category_page", "observed_at": "2026-09-25"}


def snapshot():
    shop = ShopCandidate("7495552971774921097", "CHOUYATOU", ("CHOUYATOU",), SHOP_URL, "US", ("path",))
    return PublicPageSnapshot(SOURCE, SOURCE, 200, 100, "a" * 64, 12, 1, NOW, NOW, (shop,))


def storefront():
    return replace(snapshot(), source_url=SHOP_URL, final_url=SHOP_URL)


def test_url_page_identity_and_name_must_agree():
    row = normalize_seed(raw_seed(), 2)
    checked = verify_store_link(row, storefront())
    assert checked.shop_url == SHOP_URL and checked.matched_by == "exact_name"
    with pytest.raises(PublicPageError) as error:
        verify_store_link(row, storefront(), expected_id="7495552971774921098")
    assert error.value.code == "identity_conflict"
    with pytest.raises(PublicPageError) as error:
        verify_store_link(row, snapshot())
    assert error.value.code == "not_a_storefront"


def test_similar_but_different_shop_name_is_not_confirmed():
    row = normalize_seed(raw_seed(), 2)
    other = replace(storefront(), candidates=(ShopCandidate(
        "7495552971774921097", "CHOUYATOU Studio", ("CHOUYATOU Studio",), SHOP_URL, "US", ("path",),
    ),))
    with pytest.raises(PublicPageError) as error:
        verify_store_link(row, other)
    assert error.value.code == "name_mismatch"
    assert verify_store_link(row, other, expected_id="7495552971774921097").matched_by == "stable_id"


def test_direct_verified_link_resolves_original_seed_without_changing_its_hash(session):
    row = normalize_seed(raw_seed(), 2)
    import_seed_batch(session, SeedBatch((row,), 1))
    assert sync_verified_store_link(session, row, storefront(), "public-store.json") == "resolved"
    seed = session.scalar(select(SeedShopObservation))
    assert seed.row_hash == row.row_hash and seed.shop_url is None
    assert seed.raw_record == raw_seed() and seed.resolution_status == ResolutionStatus.RESOLVED
    assert session.scalar(select(Shop)).canonical_url == SHOP_URL
    task = session.scalar(select(CrawlTask).where(CrawlTask.task_type == TaskType.PRODUCT_DISCOVERY))
    assert task.payload["shop_url"] == SHOP_URL
    assert sync_verified_store_link(session, row, storefront(), "public-store.json") == "url_enriched"


def test_link_sync_does_not_replace_running_task_payload(session):
    row = normalize_seed(raw_seed(), 2)
    import_seed_batch(session, SeedBatch((row,), 1))
    sync_verified_store_link(session, row, storefront(), "source.json")
    task = session.scalar(select(CrawlTask).where(CrawlTask.task_type == TaskType.PRODUCT_DISCOVERY))
    task.status = TaskStatus.RUNNING
    task.payload = {"keep": "original worker payload"}
    session.flush()
    sync_verified_store_link(session, row, storefront(), "source.json")
    assert task.payload == {"keep": "original worker payload"}
