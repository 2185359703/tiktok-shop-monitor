import json
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from app.crawlers.product_list import component_projection, parse_feed_data, parse_product_page_json, parse_store_products, ProductPageClient, ProductPageSnapshot, PublicPagination, save_product_failure
from app.crawlers.public_shop import PublicPageError


SHOP_ID = "7495434645551745353"
PRODUCT_ID = "1729591769491607881"
URL = f"https://shop.tiktok.com/us/store/anrabess/{SHOP_ID}"


def product(identifier=PRODUCT_ID):
    return {"product_id": identifier, "title": "ANRABESS Women Long Sleeve Rib Knit Shirt",
            "seller_info": {"seller_id": SHOP_ID, "shop_name": "ANRABESS"},
            "seo_url": {"canonical_url": f"https://shop.tiktok.com/us/pdp/rib-knit-shirt/{identifier}"},
            "product_price_info": {"currency_name": "USD", "sale_price_decimal": "27.99",
                                   "origin_price_decimal": "40.29", "discount_decimal": "0.31", "price_prefix": "From"},
            "rate_info": {"score": 4.7, "review_count": "3541"}, "sold_info": {"sold_count": 52084},
            "brand_info": {"brand_name": "ANRABESS"}, "sku_info": [{"SkuId": "999", "PriceInfo": {}}]}


def component():
    return {"component_type": "feed_list", "component_name": "feed_list_store_list_product",
            "fe_config": {"is_main_feed": True, "is_fmp_significant": True, "show_structure_data": True,
                          "show_empty_page": True, "data_source": {"type": "store_list_product", "params": {"count": 20, "clamp": False}}}}


def page_data(items=None, has_more=True, cursor="public-cursor-page-2"):
    return {"products": items if items is not None else [product()], "has_more": has_more,
            "load_more_params": {"search_params": cursor}}


def store_html(items=None, has_more=True):
    feed = component()
    feed["component_data"] = page_data(items, has_more)
    root = {"loaderData": {"(region)/store/(route_keyword)/(route_keyword_2)/page": {
        "route_info": {"path_region": "us"}, "page_config": {"components_map": [
            {"component_type": "shop_info", "component_data": {"shopInfo": {
                "seller_id": SHOP_ID, "shop_name": "ANRABESS", "region": "US", "on_sell_product_count": 100}}}, feed]}}}}
    return '<title>ANRABESS</title><script id="__MODERN_ROUTER_DATA__">' + json.dumps(root) + "</script>"


def snapshot(items=None, has_more=True, captured_at=None):
    now = captured_at or datetime.now(timezone.utc)
    return ProductPageSnapshot(URL, URL, 200, 100, "a" * 64, 10, now, now, parse_store_products(store_html(items, has_more), URL))


def test_real_field_mapping_and_from_price_is_not_an_exact_range():
    feed = parse_store_products(store_html(), URL)
    item = feed.products[0]
    assert item.platform_product_id == PRODUCT_ID and item.platform_shop_id == SHOP_ID
    assert item.price_min == Decimal("27.99") and item.price_max is None
    assert item.original_price_min == Decimal("40.29") and item.original_price_max is None
    assert item.rating == Decimal("4.7") and item.review_count == 3541 and item.sales_count == 52084
    assert feed.next_pagination.seller_id == SHOP_ID and feed.total_product_count == 100


def test_public_canonical_urls_without_a_slug_preserve_product_identity():
    row = product()
    row["seo_url"]["canonical_url"] = f"https://shop.tiktok.com/us/pdp/{PRODUCT_ID}"
    feed = parse_store_products(store_html([row]), URL)
    assert feed.products[0].platform_product_id == PRODUCT_ID
    assert feed.products[0].product_url == row["seo_url"]["canonical_url"]
    row["seo_url"]["canonical_url"] = "https://shop.tiktok.com/us/pdp/111"
    with pytest.raises(PublicPageError) as error:
        parse_store_products(store_html([row]), URL)
    assert error.value.code == "product_url_identity_mismatch"


@pytest.mark.parametrize("mutation,code", [
    (lambda row: row.update(product_id=float(PRODUCT_ID)), "invalid_product_identifier"),
    (lambda row: row["seller_info"].update(seller_id="111"), "product_owner_mismatch"),
    (lambda row: row["seo_url"].update(canonical_url="https://shop.tiktok.com/us/pdp/wrong/111"), "product_url_identity_mismatch"),
    (lambda row: row["product_price_info"].update(currency_name="GBP"), "product_currency_mismatch"),
    (lambda row: row["product_price_info"].update(sale_price_decimal="27.999"), "invalid_price"),
    (lambda row: row["product_price_info"].update(discount_decimal="1.1"), "invalid_discount"),
    (lambda row: row["rate_info"].update(score=5.2), "invalid_rating"),
    (lambda row: row.update(title="x" * 513), "invalid_product_text"),
])
def test_invalid_identity_currency_or_fields_fail_closed(mutation, code):
    row = product()
    mutation(row)
    with pytest.raises(PublicPageError) as error:
        parse_store_products(store_html([row]), URL)
    assert error.value.code == code


def test_missing_prices_and_abbreviated_sales_remain_unknown():
    row = product()
    row["product_price_info"] = {}
    row["rate_info"] = {}
    row["sold_info"] = {"sold_count": "52.1K+"}
    candidate = parse_store_products(store_html([row]), URL).products[0]
    assert candidate.price_min is None and candidate.rating is None and candidate.review_count is None
    assert candidate.sales_count is None and candidate.sales_count_text == "52.1K+"


def test_pagination_must_progress_and_empty_more_is_not_success():
    first = parse_store_products(store_html(), URL)
    pagination = first.next_pagination
    second = product("1730866978455458121")
    payload = {"code": 0, "data": page_data([second], True, "public-cursor-page-3")}
    result = parse_product_page_json(json.dumps(payload).encode(), pagination)
    assert result.next_pagination.excluded_product_ids == (PRODUCT_ID, second["product_id"])
    for bad in (page_data([], True), page_data([product()], True), page_data([second], True, pagination.search_params)):
        with pytest.raises(PublicPageError) as error:
            parse_product_page_json(json.dumps({"code": 0, "data": bad}).encode(), pagination)
        assert error.value.code == "pagination_not_progressing"
    assert parse_product_page_json(json.dumps({"code": 0, "data": page_data([], False)}).encode(), pagination).has_more is False


def test_duplicate_products_do_not_multiply_records_but_conflicting_duplicates_fail():
    row = product()
    feed = parse_store_products(store_html([row, deepcopy(row)]), URL)
    assert len(feed.products) == 1 and feed.duplicate_products == 1
    different = deepcopy(row)
    different["title"] = "Different title"
    with pytest.raises(PublicPageError) as error:
        parse_store_products(store_html([row, different]), URL)
    assert error.value.code == "duplicate_product_conflict"


def test_api_is_stateless_unsigned_and_every_request_uses_gate():
    pagination = parse_store_products(store_html(), URL).next_pagination
    requests, turns = [], []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"code": 0, "data": page_data([product("1730866978455458121")], False)})
    with ProductPageClient(interval=0, max_attempts=1, before_request=lambda: turns.append(1),
                           transport=httpx.MockTransport(respond)) as client:
        page = client.fetch_next(pagination)
    assert not page.feed.has_more and len(turns) == 1
    request = requests[0]
    assert request.method == "POST" and not request.url.query and "cookie" not in request.headers
    params = json.loads(json.loads(request.content)["load_more_params"])
    assert params["offset"] == 0 and params["seller_id"] == SHOP_ID and params["exclude_product_ids"] == [PRODUCT_ID]


@pytest.mark.parametrize("status", [401, 403, 429, 503, 302])
def test_page_http_errors_do_not_become_empty_results(status):
    pagination = parse_store_products(store_html(), URL).next_pagination
    calls = []
    def respond(request):
        calls.append(1)
        return httpx.Response(status, headers={"Location": "https://example.com"})
    with ProductPageClient(interval=0, transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(PublicPageError) as error:
            client.fetch_next(pagination)
    assert error.value.status == status and len(calls) == 1
    assert error.value.retryable == (status >= 500)


def test_projection_never_contains_layout_cookie_or_sku_private_data(tmp_path):
    row = product()
    row["cookie"] = "do-not-save-secret"
    row["sku_info"][0]["private"] = "do-not-save-secret"
    page = snapshot([row])
    output = page.save_evidence(tmp_path).read_text(encoding="utf-8")
    assert "do-not-save-secret" not in output and "cookie" not in output and "sku_info" not in output
    assert json.loads(output)["parser_version"] == "public_store_feed_v1"


@pytest.mark.parametrize("body", [b'<title>Security Check</title>', b'{"code":false,"data":{}}', b'{"code":9,"message":"secret"}', b'not-json'])
def test_challenges_and_bad_business_codes_are_rejected(body):
    pagination = parse_store_products(store_html(), URL).next_pagination
    with pytest.raises(PublicPageError):
        parse_product_page_json(body, pagination)


def test_failure_evidence_has_hash_and_status_but_no_response_secrets(tmp_path):
    pagination = parse_store_products(store_html(), URL).next_pagination
    response_body = b'{"code":9,"message":"do-not-save-response-secret"}'
    with ProductPageClient(interval=0, transport=httpx.MockTransport(lambda request: httpx.Response(200, content=response_body))) as client:
        with pytest.raises(PublicPageError) as error:
            client.fetch_next(pagination)
    path = save_product_failure(error.value, tmp_path)
    text = path.read_text(encoding="utf-8")
    report = json.loads(text)
    assert "do-not-save-response-secret" not in text
    assert report["response_status"] == 200 and len(report["response_hash"]) == 64
    assert report["response_bytes"] == len(response_body)
