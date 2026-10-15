from datetime import datetime, timezone
from threading import Event

from sqlalchemy import select

from app.crawlers.public_shop import PublicPageSnapshot, ShopCandidate
from app.models import CrawlAttempt, CrawlTask, ResolutionStatus, SeedShopObservation, Shop, TaskStatus, TaskType
from app.services.seed_importer import import_seed_batch, normalize_seed, SeedBatch
from app.services.task_runtime import claim_task, finalize_task
from app.settings import Settings
from app.workers.handlers import HandlerContext, ShopDiscoveryHandler


def test_existing_identity_service_is_used_without_duplicate_attempts(session, monkeypatch, tmp_path):
    url = "https://shop.tiktok.com/us/store/test-clothes/90000123456789"
    row = normalize_seed({"region": "US", "category": "women_clothing", "seed_type": "shop_name",
                          "seed_value": "Test Clothes", "shop_url": url, "source_url": url,
                          "verification_status": "verified_public_shop_page", "observed_at": "2026-10-13"}, 2)
    import_seed_batch(session, SeedBatch((row,), 1))
    task = session.scalar(select(CrawlTask).where(CrawlTask.task_type == TaskType.SHOP_DISCOVERY))
    config = Settings(enabled_task_types=(TaskType.SHOP_DISCOVERY,), evidence_directory=tmp_path)
    lease = claim_task(session, task.task_id, task.task_type, "test-worker", config)
    now = datetime.now(timezone.utc)
    snapshot = PublicPageSnapshot(url, url, 200, 100, "a" * 64, 10, 1, now, now,
                                  (ShopCandidate("90000123456789", "Test Clothes", ("Test Clothes",), url, "US", ("fixture",)),))
    monkeypatch.setattr("app.workers.handlers.TikTokShopAdapter.discover_shops", lambda self, source, stop: snapshot)
    result = ShopDiscoveryHandler().prepare(lease, HandlerContext(config, None, Event()))
    assert finalize_task(session, lease, config, result=result) == TaskStatus.SUCCEEDED
    assert session.scalar(select(SeedShopObservation)).resolution_status == ResolutionStatus.RESOLVED
    assert session.scalar(select(Shop)).canonical_url == url
    assert len(session.scalars(select(CrawlAttempt)).all()) == 1
    assert session.get(CrawlTask, lease.id).attempt_count == 1
    assert session.scalar(select(CrawlTask).where(CrawlTask.task_type == TaskType.PRODUCT_DISCOVERY)).status == TaskStatus.PENDING
