"""Meaningful relation persistence and scope tests on an isolated in-memory database."""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import DefaultClause, Integer, MetaData, String, create_engine, event, func, select, text
from sqlalchemy.dialects.mysql import BIGINT
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.routes_matching import router
from app.models import Base, Product, ProductVariant, Shop
from app.models.matching import MatchStatus, ProductMatch, SkuMatch
from app.services.matching_service import MatchingError, candidate_matches, confirm_match, matching_summary, review_match, serialize_match
from app.settings import Settings


NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


@pytest.fixture
def matching_engine():
    """Adapt physical MySQL column types only; exercise the actual mapped services."""
    metadata = MetaData()
    for name in ("shops", "products", "product_variants", "product_matches", "sku_matches"):
        table = Base.metadata.tables[name].to_metadata(metadata)
        for column in table.c:
            if isinstance(column.type, BIGINT):
                column.type = Integer()
            elif isinstance(column.type, String):
                column.type = String(column.type.length)
            if column.server_default is not None and "CURRENT_TIMESTAMP" in str(column.server_default.arg):
                column.server_default = DefaultClause(text("CURRENT_TIMESTAMP"))
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    @event.listens_for(engine, "connect")
    def enforce_fks(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")
    metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def matching_db(matching_engine):
    with Session(matching_engine, expire_on_commit=False) as session:
        session.add_all([Shop(id=1, platform="tiktok_shop", region="US", platform_shop_id="s1", canonical_name="Shop one"),
                         Shop(id=2, platform="tiktok_shop", region="GB", platform_shop_id="s2", canonical_name="Other market")])
        session.flush()
        for identifier, shop_id, region in ((10, 1, "US"), (20, 1, "US"), (30, 1, "US"), (40, 2, "GB")):
            session.add(Product(id=identifier, shop_id=shop_id, platform="tiktok_shop", region=region,
                                platform_product_id=str(identifier), title="ANRABESS Cotton Ribbed Cardigan", brand="ANRABESS",
                                normalized_category="cardigan", product_url="https://example.test/product/" + str(identifier),
                                last_success_at=NOW, currency="USD", pricing_context="public_listed",
                                color_summary=["Black"], size_summary=["S"], raw_attributes={"observed_sku_ids": ["sku-" + str(identifier)]}))
        session.flush()
        for identifier, product_id, price in ((101, 10, "10.00"), (102, 20, "12.00"), (103, 30, "14.00")):
            session.add(ProductVariant(id=identifier, product_id=product_id, variant_key=str(identifier).zfill(64),
                                       platform_variant_id="sku-" + str(product_id), color="Black", size="S",
                                       options={"Color": "Black", "Size": "S"}, currency="USD", price=Decimal(price)))
        session.commit()
        yield session


def sku_body(**changes):
    return {"left_product_id": 10, "right_product_id": 20, "match_type": "same_sku", "left_variant_id": 101, "right_variant_id": 102, **changes}


def test_reversed_product_and_sku_pairs_are_idempotent_and_survive_new_session(matching_db, matching_engine):
    first, sku, created = confirm_match(matching_db, Settings(), **sku_body())
    assert created
    repeated, same_sku, created = confirm_match(matching_db, Settings(), **sku_body(left_product_id=20, right_product_id=10, left_variant_id=102, right_variant_id=101))
    assert not created and repeated.id == first.id and same_sku.id == sku.id
    matching_db.commit()
    with Session(matching_engine) as fresh:
        persisted = fresh.get(ProductMatch, first.id)
        assert persisted.status == MatchStatus.CONFIRMED
        assert fresh.scalar(select(func.count()).select_from(ProductMatch)) == 1
        assert fresh.scalar(select(func.count()).select_from(SkuMatch)) == 1
        comparison = serialize_match(fresh, Settings(), persisted)["sku_matches"][0]["comparison"]
        assert comparison["comparable"] and comparison["difference_right_minus_left"] == "2.00"


def test_suggestions_never_write_or_count_as_confirmed(matching_db):
    response = candidate_matches(matching_db, Settings(), product_id=10)
    assert response["items"] and all(row["requires_confirmation"] for row in response["items"])
    assert response["items"][0]["evidence"]["title"]["shared_tokens"]
    assert matching_db.scalar(select(func.count()).select_from(ProductMatch)) == 0
    summary = matching_summary(matching_db, Settings())
    assert summary["confirmed_product_pairs"] == 0 and summary["confirmed_sku_pairs"] == 0
    assert summary["unconfirmed_candidates"] > 0
    assert all("40" not in (row["left_product_id"], row["right_product_id"]) for row in response["items"])


def test_database_pair_and_sku_ownership_constraints_protect_other_write_paths(matching_db):
    match, _, _ = confirm_match(matching_db, Settings(), left_product_id=10, right_product_id=20)
    for left, right in ((10, 10), (20, 10), (10, 20)):
        with pytest.raises(IntegrityError), matching_db.begin_nested():
            matching_db.add(ProductMatch(platform="tiktok_shop", region="US", left_product_id=left, right_product_id=right,
                                         status=MatchStatus.CONFIRMED, source="manual_confirmation", rule_version="test", evidence={}, reviewed_at=NOW))
            matching_db.flush()
    with pytest.raises(IntegrityError), matching_db.begin_nested():
        matching_db.add(SkuMatch(product_match_id=match.id, left_product_id=10, right_product_id=20,
                                left_variant_id=103, right_variant_id=102, status=MatchStatus.CONFIRMED,
                                source="manual_confirmation", rule_version="test", evidence={}, reviewed_at=NOW))
        matching_db.flush()


@pytest.mark.parametrize("changes,code", [
    ({"left_product_id": 10, "right_product_id": 10}, "cannot_match_product_to_itself"),
    ({"right_product_id": 40}, "product_not_found_in_market"),
    ({"right_variant_id": 103}, "variant_does_not_belong_to_product"),
    ({"left_variant_id": 101, "right_variant_id": 101}, "two_distinct_variant_ids_required"),
])
def test_invalid_market_self_and_foreign_sku_are_rejected_without_relation(matching_db, changes, code):
    with pytest.raises(MatchingError) as error:
        confirm_match(matching_db, Settings(), **sku_body(**changes))
    assert error.value.code == code
    assert matching_db.scalar(select(func.count()).select_from(ProductMatch)) == 0


@pytest.mark.parametrize("column,value,reason", [
    ("size", None, "missing_size"),
    ("color", None, "missing_color"),
    ("size", "Small", "spec_values_differ"),
    ("options", {"Color": "Black", "Size": "S", "Pack": "2"}, "spec_keys_differ"),
    ("options", {"Color": "Black", "Size": "S", "Pack": None}, "missing_or_invalid_spec_value"),
])
def test_missing_or_nonexact_specs_do_not_become_same_sku(matching_db, column, value, reason):
    right = matching_db.get(ProductVariant, 102)
    if column in {"size", "color"}:
        right.options = {key: val for key, val in right.options.items() if key.lower() != column}
    setattr(right, column, value)
    with pytest.raises(MatchingError) as error:
        confirm_match(matching_db, Settings(), **sku_body())
    assert any(reason in item for item in error.value.reasons)
    assert matching_db.scalar(select(func.count()).select_from(SkuMatch)) == 0


@pytest.mark.parametrize("change,reason", [
    ("currency", "currency_mismatch"), ("pricing_context", "pricing_context_mismatch"),
    ("price", "exact_sku_price_missing"), ("options", "spec_values_differ"),
    ("latest", "right_sku_not_observed_in_latest_detail"),
])
def test_price_gap_is_withheld_when_current_specs_or_price_scope_change(matching_db, change, reason):
    match, _, _ = confirm_match(matching_db, Settings(), **sku_body())
    variant, product = matching_db.get(ProductVariant, 102), matching_db.get(Product, 20)
    if change == "currency": variant.currency = "GBP"
    if change == "pricing_context": product.pricing_context = "member_only"
    if change == "price": variant.price = None
    if change == "options": variant.size, variant.options = "M", {"Color": "Black", "Size": "M"}
    if change == "latest": product.raw_attributes = {"observed_sku_ids": []}
    matching_db.flush()
    comparison = serialize_match(matching_db, Settings(), match)["sku_matches"][0]["comparison"]
    assert not comparison["comparable"] and reason in comparison["reasons"]
    assert comparison["difference_right_minus_left"] is None


def test_parent_rejection_cancels_skus_and_parent_reconfirmation_requires_new_sku_review(matching_db):
    match, sku, _ = confirm_match(matching_db, Settings(), **sku_body())
    review_match(matching_db, Settings(), match.id, MatchStatus.REJECTED)
    assert match.status == sku.status == MatchStatus.REJECTED
    assert matching_summary(matching_db, Settings())["confirmed_sku_pairs"] == 0
    review_match(matching_db, Settings(), match.id, MatchStatus.CONFIRMED)
    assert sku.status == MatchStatus.REJECTED
    review_match(matching_db, Settings(), match.id, MatchStatus.CONFIRMED, sku_match_id=sku.id)
    assert matching_summary(matching_db, Settings())["confirmed_sku_pairs"] == 1


def test_candidates_exclude_reviewed_pairs_and_mark_bounded_scope(matching_db, monkeypatch):
    confirm_match(matching_db, Settings(), left_product_id=10, right_product_id=20)
    review_match(matching_db, Settings(), 1, MatchStatus.REJECTED)
    response = candidate_matches(matching_db, Settings(), product_id=10)
    assert not any({row["left_product_id"], row["right_product_id"]} == {"10", "20"} for row in response["items"])
    monkeypatch.setattr("app.services.matching_service.CANDIDATE_SCAN_LIMIT", 1)
    summary = matching_summary(matching_db, Settings())
    assert not summary["candidate_count_is_complete"] and summary["candidate_scan"]["bounded"]


def test_api_requires_existing_bearer_token_and_explicit_confirmation(matching_db, matching_engine):
    app = FastAPI()
    app.include_router(router)
    app.state.factory = sessionmaker(bind=matching_engine, expire_on_commit=False)
    app.state.settings = Settings()
    body = {"left_product_id": "10", "right_product_id": "20", "confirmed": True}
    with TestClient(app) as client:
        assert client.post("/api/v1/matches", json=body).status_code == 503
        app.state.settings = replace(Settings(), api_token="test-only-token-at-least-24-characters")
        assert client.post("/api/v1/matches", json=body).status_code == 401
        headers = {"Authorization": "Bearer test-only-token-at-least-24-characters"}
        assert client.post("/api/v1/matches", json={"left_product_id": 10, "right_product_id": 20}, headers=headers).status_code == 422
        assert client.post("/api/v1/matches", json={**body, "confirmed": False}, headers=headers).status_code == 422
        saved = client.post("/api/v1/matches", json=body, headers=headers)
        assert saved.status_code == 200 and saved.json()["created"]
        repeated = client.post("/api/v1/matches", json=body, headers=headers)
        assert repeated.status_code == 200 and not repeated.json()["created"]
        assert len(client.get("/api/v1/products/10/matches").json()["items"]) == 1
        assert client.patch("/api/v1/matches/1", json={"status": "rejected"}).status_code == 401
        assert client.patch("/api/v1/matches/1", json={"status": "rejected"}, headers=headers).status_code == 200
        assert client.get("/api/v1/matches/summary").json()["confirmed_product_pairs"] == 0
