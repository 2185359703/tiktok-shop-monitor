from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Event

import pytest
from sqlalchemy import func, select

from app.models import CrawlAttempt, CrawlTask, Product, ResourceType, Shop, StockStatus, TaskStatus, TaskType
from app.queue.retry_policy import TaskFailure
from app.services.product_discovery import persist_product_page
from app.services.shop_discovery import ensure_product_discovery_task
from app.services.task_runtime import claim_task, ExecutionResult, finalize_task
from app.settings import Settings
from app.workers.contracts import HandlerContext
from app.workers.handlers import ProductDiscoveryHandler
from test_product_list import product, snapshot, SHOP_ID, URL


CONFIG = Settings(enabled_task_types=(TaskType.PRODUCT_DISCOVERY,))


def initial_task(session):
    shop = Shop(region="US", platform_shop_id=SHOP_ID, canonical_name="ANRABESS", canonical_url=URL)
    session.add(shop)
    session.flush()
    ensure_product_discovery_task(session, shop, URL, datetime.now(timezone.utc))
    task = session.scalar(select(CrawlTask).where(CrawlTask.task_type == TaskType.PRODUCT_DISCOVERY))
    return shop, task


def test_page_commit_enqueues_next_page_and_detail_without_claiming_full_store(session, monkeypatch, tmp_path):
    shop, task = initial_task(session)
    lease = claim_task(session, task.task_id, task.task_type, "product-test", CONFIG)
    page = snapshot()
    monkeypatch.setattr("app.workers.handlers.TikTokShopAdapter.discover_products", lambda self, source, pagination, stop: page)
    result = ProductDiscoveryHandler().prepare(lease, HandlerContext(replace(CONFIG, evidence_directory=tmp_path), None, Event()))
    finalize_task(session, lease, CONFIG, result=result)
    saved = session.scalar(select(Product))
    assert saved.price_min == Decimal("27.99") and saved.price_max is None and saved.stock_status == StockStatus.UNKNOWN
    assert saved.last_success_at is None and saved.content_hash is None
    assert task.checkpoint["complete"] is False and task.checkpoint["next_task_created"] is True
    next_page = session.scalar(select(CrawlTask).where(CrawlTask.payload["page_number"].as_integer() == 2))
    detail = session.scalar(select(CrawlTask).where(CrawlTask.task_type == TaskType.PRODUCT_DETAIL))
    assert next_page.status == detail.status == TaskStatus.PENDING
    assert next_page.scheduled_for == task.scheduled_for and next_page.payload["root_task_id"] == task.task_id
    assert detail.resource_id == str(saved.id) and detail.payload["shop_id"] == str(shop.id)
    assert len(session.scalars(select(CrawlAttempt)).all()) == 1


def test_replay_preserves_detail_task_state_and_does_not_duplicate(session):
    shop, task = initial_task(session)
    page = snapshot()
    first = persist_product_page(session, task, page, page_number=1, root_task_id=task.task_id, max_pages=50)
    detail = session.scalar(select(CrawlTask).where(CrawlTask.task_type == TaskType.PRODUCT_DETAIL))
    detail.status, detail.checkpoint = TaskStatus.CANCELLED, {"keep": True}
    session.flush()
    repeated = persist_product_page(session, task, page, page_number=1, root_task_id=task.task_id, max_pages=50)
    assert first["new_products"] == 1 and repeated["new_products"] == repeated["new_detail_tasks"] == 0
    assert repeated["next_task_created"] is False and detail.status == TaskStatus.CANCELLED
    assert detail.checkpoint == {"keep": True} and session.scalar(select(func.count()).select_from(Product)) == 1


def test_listing_metadata_updates_version_before_overwriting_and_stale_source_cannot_revert(session):
    shop, task = initial_task(session)
    page = snapshot()
    persist_product_page(session, task, page, page_number=1, root_task_id=task.task_id, max_pages=50)
    row = product()
    row["title"] = "New product title"
    newer = snapshot([row], captured_at=page.captured_at + timedelta(seconds=1))
    persist_product_page(session, task, newer, page_number=1, root_task_id=task.task_id, max_pages=50)
    saved = session.scalar(select(Product))
    assert saved.title == "New product title" and saved.data_version == 2
    persist_product_page(session, task, page, page_number=1, root_task_id=task.task_id, max_pages=50)
    assert saved.title == "New product title" and saved.data_version == 2


def test_verified_details_are_not_erased_by_partial_listing(session):
    shop, task = initial_task(session)
    page = snapshot()
    persist_product_page(session, task, page, page_number=1, root_task_id=task.task_id, max_pages=50)
    saved = session.scalar(select(Product))
    saved.title, saved.description = "Detail title", "Complete detail description"
    saved.price_min = saved.price_max = Decimal("99.99")
    saved.last_success_at = page.captured_at
    saved.parser_version, saved.content_hash, saved.data_version = "detail_v1", "d" * 64, 7
    session.flush()
    persist_product_page(session, task, replace(page, captured_at=page.captured_at + timedelta(seconds=1)),
                         page_number=1, root_task_id=task.task_id, max_pages=50)
    assert saved.title == "Detail title" and saved.description == "Complete detail description"
    assert saved.price_min == Decimal("99.99") and saved.parser_version == "detail_v1"
    assert saved.content_hash == "d" * 64 and saved.data_version == 7


def test_owner_conflict_is_rejected_without_transferring_product(session):
    shop, task = initial_task(session)
    other = Shop(region="US", platform_shop_id="111", canonical_name="Another shop")
    session.add(other)
    session.flush()
    session.add(Product(region="US", shop_id=other.id, platform_product_id=product()["product_id"],
                        product_url=product()["seo_url"]["canonical_url"]))
    session.flush()
    with pytest.raises(TaskFailure):
        persist_product_page(session, task, snapshot(), page_number=1, root_task_id=task.task_id, max_pages=50)
    assert session.scalar(select(Product)).shop_id == other.id
    assert session.scalar(select(func.count()).select_from(CrawlTask)) == 1


def test_page_budget_is_partial_and_no_false_complete(session):
    shop, task = initial_task(session)
    summary = persist_product_page(session, task, snapshot(), page_number=1, root_task_id=task.task_id, max_pages=1)
    assert summary["complete"] is False and summary["stop_reason"] == "page_budget" and not summary["next_task_created"]


def test_exhausted_source_is_complete_without_extra_page(session):
    shop, task = initial_task(session)
    summary = persist_product_page(session, task, snapshot(has_more=False), page_number=1, root_task_id=task.task_id, max_pages=50)
    assert summary["complete"] is True and summary["stop_reason"] == "source_exhausted"
    assert summary["next_task_created"] is False


def test_domain_writes_and_child_tasks_roll_back_together(session):
    shop, task = initial_task(session)
    lease = claim_task(session, task.task_id, task.task_type, "test-worker", CONFIG)
    def apply(db, current):
        persist_product_page(db, current, snapshot(), page_number=1, root_task_id=current.task_id, max_pages=50)
        raise RuntimeError("forced rollback")
    with pytest.raises(RuntimeError), session.begin_nested():
        finalize_task(session, lease, CONFIG, result=ExecutionResult(apply=apply))
    assert session.scalar(select(func.count()).select_from(Product)) == 0
    assert session.scalar(select(func.count()).select_from(CrawlTask)) == 1
    assert session.get(CrawlTask, task.id).status == TaskStatus.RUNNING
