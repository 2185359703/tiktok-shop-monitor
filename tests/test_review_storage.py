from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.crawlers.product_reviews import ReviewSnapshot, parse_review_data
from app.models import CrawlAttempt, CrawlTask, Product, ProductReview, Shop, TaskStatus, TaskType
from app.queue.retry_policy import Failure, TaskFailure
from app.services.product_reviews import ensure_review_task, persist_review_page, validate_review_payload
from app.services.task_runtime import ExecutionResult, LeaseLost, claim_task, finalize_task
from app.settings import Settings
from test_product_reviews import PID, SID, URL, review_data


CONFIG = Settings(enabled_task_types=(TaskType.PRODUCT_REVIEWS,), review_max_pages=3)


def setup(session, max_pages=3):
    shop = Shop(region="US", platform_shop_id=SID, canonical_name="Review fixture")
    session.add(shop)
    session.flush()
    product = Product(region="US", shop_id=shop.id, platform_product_id=PID, product_url=URL, title="Review fixture")
    session.add(product)
    session.flush()
    task, created = ensure_review_task(session, CONFIG, product.id, max_pages=max_pages)
    assert created
    return product, task


def snapshot(data=None, page=1):
    parsed = parse_review_data(data if data is not None else review_data(), PID, page_number=page, page_size=5,
                               source_kind="document" if page == 1 else "api")
    return ReviewSnapshot(parsed, URL, "a" * 64, 1234, datetime.now(timezone.utc), 120)


def commit(session, task, capture):
    lease = claim_task(session, task.task_id, task.task_type, "reviews", CONFIG)
    validate_review_payload(lease)
    progress = {}
    def apply(db, row):
        progress.update(persist_review_page(db, row, capture))
    finalize_task(session, lease, CONFIG, result=ExecutionResult(apply=apply, checkpoint=progress))
    return lease


def test_page_rows_next_task_and_checkpoint_commit_together(session):
    product, task = setup(session)
    assert ensure_review_task(session, CONFIG, product.id)[1] is False
    lease = commit(session, task, snapshot())
    rows = session.scalars(select(ProductReview).where(ProductReview.product_id == product.id)).all()
    next_task = session.scalar(select(CrawlTask).where(CrawlTask.task_id == task.checkpoint["next_task_id"]))
    assert len(rows) == 5 and next_task.payload["page_number"] == 2
    assert next_task.payload["seen_page_hashes"] == [snapshot().page.page_hash]
    assert not task.checkpoint["complete"] and task.status == TaskStatus.SUCCEEDED
    assert session.scalar(select(CrawlAttempt).where(CrawlAttempt.task_id == task.id)).succeeded
    assert product.rating is None and product.review_count is None
    with pytest.raises(LeaseLost):
        finalize_task(session, lease, CONFIG, result=ExecutionResult())


def test_repeat_stale_and_updated_reviews_do_not_duplicate_or_erase_data(session):
    product, task = setup(session, max_pages=1)
    original = snapshot()
    first = persist_review_page(session, task, original)
    assert first["reviews_inserted"] == 5 and first["stop_reason"] == "page_budget" and not first["complete"]
    assert persist_review_page(session, task, original)["reviews_inserted"] == 0
    changed = review_data()
    changed["product_reviews"][0]["review_text"] = "Edited by author"
    newer = replace(snapshot(changed), captured_at=original.captured_at + timedelta(seconds=1))
    assert persist_review_page(session, task, newer)["reviews_updated"] == 1
    assert persist_review_page(session, task, original)["stale_reviews_skipped"] == 5
    row = session.scalar(select(ProductReview).where(ProductReview.platform_review_id == "1"))
    assert row.text == "Edited by author" and row.content_version == 2
    assert session.scalar(select(func.count()).select_from(ProductReview)) == 5


def test_failed_second_page_preserves_first_page_and_retry_position(session):
    product, task = setup(session)
    commit(session, task, snapshot())
    next_task = session.scalar(select(CrawlTask).where(CrawlTask.task_id == task.checkpoint["next_task_id"]))
    lease = claim_task(session, next_task.task_id, next_task.task_type, "reviews", CONFIG)
    assert finalize_task(session, lease, CONFIG, failure=Failure("review_api_business_error", "error", True)) == TaskStatus.RETRY_WAITING
    assert next_task.payload["page_number"] == 2 and next_task.payload["root_task_id"] == task.task_id
    assert session.scalar(select(func.count()).select_from(ProductReview)) == 5


def test_repeated_page_rejected_without_generating_more_tasks(session):
    product, task = setup(session)
    commit(session, task, snapshot())
    next_task = session.scalar(select(CrawlTask).where(CrawlTask.task_id == task.checkpoint["next_task_id"]))
    with pytest.raises(TaskFailure) as error:
        persist_review_page(session, next_task, snapshot(page=2))
    assert error.value.failure.code == "review_repeated_page"
    assert session.scalar(select(func.count()).select_from(CrawlTask)) == 2


def test_explicit_last_page_completes_chain_and_empty_text_time_stay_null(session):
    product, task = setup(session)
    commit(session, task, snapshot())
    next_task = session.scalar(select(CrawlTask).where(CrawlTask.task_id == task.checkpoint["next_task_id"]))
    data = review_data(more=False, count=2, start=6)
    data["product_reviews"][0].update(review_text=None, review_time=None)
    commit(session, next_task, snapshot(data, page=2))
    assert next_task.checkpoint["complete"] and next_task.checkpoint["next_task_id"] is None
    row = session.scalar(select(ProductReview).where(ProductReview.platform_review_id == "6"))
    assert row.text is None and row.reviewed_at is None
    assert session.scalar(select(func.count()).select_from(ProductReview)) == 7
