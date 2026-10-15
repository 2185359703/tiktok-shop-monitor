import asyncio
import json
from copy import deepcopy
from datetime import datetime, timezone

import httpx
import pytest

from app.crawlers.product_reviews import AsyncReviewClient, parse_review_data, parse_review_document, parse_review_response, review_time
from app.crawlers.public_shop import PublicPageError


PID, SID = "1729437431865054197", "7495552971774921097"
URL = f"https://shop.tiktok.com/us/pdp/{PID}"


def review_data(*, more=True, count=5, start=1):
    return {"has_more": more, "total_reviews": "38" if more else str(count),
            "product_reviews": [{"review_id": str(i), "product_id": PID, "sku_id": "123456",
                "review_rating": 5, "review_time": "1780183005374", "review_text": "Good fit",
                "reviewer_name": "A***", "sku_specification": "Black, M", "review_country": "US",
                "is_verified_purchase": True, "is_incentivized_review": False,
                "review_images": ["https://example.com/photo.jpg", "javascript:alert(1)"]}
                for i in range(start, start + count)]}


def document(data=None):
    block = {"error_code": 0, "product_info": {"product_id": PID, "seller_id": SID},
             "shop_info": {"seller_id": SID, "region": "US"}, "review_info": data if data is not None else review_data()}
    root = {"loaderData": {"product/page": {"route_info": {"path_region": "US"}, "page_config": {
        "components_map": {"product": {"component_type": "product_info", "component_data": block}}}}}}
    return '<script id="__MODERN_ROUTER_DATA__" type="application/json">' + json.dumps(root) + '</script>'


def test_public_projection_preserves_time_and_does_not_copy_account_fields():
    data = review_data()
    data["product_reviews"][0].update(reviewer_id="private-user", reviewer_avatar_url="https://example.com/avatar")
    page = parse_review_document(document(data), URL, SID)
    row = page.reviews[0]
    assert row.reviewed_at == datetime(2026, 5, 30, 23, 16, 45, 374000, tzinfo=timezone.utc)
    assert row.images == ("https://example.com/photo.jpg",)
    assert page.has_more and page.total == 38 and page.page_size == 5
    assert not hasattr(row, "reviewer_id") and len(row.content_hash) == 64


@pytest.mark.parametrize("value", [None, "", "0", 0])
def test_missing_source_date_stays_null(value):
    assert review_time(value) is None


@pytest.mark.parametrize("mutation", [
    lambda d: d["product_reviews"][0].update(product_id="999"),
    lambda d: d["product_reviews"][0].update(review_rating=True),
    lambda d: d["product_reviews"][0].update(review_rating=6),
    lambda d: d.update(has_more="false"),
    lambda d: d.update(product_reviews=[]),
    lambda d: d.pop("product_reviews"),
])
def test_wrong_identity_and_missing_or_malformed_records_fail(mutation):
    data = review_data()
    mutation(data)
    with pytest.raises(PublicPageError):
        parse_review_data(data, PID, page_number=1, page_size=5, source_kind="document")


def test_empty_requires_explicit_zero_and_exhaustion():
    page = parse_review_document(document(review_data(more=False, count=0)), URL, SID)
    assert not page.has_more and not page.reviews and page.total == 0
    with pytest.raises(PublicPageError):
        parse_review_document(document(), URL, "999")
    with pytest.raises(PublicPageError):
        parse_review_document('<title>Security Check</title>', URL, SID)


@pytest.mark.parametrize("body", [b'{"code":10000}', b'{"code":100000}', b'{"code":0}', b'<html>blocked</html>'])
def test_business_errors_are_not_empty_reviews(body):
    with pytest.raises(PublicPageError) as result:
        parse_review_response(body, PID, 2)
    assert result.value.retryable


def test_duplicate_id_is_deduplicated_but_conflicting_content_rejected():
    data = review_data(more=False, count=1)
    data["product_reviews"].append(deepcopy(data["product_reviews"][0]))
    assert len(parse_review_data(data, PID, page_number=1, page_size=5, source_kind="api").reviews) == 1
    data["product_reviews"][1]["review_text"] = "Conflict"
    with pytest.raises(PublicPageError):
        parse_review_data(data, PID, page_number=1, page_size=5, source_kind="api")


@pytest.mark.parametrize("count", [0, 2])
def test_empty_or_short_page_cannot_claim_exhaustion_before_reported_total(count):
    data = review_data(more=False, count=count, start=6)
    data["total_reviews"] = "38"
    with pytest.raises(PublicPageError):
        parse_review_data(data, PID, page_number=2, page_size=5, source_kind="api")


def test_async_client_bootstraps_anonymous_session_and_posts_real_pagination():
    async def run():
        calls, gates = [], []
        async def gate():
            gates.append(True)
            await asyncio.sleep(0)
        async def route(request):
            calls.append(request)
            if request.method == "GET":
                return httpx.Response(200, text=document())
            body = json.loads(request.content)
            assert body["page_start"] == 2 and body["page_size"] == 5 and body["sort_rule"] == 1
            assert body["review_filter"] == {"filter_type": 1, "filter_value": 6}
            return httpx.Response(200, json={"code": 0, "data": review_data(more=False, count=2, start=6)})
        async with AsyncReviewClient(backend="httpx", transport=httpx.MockTransport(route), before_request=gate) as client:
            result = await client.fetch_page(URL, SID, page_number=2)
        assert len(calls) == len(gates) == 2
        assert len(result.page.reviews) == 2 and not result.page.has_more
    asyncio.run(run())


def test_redirect_cannot_switch_product_before_requesting_new_location():
    async def run():
        calls = []
        def route(request):
            calls.append(request)
            return httpx.Response(302, headers={"location": "https://shop.tiktok.com/us/pdp/999"})
        async with AsyncReviewClient(backend="httpx", transport=httpx.MockTransport(route)) as client:
            with pytest.raises(PublicPageError):
                await client.fetch_page(URL, SID)
        assert len(calls) == 1
    asyncio.run(run())
