from contextlib import nullcontext
from dataclasses import replace
from threading import Event

import pytest
from sqlalchemy import func, select

from app.crawlers.product_images import IMAGE_SOURCE_VERSION
from app.models import ProductContent, ProductImage, ProductSnapshot, ProductVariant, TaskStatus
from app.queue.retry_policy import TaskFailure
from app.services.product_detail import ensure_initial_detail_task, persist_product_detail
from app.services.image_backfill import MEDIA_MODE, image_task_key, media_task_key
from app.crawlers.product_content import CONTENT_SOURCE_VERSION
from app.services.task_runtime import claim_task, finalize_task
from app.workers.contracts import HandlerContext
from app.workers.handlers import ProductDetailHandler
from test_product_detail import snapshot
from test_product_detail_storage import CONFIG, initial
from test_product_images_storage import image_page


def set_image_mode(task):
    task.payload = {**task.payload, "capture_mode": "images_only", "image_source_version": IMAGE_SOURCE_VERSION}
    task.idempotency_key = image_task_key(int(task.resource_id), task.payload["platform_product_id"], task.platform, task.region)


def test_image_handler_commits_only_gallery_even_when_baselines_are_enabled(session, monkeypatch, tmp_path):
    product, task = initial(session)
    persist_product_detail(session, task, snapshot())
    before = (product.price_min, product.content_hash, product.data_version, product.last_success_at)
    set_image_mode(task)
    config = replace(CONFIG, baseline_on_detail=True, evidence_directory=tmp_path)
    calls = []
    def fetch(*args):
        calls.append(1)
        return image_page()
    monkeypatch.setattr("app.workers.handlers.TikTokShopAdapter.fetch_product_detail", fetch)
    lease = claim_task(session, task.task_id, task.task_type, "images-only-test", config)
    context = HandlerContext(config, None, Event(), session_factory=lambda: nullcontext(session))
    result = ProductDetailHandler().prepare(lease, context)
    assert finalize_task(session, lease, config, result=result) == TaskStatus.SUCCEEDED
    assert calls == [1] and task.checkpoint["outcome"] == "available"
    assert (product.price_min, product.content_hash, product.data_version, product.last_success_at) == before
    assert session.scalar(select(func.count()).select_from(ProductImage)) == 2
    assert session.scalar(select(func.count()).select_from(ProductVariant)) == 2
    assert session.scalar(select(func.count()).select_from(ProductSnapshot)) == 0


def test_image_handler_skips_an_already_available_gallery_without_http(session, monkeypatch, tmp_path):
    product, task = initial(session)
    persist_product_detail(session, task, image_page())
    set_image_mode(task)
    config = replace(CONFIG, evidence_directory=tmp_path)
    def unexpected(*args): raise AssertionError("A current gallery must skip HTTP")
    monkeypatch.setattr("app.workers.handlers.TikTokShopAdapter.fetch_product_detail", unexpected)
    lease = claim_task(session, task.task_id, task.task_type, "images-skip-test", config)
    result = ProductDetailHandler().prepare(lease, HandlerContext(config, None, Event(),
        session_factory=lambda: nullcontext(session)))
    assert finalize_task(session, lease, config, result=result) == TaskStatus.SUCCEEDED
    assert task.checkpoint["outcome"] == "skipped_already_available"


def test_unknown_capture_mode_never_runs_full_detail_as_a_fallback(session, monkeypatch):
    _, task = initial(session)
    task.payload = {**task.payload, "capture_mode": "unexpected"}
    def unexpected(*args): raise AssertionError("Unknown modes must fail before HTTP")
    monkeypatch.setattr("app.workers.handlers.TikTokShopAdapter.fetch_product_detail", unexpected)
    lease = claim_task(session, task.task_id, task.task_type, "bad-mode-test", CONFIG)
    with pytest.raises(TaskFailure) as error:
        ProductDetailHandler().prepare(lease, HandlerContext(CONFIG, None, Event()))
    assert error.value.failure.code == "invalid_capture_mode"


def test_initial_detail_lookup_does_not_mistake_image_work_for_full_details(session):
    product, task = initial(session)
    set_image_mode(task)
    full, created = ensure_initial_detail_task(session, product, product.shop)
    assert created and full.task_id != task.task_id
    assert full.payload.get("capture_mode", "full") == "full"


def test_combined_media_capture_uses_one_request_without_changing_business_data(session, monkeypatch, tmp_path):
    product, task = initial(session)
    historical = snapshot()
    historical = replace(historical, detail=replace(historical.detail, content=None))
    persist_product_detail(session, task, historical)
    before = (product.price_min, product.description, product.content_hash, product.data_version, product.last_success_at)
    task.payload = {**task.payload, "capture_mode": MEDIA_MODE, "image_source_version": IMAGE_SOURCE_VERSION,
                    "content_source_version": CONTENT_SOURCE_VERSION}
    task.idempotency_key = media_task_key(product.id, product.platform_product_id, task.platform, task.region)
    calls = []
    def fetch(*args):
        calls.append(1)
        return image_page()
    monkeypatch.setattr("app.workers.handlers.TikTokShopAdapter.fetch_product_detail", fetch)
    config = replace(CONFIG, baseline_on_detail=True, evidence_directory=tmp_path)
    lease = claim_task(session, task.task_id, task.task_type, "media-test", config)
    result = ProductDetailHandler().prepare(lease, HandlerContext(config, None, Event(),
        session_factory=lambda: nullcontext(session)))
    assert finalize_task(session, lease, config, result=result) == TaskStatus.SUCCEEDED
    assert calls == [1] and task.checkpoint["capture_mode"] == MEDIA_MODE
    assert task.checkpoint["content_saved"] and task.checkpoint["images_upserted"] == 2
    assert session.scalar(select(func.count()).select_from(ProductContent)) == 1
    assert (product.price_min, product.description, product.content_hash, product.data_version, product.last_success_at) == before
    assert session.scalar(select(func.count()).select_from(ProductSnapshot)) == 0
