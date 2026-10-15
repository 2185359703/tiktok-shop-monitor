import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.mysql import dialect

from app.api.routes import session_dependency
from app.api.routes_images import router
from app.crawlers.product_images import IMAGE_SOURCE_VERSION
from app.models import CrawlTask, Product, ProductContent, ProductSnapshot, ProductVariant, TaskStatus, TaskType
from app.crawlers.product_content import CONTENT_SOURCE_VERSION, parse_product_content
from app.models.product_image import ProductImage
from app.queue.retry_policy import TaskFailure
from app.services.image_backfill import (
    IMAGE_MODE, MEDIA_MODE, capture_mode_expression, enqueue_image_tasks, image_backfill_status,
    image_capture_state, image_task_key, persist_image_observation, save_image_evidence,
)
from app.services.product_detail import persist_product_detail
from app.services.seed_importer import SeedBatch
from test_collection import scope_fixture
from test_product_detail import SID, snapshot
from test_product_detail_storage import initial
from test_product_images_storage import image_page


def settings():
    return SimpleNamespace(platform="tiktok_shop", region="US", currency="USD", image_backfill_enabled=True,
                           enabled_task_types=(TaskType.PRODUCT_DETAIL,))


def image_payload(product):
    return {"product_id": str(product.id), "platform_product_id": product.platform_product_id,
            "shop_id": str(product.shop_id), "platform_shop_id": SID, "product_url": product.product_url,
            "pricing_context": "public_listed", "capture_mode": IMAGE_MODE,
            "image_source_version": IMAGE_SOURCE_VERSION, "scope_hash": "a" * 64}


def test_capture_mode_expression_preserves_old_full_tasks_and_key_has_no_run_time():
    sql = str(capture_mode_expression().compile(dialect=dialect(), compile_kwargs={"literal_binds": True}))
    assert "coalesce" in sql.lower() and "'full'" in sql and "capture_mode" in sql
    key = image_task_key(7, "1000", "tiktok_shop", "US")
    assert key == image_task_key(7, "1000", "tiktok_shop", "US")
    assert key != image_task_key(8, "1000", "tiktok_shop", "US")
    assert len(key) == 64


def test_image_evidence_excludes_full_catalog_and_sku_payload(tmp_path):
    page = image_page()
    path = save_image_evidence(page, tmp_path)
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["capture_mode"] == IMAGE_MODE and len(value["images"]) == 2
    assert value["platform_product_id"] == page.detail.platform_product_id
    assert not {"detail", "variants", "skus", "title", "raw_attributes", "description"} & value.keys()
    assert "variants" not in path.read_text(encoding="utf-8")


def test_state_route_is_read_only_and_never_returns_private_input(monkeypatch):
    monkeypatch.setattr("app.api.routes_images.fixed_image_seed_batch", lambda: object())
    monkeypatch.setattr("app.api.routes_images.image_backfill_status", lambda db, config, batch: {
        "status": "running", "active": 4, "dead": 1, "available": 2,
        "no_images": 1, "skipped": 1, "products_with_current_images": 3})
    app = FastAPI()
    app.state.settings = settings()
    app.include_router(router)
    app.dependency_overrides[session_dependency] = lambda: object()
    with TestClient(app) as client:
        data = client.get("/api/v1/images/backfill/status").json()
        assert data["active"] == 4 and data["products_with_current_images"] == 3
        assert client.post("/api/v1/images/backfill/status").status_code == 405


def test_enqueue_preview_is_read_only_and_replay_keeps_the_same_global_task(session):
    batch, _ = scope_fixture(session)
    before = session.scalar(select(func.count()).select_from(CrawlTask))
    preview = enqueue_image_tasks(session, settings(), batch)
    assert preview["planned_tasks"] == 1 and preview["created_tasks"] == preview["http_requests"] == 0
    assert session.scalar(select(func.count()).select_from(CrawlTask)) == before
    first = enqueue_image_tasks(session, settings(), batch, execute=True, workers_ready=True)
    again = enqueue_image_tasks(session, settings(), batch, execute=True, workers_ready=True)
    assert first["created_tasks"] == 1 and again["created_tasks"] == 0 and again["existing_tasks"] == 1
    task = session.scalar(select(CrawlTask).where(capture_mode_expression() == IMAGE_MODE))
    task.status = TaskStatus.DEAD
    session.flush()
    assert enqueue_image_tasks(session, settings(), batch, execute=True, workers_ready=True)["created_tasks"] == 0
    assert task.status == TaskStatus.DEAD


def test_enqueue_requires_an_explicit_switch_worker_capability_and_resolved_scope(session):
    batch, _ = scope_fixture(session)
    config = settings()
    config.image_backfill_enabled = False
    with pytest.raises(ValueError):
        enqueue_image_tasks(session, config, batch, execute=True, workers_ready=True)
    with pytest.raises(ValueError):
        enqueue_image_tasks(session, settings(), batch, execute=True)
    with pytest.raises(ValueError):
        enqueue_image_tasks(session, settings(), SeedBatch((), 0))


def test_capture_state_validates_identity_and_skips_existing_gallery(session):
    product, task = initial(session)
    payload = image_payload(product)
    assert not image_capture_state(session, product.id, payload, "tiktok_shop", "US")
    for field, value in (("shop_id", "999999"), ("platform_product_id", "1234"), ("image_source_version", "unknown")):
        with pytest.raises(TaskFailure):
            image_capture_state(session, product.id, {**payload, field: value}, "tiktok_shop", "US")
    persist_product_detail(session, task, image_page())
    assert image_capture_state(session, product.id, payload, "tiktok_shop", "US")


def test_image_commit_changes_only_photos_and_preserves_original_business_observations(session):
    product, task = initial(session)
    persist_product_detail(session, task, snapshot())
    task.payload = image_payload(product)
    before = {name: getattr(product, name) for name in (
        "title", "description", "price_min", "price_max", "stock_status", "raw_attributes",
        "content_hash", "data_version", "last_seen_at", "last_success_at", "parser_version")}
    variants = [dict(row) for row in session.execute(select(ProductVariant.__table__).order_by(ProductVariant.id)).mappings()]
    result = persist_image_observation(session, task, image_page())
    assert result["outcome"] == "available" and result["images_upserted"] == 2
    assert {name: getattr(product, name) for name in before} == before
    assert [dict(row) for row in session.execute(select(ProductVariant.__table__).order_by(ProductVariant.id)).mappings()] == variants
    assert session.scalar(select(func.count()).select_from(ProductSnapshot)) == 0
    assert session.scalar(select(func.count()).select_from(ProductImage)) == 2


def test_no_images_remains_visible_and_status_uses_actual_coverage(session):
    batch, _ = scope_fixture(session)
    enqueue_image_tasks(session, settings(), batch, execute=True, workers_ready=True)
    task = session.scalar(select(CrawlTask).where(capture_mode_expression() == IMAGE_MODE))
    result = persist_image_observation(session, task, snapshot())
    assert result["outcome"] == "no_images" and result["images_upserted"] == 0
    task.status, task.checkpoint = TaskStatus.SUCCEEDED, result
    session.flush()
    state = image_backfill_status(session, settings(), batch)
    assert state["no_images"] == 1 and state["available"] == 0 and state["products_with_current_images"] == 0
    assert state["historical_details_missing_images"] == 1 and state["status"] == "partial"
    assert "http" not in json.dumps({key: value for key, value in state.items() if key != "http_requests"})
    assert enqueue_image_tasks(session, settings(), batch, execute=True, workers_ready=True)["created_tasks"] == 0


def test_wrong_response_identity_rolls_back_photo_write(session):
    product, task = initial(session)
    task.payload = image_payload(product)
    page = image_page()
    bad = replace(page, detail=replace(page.detail, platform_shop_id="999999"))
    with pytest.raises(TaskFailure), session.begin_nested():
        persist_image_observation(session, task, bad)
    assert session.scalar(select(func.count()).select_from(ProductImage)) == 0


def test_combined_backfill_plans_content_even_when_a_gallery_exists(session):
    batch, _ = scope_fixture(session)
    product = session.scalar(select(Product))
    from app.services.product_images import persist_product_images
    page = image_page()
    persist_product_images(session, product.id, page.detail.images, source_url=page.final_url,
                           source_kind="detail", observed_at=page.captured_at)
    session.execute(delete(ProductContent).where(ProductContent.product_id == product.id))
    preview = enqueue_image_tasks(session, settings(), batch, capture_mode=MEDIA_MODE)
    assert preview["planned_tasks"] == 1 and preview["created_tasks"] == 0
    enqueue_image_tasks(session, settings(), batch, execute=True, workers_ready=True, capture_mode=MEDIA_MODE)
    task = session.scalar(select(CrawlTask).where(capture_mode_expression() == MEDIA_MODE))
    assert not image_capture_state(session, product.id, task.payload, "tiktok_shop", "US")


def test_combined_backfill_cannot_claim_complete_from_photos_alone(session):
    batch, _ = scope_fixture(session)
    enqueue_image_tasks(session, settings(), batch, execute=True, workers_ready=True, capture_mode=MEDIA_MODE)
    task = session.scalar(select(CrawlTask).where(capture_mode_expression() == MEDIA_MODE))
    product = session.scalar(select(Product))
    from app.services.product_images import persist_product_images
    page = image_page()
    persist_product_images(session, product.id, page.detail.images, source_url=page.final_url,
                           source_kind="detail", observed_at=page.captured_at)
    session.execute(delete(ProductContent).where(ProductContent.product_id == product.id))
    task.status, task.checkpoint = TaskStatus.SUCCEEDED, {"outcome": "available"}
    session.flush()
    status = image_backfill_status(session, settings(), batch)
    assert status["historical_details_missing_images"] == 0
    assert status["historical_details_missing_content"] == 1 and status["status"] == "partial"
    assert status["tasks_by_mode"][MEDIA_MODE] == 1


def test_partial_content_stays_eligible_and_cannot_be_skipped_as_complete(session):
    batch, _ = scope_fixture(session)
    product = session.scalar(select(Product))
    row = session.scalar(select(ProductContent).where(ProductContent.product_id == product.id))
    assert row is not None
    row.completeness = "partial"
    from app.services.product_images import persist_product_images
    page = image_page()
    persist_product_images(session, product.id, page.detail.images, source_url=page.final_url,
                           source_kind="detail", observed_at=page.captured_at)
    result = enqueue_image_tasks(session, settings(), batch, execute=True, workers_ready=True, capture_mode=MEDIA_MODE)
    assert result["created_tasks"] == 1
    task = session.scalar(select(CrawlTask).where(capture_mode_expression() == MEDIA_MODE))
    assert not image_capture_state(session, product.id, task.payload, "tiktok_shop", "US")
    task.status, task.checkpoint = TaskStatus.SUCCEEDED, {"outcome": "available"}
    session.flush()
    status = image_backfill_status(session, settings(), batch)
    assert status["status"] == "partial" and status["historical_details_incomplete_content"] == 1
    assert enqueue_image_tasks(session, settings(), batch, execute=True, workers_ready=True,
                               capture_mode=MEDIA_MODE)["created_tasks"] == 0
