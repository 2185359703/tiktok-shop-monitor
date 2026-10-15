from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.crawlers.product_content import parse_product_content
from app.models import ChangeEvent, ProductContent, ProductSnapshot, ProductVariant
from app.queue.retry_policy import TaskFailure
from app.services.product_content import content_observation_metadata, persist_product_content
from app.services.product_detail import persist_product_detail
from test_product_detail import snapshot
from test_product_detail_storage import initial


def content(text="Merchant coat"):
    return parse_product_content({"specifications": [{"name": "Fabric weight", "value": "Heavyweight"}],
                                  "desc_blocks": [{"type": "text", "text": text}]})


def save(session, product, projection, at, response_hash="c" * 64, url=None):
    return persist_product_content(session, product.id, projection, source_url=url or product.product_url,
                                   response_hash=response_hash, observed_at=at)


def test_content_write_is_independent_of_product_skus_and_history(session):
    product, task = initial(session)
    page = snapshot()
    persist_product_detail(session, task, page)
    fields = ("title", "description", "raw_attributes", "price_min", "price_max", "stock_status", "last_success_at", "last_seen_at", "content_hash", "data_version", "parser_version")
    before = {key: getattr(product, key) for key in fields}
    sku_before = [(row.id, row.price, row.stock_status, row.variant_payload_hash) for row in session.scalars(select(ProductVariant))]
    result = save(session, product, content(), page.captured_at + timedelta(seconds=1))
    assert result["content_saved"] and result["complete"]
    session.refresh(product)
    assert before == {key: getattr(product, key) for key in fields}
    assert sku_before == [(row.id, row.price, row.stock_status, row.variant_payload_hash) for row in session.scalars(select(ProductVariant))]
    assert session.scalar(select(func.count()).select_from(ProductSnapshot)) == 0
    assert session.scalar(select(func.count()).select_from(ChangeEvent)) == 0


def test_content_upsert_replay_stale_and_versioning(session):
    product, _ = initial(session)
    now = datetime.now(timezone.utc)
    first = save(session, product, content(), now)
    repeated = save(session, product, content(), now + timedelta(seconds=1))
    assert first["content_document_version"] == repeated["content_document_version"] == 1
    assert session.scalar(select(func.count()).select_from(ProductContent)) == 1
    changed = save(session, product, content("Updated merchant coat"), now + timedelta(seconds=2))
    assert changed["content_document_version"] == 2
    stale = save(session, product, content("Old coat"), now)
    assert stale["stale_content_skipped"] and not stale["content_saved"]
    assert session.scalar(select(ProductContent)).description_text == "Updated merchant coat"


def test_incomplete_or_empty_page_does_not_erase_existing_introduction(session):
    product, _ = initial(session)
    now = datetime.now(timezone.utc)
    save(session, product, content(), now)
    for source in ({}, {"specifications": [], "desc_blocks": []}, {"specifications": [], "desc_blocks": [{"type": "unknown"}]}):
        result = save(session, product, parse_product_content(source), now + timedelta(seconds=1))
        assert result["retained_existing_content"] and not result["content_saved"]
    assert session.scalar(select(ProductContent)).description_text == "Merchant coat"


@pytest.mark.parametrize("invalid_kind", ["product_url", "response_hash", "naive_time", "uncontrolled_projection"])
def test_bad_identity_or_evidence_cannot_write_content(session, invalid_kind):
    product, _ = initial(session)
    projection, now, response_hash, url = content(), datetime.now(timezone.utc), "c" * 64, product.product_url
    if invalid_kind == "product_url":
        url = "https://shop.tiktok.com/us/pdp/wrong/12345"
    elif invalid_kind == "response_hash":
        response_hash = "unverified"
    elif invalid_kind == "naive_time":
        now = now.replace(tzinfo=None)
    else:
        projection = replace(projection, source_version="unknown_version")
    with pytest.raises(TaskFailure):
        save(session, product, projection, now, response_hash, url)
    assert session.scalar(select(func.count()).select_from(ProductContent)) == 0


def test_content_obeys_parent_collection_transaction(session):
    product, _ = initial(session)
    with pytest.raises(RuntimeError), session.begin_nested():
        save(session, product, content(), datetime.now(timezone.utc))
        raise RuntimeError("rollback collection")
    assert session.scalar(select(func.count()).select_from(ProductContent)) == 0


def test_complementary_partial_sections_are_supplemented_without_refreshing_old_materials(session):
    product, _ = initial(session)
    now = datetime.now(timezone.utc)
    parameters = parse_product_content({"specifications": [{"name": "Material", "value": "Cotton"}]})
    description = parse_product_content({"desc_blocks": [{"type": "text", "text": "Merchant coat"}]})
    save(session, product, parameters, now)
    result = save(session, product, description, now + timedelta(seconds=5), "d" * 64)
    row = session.scalar(select(ProductContent))
    assert result["content_saved"] and not result["complete"] and row.completeness == "partial"
    assert row.sections["specifications"][0]["value"] == "Cotton" and row.description_text == "Merchant coat"
    assert row.observed_at == now and row.response_hash == "c" * 64
    metadata = content_observation_metadata(row)
    assert metadata["section_observations"]["specifications"]["observed_at"] == now.isoformat()
    assert metadata["section_observations"]["description"]["observed_at"] == (now + timedelta(seconds=5)).isoformat()
    assert metadata["latest_observation"]["observed_at"] == (now + timedelta(seconds=5)).isoformat()
    assert product.last_success_at is None and product.content_hash is None


def test_partial_parameters_add_new_names_but_do_not_replace_previous_material_claim(session):
    product, _ = initial(session)
    now = datetime.now(timezone.utc)
    save(session, product, parse_product_content({"specifications": [{"name": "Material", "value": "Cotton"}]}), now)
    newer = parse_product_content({"specifications": [{"name": "Material", "value": "Polyester"}, {"name": "Fit", "value": "Loose"}]})
    result = save(session, product, newer, now + timedelta(seconds=1))
    row = session.scalar(select(ProductContent))
    assert result["content_saved"] and row.completeness == "partial"
    assert row.sections["specifications"] == [{"name": "Material", "value": "Cotton", "source": "specifications"},
        {"name": "Fit", "value": "Loose", "source": "specifications"}]
    assert row.observed_at == now


def test_new_partial_observation_keeps_complete_document_age_and_records_actual_coverage(session):
    product, _ = initial(session)
    now = datetime.now(timezone.utc)
    first = save(session, product, content(), now)
    result = save(session, product, parse_product_content({"specifications": []}), now + timedelta(seconds=2))
    row = session.scalar(select(ProductContent))
    assert not result["content_saved"] and result["observation_recorded"]
    assert row.completeness == "complete" and row.observed_at == now
    assert row.content_hash == first["content_document_hash"] and row.content_version == first["content_document_version"]
    metadata = content_observation_metadata(row)
    assert metadata["latest_observation"]["completeness"] == "partial"
    assert metadata["latest_observation"]["observed_at"] == (now + timedelta(seconds=2)).isoformat()


def test_mixed_document_stale_guard_uses_newest_retained_section_not_earliest_document_time(session):
    product, _ = initial(session)
    now = datetime.now(timezone.utc)
    save(session, product, parse_product_content({"specifications": [{"name": "Material", "value": "Cotton"}]}), now)
    save(session, product, parse_product_content({"desc_blocks": [{"type": "text", "text": "New coat"}]}), now + timedelta(seconds=2))
    result = save(session, product, content("Out-of-order coat"), now + timedelta(seconds=1))
    assert result["stale_content_skipped"]
    assert session.scalar(select(ProductContent)).description_text == "New coat"


def test_later_incomplete_attempt_does_not_block_valid_intermediate_complete_observation(session):
    product, _ = initial(session)
    now = datetime.now(timezone.utc)
    save(session, product, content(), now)
    save(session, product, parse_product_content({}), now + timedelta(seconds=2))
    result = save(session, product, content("Confirmed newer coat"), now + timedelta(seconds=1))
    row = session.scalar(select(ProductContent))
    assert result["content_saved"] and row.description_text == "Confirmed newer coat"
    assert row.observed_at == now + timedelta(seconds=1)
    assert content_observation_metadata(row)["latest_observation"]["observed_at"] == (now + timedelta(seconds=2)).isoformat()


def test_oversized_partial_union_retains_content_and_its_section_provenance(session):
    product, _ = initial(session)
    now = datetime.now(timezone.utc)
    initial_parameters = [{"name": f"Parameter {index}", "value": "Original"} for index in range(200)]
    save(session, product, parse_product_content({"specifications": initial_parameters}), now)
    later = parse_product_content({"specifications": [{"name": "Extra parameter", "value": "New"}]})
    result = save(session, product, later, now + timedelta(seconds=1))
    row = session.scalar(select(ProductContent))
    assert not result["content_saved"] and result["supplement_limits_reached"]
    assert len(row.sections["specifications"]) == 200
    provenance = content_observation_metadata(row)
    assert provenance["section_observations"]["specifications"]["completeness"] == "complete"
    assert provenance["section_observations"]["specifications"]["observed_at"] == now.isoformat()
    assert provenance["latest_observation"]["observed_at"] == (now + timedelta(seconds=1)).isoformat()
