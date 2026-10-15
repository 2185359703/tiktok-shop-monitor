from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.main import create_app
from app.models import CrawlAttempt, CrawlTask, Product, ProductVariant, ProductSnapshot, ResourceType, Shop, TaskStatus, TaskType
from app.services.product_detail import ensure_initial_detail_task, persist_product_detail
from app.services.task_runtime import claim_task, ExecutionResult, finalize_task
from app.settings import Settings
from app.workers.contracts import HandlerContext
from app.workers.handlers import ProductDetailHandler
from test_product_detail import block, snapshot, PID, SID, URL


CONFIG = Settings(enabled_task_types=(TaskType.PRODUCT_DETAIL,))


def initial(session):
    shop = Shop(region="US", platform_shop_id=SID, canonical_name="ANRABESS")
    session.add(shop)
    session.flush()
    product = Product(region="US", shop_id=shop.id, platform_product_id=PID, product_url=URL, title="Listing title")
    session.add(product)
    session.flush()
    task, _ = ensure_initial_detail_task(session, product, shop)
    return product, task


def test_detail_worker_commits_product_skus_and_attempt_together(session, monkeypatch, tmp_path):
    product, task = initial(session)
    page = snapshot()
    lease = claim_task(session, task.task_id, task.task_type, "detail-test", CONFIG)
    monkeypatch.setattr("app.workers.handlers.TikTokShopAdapter.fetch_product_detail", lambda self, source, shop, stop: page)
    result = ProductDetailHandler().prepare(lease, HandlerContext(replace(CONFIG, evidence_directory=tmp_path), None, Event()))
    assert finalize_task(session, lease, CONFIG, result=result) == TaskStatus.SUCCEEDED
    assert product.last_success_at is not None and product.content_hash and product.data_version == 2
    assert product.material is None and product.description == "Merchant description"
    assert len(session.scalars(select(ProductVariant)).all()) == 2
    assert session.scalar(select(CrawlAttempt)).succeeded is True
    assert session.scalar(select(func.count()).select_from(ProductSnapshot)) == 0


def test_repeated_details_no_duplicate_or_version_increase(session):
    product, task = initial(session)
    page = snapshot()
    first = persist_product_detail(session, task, page)
    repeated = persist_product_detail(session, task, replace(page, captured_at=page.captured_at + timedelta(seconds=1)))
    assert first["new_variants"] == 2 and repeated["new_variants"] == 0 and product.data_version == 2
    assert len(session.scalars(select(ProductVariant)).all()) == 2


def test_old_details_cannot_revert_prices_or_skus(session):
    product, task = initial(session)
    old = snapshot()
    newer_value = block()
    newer_value["product_info"]["title"] = "Updated merchant title"
    newer = snapshot(newer_value, captured_at=old.captured_at + timedelta(seconds=1))
    persist_product_detail(session, task, newer)
    result = persist_product_detail(session, task, old)
    assert result["stale_observation_skipped"] and product.title == "Updated merchant title"


def test_missing_old_sku_is_retained_not_deleted_or_marked_removed(session):
    product, task = initial(session)
    page = snapshot()
    persist_product_detail(session, task, page)
    value = block()
    value["product_info"]["skus"] = value["product_info"]["skus"][:1]
    persist_product_detail(session, task, snapshot(value, captured_at=page.captured_at + timedelta(seconds=1)))
    assert session.scalar(select(func.count()).select_from(ProductVariant)) == 2
    assert len(product.raw_attributes["observed_sku_ids"]) == 1


def test_existing_sku_identity_key_is_preserved(session):
    product, task = initial(session)
    page = snapshot()
    variant = page.detail.variants[0]
    session.add(ProductVariant(product_id=product.id, platform_variant_id=variant.platform_variant_id, variant_key="b" * 64))
    session.flush()
    persist_product_detail(session, task, page)
    saved = session.scalar(select(ProductVariant).where(ProductVariant.platform_variant_id == variant.platform_variant_id))
    assert saved.variant_key == "b" * 64


def test_ownership_conflict_rolls_back_and_does_not_transfer_product(session):
    product, task = initial(session)
    wrong = replace(snapshot(), detail=replace(snapshot().detail, platform_shop_id="999"))
    with pytest.raises(Exception): persist_product_detail(session, task, wrong)
    assert session.scalar(select(func.count()).select_from(ProductVariant)) == 0


def test_full_detail_and_variant_writes_roll_back_together(session):
    product, task = initial(session)
    lease = claim_task(session, task.task_id, task.task_type, "rollback", CONFIG)
    def apply(db, current):
        persist_product_detail(db, current, snapshot())
        raise RuntimeError("forced rollback")
    with pytest.raises(RuntimeError), session.begin_nested():
        finalize_task(session, lease, CONFIG, result=ExecutionResult(apply=apply))
    assert session.scalar(select(func.count()).select_from(ProductVariant)) == 0
    assert session.get(Product, product.id).last_success_at is None
    assert session.get(CrawlTask, task.id).status == TaskStatus.RUNNING


def test_api_detail_endpoints_empty_or_not_found(mysql_engine):
    with TestClient(create_app(settings=Settings(), engine=mysql_engine)) as client:
        assert client.get("/api/v1/products/999999/variants").status_code == 404
        assert client.get("/api/v1/products/999999").status_code == 404
