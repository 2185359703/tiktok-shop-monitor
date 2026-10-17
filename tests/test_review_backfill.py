import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models import CrawlTask, Product, Shop, TaskStatus, TaskType
from app.services.product_reviews import ensure_initial_review_task, ensure_review_task
from app.settings import Settings
from scripts.enqueue_review_backfill import candidates, enqueue_batch


CONFIG = Settings(enabled_task_types=(TaskType.PRODUCT_REVIEWS,))


def product(session, shop=None, **values):
    if shop is None:
        shop = Shop(region="US", platform_shop_id="100", canonical_name="Test shop")
        session.add(shop)
        session.flush()
    number = 1000 + (session.scalar(select(func.count()).select_from(Product)) or 0)
    row = Product(region="US", shop_id=shop.id, platform_product_id=str(number),
                  product_url=f"https://shop.tiktok.com/us/pdp/{number}",
                  last_success_at=datetime.now(timezone.utc), review_count=20)
    for key, value in values.items():
        setattr(row, key, value)
    session.add(row)
    session.flush()
    return row


def test_initial_reviews_are_idempotent_across_windows_and_failed_jobs(session):
    row = product(session)
    task, created = ensure_initial_review_task(session, CONFIG, row.id)
    assert created and task.payload["max_pages"] == 1 and task.payload["page_number"] == 1
    task.status = TaskStatus.DEAD
    task.scheduled_for = datetime(2025, 1, 1, tzinfo=timezone.utc)
    session.flush()
    existing, created = ensure_initial_review_task(session, CONFIG, row.id)
    assert existing.id == task.id and not created
    assert session.scalar(select(func.count()).select_from(CrawlTask)) == 1


@pytest.mark.parametrize("values", [{"review_count": 0}, {"last_success_at": None}])
def test_only_observed_products_with_source_reviews_are_scheduled(session, values):
    row = product(session, **values)
    assert ensure_initial_review_task(session, CONFIG, row.id) == (None, False)
    assert session.scalar(select(func.count()).select_from(CrawlTask)) == 0


def test_disabled_handler_does_not_create_work(session):
    row = product(session)
    assert ensure_initial_review_task(session, Settings(), row.id) == (None, False)


def test_unknown_source_count_does_not_mean_no_comments(session):
    row = product(session, review_count=None)
    assert candidates(session, CONFIG, ["100"]) == [row.id]
    task, created = ensure_initial_review_task(session, CONFIG, row.id)
    assert created and task.payload["max_pages"] == 1


def test_existing_manual_work_is_not_duplicated_and_rollback_is_atomic(session):
    row = product(session)
    with session.begin_nested() as savepoint:
        first, _ = ensure_initial_review_task(session, CONFIG, row.id)
        assert first is not None
        savepoint.rollback()
    assert session.scalar(select(func.count()).select_from(CrawlTask)) == 0
    manual, _ = ensure_review_task(session, CONFIG, row.id, max_pages=20)
    automatic, created = ensure_initial_review_task(session, CONFIG, row.id)
    assert automatic.id == manual.id and not created and automatic.payload["max_pages"] == 20


def test_backfill_is_scoped_spreads_shops_and_replay_creates_no_work(session):
    first = product(session)
    shop = session.get(Shop, first.shop_id)
    second = product(session, shop)
    other = Shop(region="US", platform_shop_id="200", canonical_name="Other test shop")
    session.add(other)
    session.flush()
    third = product(session, other)
    product(session, shop, review_count=0)
    scheduled = product(session, shop)
    ensure_review_task(session, CONFIG, scheduled.id)
    assert candidates(session, CONFIG, ["100", "200"]) == [first.id, third.id, second.id]
    assert candidates(session, CONFIG, ["100"]) == [first.id, second.id]
    assert enqueue_batch(session, CONFIG, [first.id, third.id, second.id]) == 3
    assert enqueue_batch(session, CONFIG, [first.id, third.id, second.id]) == 0
    assert candidates(session, CONFIG, ["100", "200"]) == []
    with pytest.raises(ValueError, match="verified_shop_scope_incomplete"):
        candidates(session, CONFIG, ["300"])


def test_batch_skips_unobserved_resources_and_preserves_transaction_boundary(session):
    first = product(session)
    unknown = product(session, session.get(Shop, first.shop_id), review_count=None)
    with session.begin_nested() as savepoint:
        assert enqueue_batch(session, CONFIG, [first.id, unknown.id, first.id]) == 2
        task = session.scalar(select(CrawlTask).where(CrawlTask.resource_id == str(first.id)))
        assert task.payload["root_task_id"] == task.task_id
        assert task.payload["max_pages"] == 1 and task.resource_id == str(first.id)
        savepoint.rollback()
    assert session.scalar(select(func.count()).select_from(CrawlTask)) == 0
    assert enqueue_batch(session, Settings(), [first.id]) == 0
    with pytest.raises(ValueError, match="product_not_found"):
        enqueue_batch(session, CONFIG, [first.id, 99999999])


@pytest.mark.parametrize("asynchronous", [False, True])
def test_detail_application_schedules_reviews_in_same_transaction(monkeypatch, tmp_path, asynchronous):
    from app.workers import async_handlers, handlers
    module = async_handlers if asynchronous else handlers
    captured = []
    snapshot = SimpleNamespace(final_url="https://shop.tiktok.com/us/pdp/1000", status=200,
        response_bytes=50, response_hash="a" * 64, latency_ms=3, save_evidence=lambda _: tmp_path / "detail.json")

    class Adapter:
        def __init__(self, *args):
            pass

        def fetch_product_detail(self, *args):
            return snapshot

    if asynchronous:
        async def fetch(*args):
            return snapshot
        Adapter.fetch_product_detail = fetch
    monkeypatch.setattr(module, "TikTokShopAdapter", Adapter)
    monkeypatch.setattr(module.ProductDetailHandler, "commit_observation", lambda *args: {})
    monkeypatch.setattr(module, "ensure_initial_review_task", lambda db, settings, pid: (captured.append((db, pid)), False))
    settings = replace(CONFIG, evidence_directory=tmp_path)
    context = SimpleNamespace(settings=settings, gate=None, stop=None)
    lease = SimpleNamespace(resource_id="17", task_id="test-detail", payload={"product_id": "17",
        "product_url": snapshot.final_url, "platform_product_id": "1000", "platform_shop_id": "100"})
    handler = module.ProductDetailHandler()
    result = asyncio.run(handler.prepare(lease, context)) if asynchronous else handler.prepare(lease, context)
    assert captured == []
    transaction = object()
    result.apply(transaction, lease)
    assert captured == [(transaction, 17)]
