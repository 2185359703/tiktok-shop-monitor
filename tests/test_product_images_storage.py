from dataclasses import asdict, replace
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models.product_image import ProductImage
from app.crawlers.product_images import ImageCandidate, image_candidates
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


def test_complete_gallery_retires_only_photos_missing_from_complete_observation(session):
    product, _ = initial(session)
    page = image_page()
    persist_product_images(session, product.id, page.detail.images, source_url=page.final_url,
                           source_kind="detail", observed_at=page.captured_at)
    current = image_candidates([gallery()[1]])
    assert all(image.source_complete for image in current)
    persist_product_images(session, product.id, current, source_url=page.final_url,
                           source_kind="detail", observed_at=page.captured_at + timedelta(seconds=1))
    rows = {row.image_key: row for row in session.scalars(select(ProductImage))}
    assert len(rows) == 2
    assert rows[page.detail.images[0].image_key].is_current is False
    assert rows[current[0].image_key].is_current is True and rows[current[0].image_key].position == 0


def test_partial_gallery_merges_valid_photos_without_retiring_previous_images(session):
    product, _ = initial(session)
    page = image_page()
    persist_product_images(session, product.id, page.detail.images, source_url=page.final_url,
                           source_kind="detail", observed_at=page.captured_at)
    partial = image_candidates([gallery()[1], {"unknown_image_shape": "unparsed"},
                                {"uri": "tos/public/new-photo", "url_list": [page.detail.images[0].image_url]}])
    assert len(partial) == 2 and all(not image.source_complete for image in partial)
    assert persist_product_images(session, product.id, partial, source_url=page.final_url,
                                  source_kind="detail", observed_at=page.captured_at + timedelta(seconds=1)) == 2
    rows = list(session.scalars(select(ProductImage)))
    assert len(rows) == 3 and all(row.is_current for row in rows)
    assert {image.image_key for image in page.detail.images} <= {row.image_key for row in rows}
    assert all(row.source_kind == "detail" for row in rows)
    by_key = {row.image_key: row for row in rows}
    assert [(by_key[image.image_key].role, by_key[image.image_key].position) for image in page.detail.images] == [
        ("cover", 0), ("gallery", 1)]
    assert by_key[partial[1].image_key].role == "gallery" and by_key[partial[1].image_key].position == 2


def test_truncated_gallery_preserves_images_outside_the_projection_limit(session):
    product, _ = initial(session)
    page = image_page()
    persist_product_images(session, product.id, page.detail.images, source_url=page.final_url,
                           source_kind="detail", observed_at=page.captured_at)
    rows = [{"uri": f"tos/public/new-photo-{index}", "url_list": [page.detail.images[0].image_url]}
            for index in range(101)]
    truncated = image_candidates(rows)
    persist_product_images(session, product.id, truncated, source_url=page.final_url,
                           source_kind="detail", observed_at=page.captured_at + timedelta(seconds=1))
    assert session.scalar(select(func.count()).select_from(ProductImage).where(ProductImage.is_current.is_(True))) == 102


def test_legacy_photo_projection_can_merge_but_cannot_retire_gallery(session):
    product, _ = initial(session)
    page = image_page()
    persist_product_images(session, product.id, page.detail.images, source_url=page.final_url,
                           source_kind="detail", observed_at=page.captured_at)
    legacy_record = asdict(page.detail.images[1])
    legacy_record.pop("source_complete")
    legacy_image = ImageCandidate.from_record(legacy_record)
    assert legacy_image.source_complete is False
    persist_product_images(session, product.id, (legacy_image,), source_url=page.final_url,
                           source_kind="detail", observed_at=page.captured_at + timedelta(seconds=1))
    assert session.scalar(select(func.count()).select_from(ProductImage).where(ProductImage.is_current.is_(True))) == 2


def test_listing_cannot_replace_even_a_partial_detail_gallery(session):
    product, _ = initial(session)
    page = image_page()
    partial = image_candidates([gallery()[0], {"image": "unsupported"}])
    persist_product_images(session, product.id, partial, source_url=page.final_url,
                           source_kind="detail", observed_at=page.captured_at)
    listing = image_candidates([gallery()[1]])
    assert persist_product_images(session, product.id, listing, source_url=page.final_url,
                                  source_kind="listing", observed_at=page.captured_at + timedelta(seconds=1)) == 0
    rows = list(session.scalars(select(ProductImage)))
    assert len(rows) == 1 and rows[0].image_key == partial[0].image_key
    assert rows[0].is_current is True and rows[0].source_kind == "detail"
