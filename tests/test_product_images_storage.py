from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models.product_image import ProductImage
from app.services.product_detail import persist_product_detail
from app.services.product_images import cover_images, persist_product_images
from test_product_detail import block, snapshot
from test_product_detail_storage import initial
from test_product_images import gallery


def image_page():
    value = block()
    value["product_info"]["images"] = gallery()
    return snapshot(value)


def test_images_commit_with_detail_and_do_not_change_content_hash_or_sku_version(session):
    product, task = initial(session)
    original = snapshot()
    persist_product_detail(session, task, original)
    content_hash, version = product.content_hash, product.data_version
    page = image_page()
    result = persist_product_detail(session, task, page)
    assert result["images_upserted"] == 2
    assert product.content_hash == content_hash and product.data_version == version
    persist_product_detail(session, task, replace(page, captured_at=page.captured_at + timedelta(seconds=1)))
    assert session.scalar(select(func.count()).select_from(ProductImage)) == 2
    assert cover_images(session, [product.id])[product.id]["image_url"] == page.detail.images[0].image_url


def test_listing_and_older_detail_never_replace_newer_gallery(session):
    product, _ = initial(session)
    page = image_page()
    images = page.detail.images
    save = lambda candidates, kind, at: persist_product_images(session, product.id, candidates,
                                    source_url=page.final_url, source_kind=kind, observed_at=at)
    assert save(images[:1], "listing", page.captured_at) == 1
    assert save(images, "detail", page.captured_at) == 2
    assert save(images[:1], "listing", page.captured_at + timedelta(seconds=1)) == 0
    assert save(images[:1], "detail", page.captured_at - timedelta(seconds=1)) == 0
    assert sum(row.is_current for row in session.scalars(select(ProductImage))) == 2
    assert save((), "detail", page.captured_at + timedelta(seconds=2)) == 0
    assert save(images[1:], "detail", page.captured_at + timedelta(seconds=3)) == 1
    assert sum(row.is_current for row in session.scalars(select(ProductImage))) == 1


def test_photo_writes_rollback_with_collection_transaction(session):
    product, task = initial(session)
    with pytest.raises(RuntimeError), session.begin_nested():
        persist_product_detail(session, task, image_page())
        raise RuntimeError("force rollback")
    assert session.scalar(select(func.count()).select_from(ProductImage)) == 0
    assert session.get(type(product), product.id).last_success_at is None
