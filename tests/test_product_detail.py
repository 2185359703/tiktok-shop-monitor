import json
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from app.crawlers.product_detail import DetailSnapshot, money, parse_detail_block, parse_product_detail, ProductDetailClient
from app.crawlers.public_shop import PublicPageError
from app.models import StockStatus


PID, SID = "1729415858004005193", "7495434645551745353"
URL = f"https://shop.tiktok.com/us/pdp/cardigan/{PID}"


def block():
    def sku(sku_id, size_id, size, quantity, button):
        return {"sku_id": sku_id, "stock": quantity, "status": 0, "pdp_button_area_id": button,
                "sku_sale_props": [{"prop_id": "100000", "prop_name": "Color", "prop_value_id": "111", "prop_value": "Apricort"},
                                   {"prop_id": "100007", "prop_name": "Size", "prop_value_id": size_id, "prop_value": size}],
                "price": {"currency_name": "USD", "sale_price_decimal": "81.89"}}
    return {"error_code": 0, "shop_info": {"seller_id": SID, "region": "US"},
            "categories": [{"category_id": "900624", "category_name": "Women's Cardigans"}],
            "product_info": {"product_id": PID, "seller_id": SID, "status": 1, "title": "ANRABESS Cardigan",
                "sold_count": "7636", "sold_count_note": {"text": "Items sold globally excluding returned items."},
                "review": {"product_rating": 4.7, "review_count": "414"},
                "price": {"currency": "USD", "min_sku_price": "81.89", "is_interval_price": False, "real_price": "$81.89"},
                "specifications": [{"name": "Brand", "value": "ANRABESS"}, {"name": "Material", "value": "50% viscose + 30% nylon + 20% PBT"},
                                   {"name": "Material", "value": "50% Viscose, 30% Nylon, 20% Polyester"}],
                "desc_blocks": [{"type": "text", "text": "<p>Merchant description</p><script>do-not-save-secret</script>"}],
                "sale_props": [{"prop_id": "100000", "prop_name": "Color", "sale_prop_values": [{"prop_value_id": "111", "prop_value": "Apricort"}]},
                               {"prop_id": "100007", "prop_name": "Size", "sale_prop_values": [{"prop_value_id": "222", "prop_value": "Small"}, {"prop_value_id": "333", "prop_value": "Large"}]}],
                "skus": [sku("1729415858004070729", "222", "Small", 8, "default"), sku("1729415858004136265", "333", "Large", 0, "sold_out")]}}


def html(value=None):
    data = {"loaderData": {"(region)/pdp/(slug)/(product_id)/page": {"route_info": {"path_region": "us"},
                             "page_config": {"components_map": [{"component_type": "product_info", "component_data": value or block()}]}}}}
    # Embedded JSON must escape closing tags, as real SSR serializers do.
    return '<script id="__MODERN_ROUTER_DATA__">' + json.dumps(data).replace("</", "<\\/") + "</script>"


def snapshot(value=None, captured_at=None):
    now = captured_at or datetime.now(timezone.utc)
    return DetailSnapshot(URL, URL, 200, 100, "a" * 64, 10, now, now, parse_detail_block(value or block(), URL, SID))


def test_public_fields_material_conflict_and_exact_sku_options():
    detail = parse_product_detail(html(), URL, SID)
    assert detail.attributes["material"] is None and "material" in detail.raw_attributes["attribute_conflicts"]
    assert detail.description == "Merchant description" and detail.attributes["brand"] == "ANRABESS"
    assert detail.price_min == detail.price_max == Decimal("81.89") and detail.original_price_min is None
    assert detail.color_summary == ["Apricort"] and detail.size_summary == ["Large", "Small"]
    assert detail.stock_status == StockStatus.PARTIAL and len(detail.variants) == 2
    assert detail.raw_attributes["metric_scopes"]["sales_count"] == "global_cumulative_excluding_returned"


def test_oversized_optional_attributes_keep_original_evidence_and_do_not_discard_skus():
    value = block()
    long_style = ("public merchant style " * 8).strip()
    value["product_info"]["specifications"].append({"name": "Style", "value": long_style})
    detail = parse_detail_block(value, URL, SID)
    assert detail.attributes["style"] is None and len(detail.variants) == 2
    assert detail.raw_attributes["normalization_issues"]["style"]["actual_length"] == len(long_style)
    assert next(record for record in detail.raw_attributes["specifications"] if record["name"] == "Style")["value"] == long_style


def test_sampled_general_size_label_keeps_exact_property_ids_and_values():
    value = block()
    value["product_info"]["sale_props"][1]["prop_name"] = "Size(General size)"
    detail = parse_detail_block(value, URL, SID)
    assert detail.size_summary == ["Large", "Small"]
    assert detail.variants[0].options["Size"] == "Small"
    value["product_info"]["skus"][0]["sku_sale_props"][1]["prop_value"] = "Wrong label"
    with pytest.raises(PublicPageError, match="公开详情") as error:
        parse_detail_block(value, URL, SID)
    assert error.value.code == "sku_property_identity_conflict"


@pytest.mark.parametrize("amount,expected", [("9,880.00", "9880.00"), ("1,234,567.89", "1234567.89"),
                                             ("9,880", "9880.00")])
def test_us_grouped_amounts_keep_the_source_value(amount, expected):
    assert money(amount) == Decimal(expected)


@pytest.mark.parametrize("amount", ["9,88.00", "1,23,456.00", "9.880,00", "9,880.001", "$9,880.00", "-9,880.00"])
def test_malformed_grouping_currency_symbols_and_excess_precision_still_fail(amount):
    with pytest.raises(PublicPageError):
        money(amount)


@pytest.mark.parametrize("mutate,code", [
    (lambda value: value["product_info"].update(product_id=float(PID)), "invalid_product_identifier"),
    (lambda value: value["shop_info"].update(region="GB"), "product_detail_identity_conflict"),
    (lambda value: value["product_info"].update(seller_id="111"), "product_detail_identity_conflict"),
    (lambda value: value.update(error_code=7), "product_detail_business_error"),
    (lambda value: value["product_info"].update(skus=[]), "missing_product_skus"),
    (lambda value: value["product_info"]["skus"][0]["price"].update(currency_name="GBP"), "sku_currency_mismatch"),
    (lambda value: value["product_info"]["skus"][0]["sku_sale_props"][0].update(prop_value_id="999"), "sku_property_identity_conflict"),
    (lambda value: value["product_info"]["skus"].append(deepcopy(value["product_info"]["skus"][0])), "duplicate_sku_identifier"),
    (lambda value: value["product_info"]["skus"][0].update(stock=True), "invalid_public_count"),
])
def test_bad_identity_sku_or_currency_is_not_success(mutate, code):
    value = block()
    mutate(value)
    with pytest.raises(PublicPageError) as error: parse_product_detail(html(value), URL, SID)
    assert error.value.code == code


def test_unknown_stock_not_assumed_and_merchant_label_is_not_ai_summary():
    value = block()
    value["product_info"]["specifications"] = []
    value["product_info"]["desc_blocks"] = [{"type": "text", "text": "Fabric type: 100% Cotton"}]
    value["aigc_info"] = {"long_text": "Material: wool do-not-save-secret"}
    value["review_info"] = {"product_reviews": [{"name": "do-not-save-secret"}]}
    value["product_info"]["skus"][0]["stock"] = None
    detail = parse_product_detail(html(value), URL, SID)
    assert detail.attributes["material"] == "100% Cotton" and detail.stock_status == StockStatus.UNKNOWN
    assert detail.variants[0].available_quantity is None
    assert "do-not-save-secret" not in json.dumps(detail.raw_attributes)


def test_source_range_and_missing_original_max_remain_honest():
    value = block()
    value["product_info"]["price"].update(is_interval_price=True, min_sku_price="32.99", max_sku_price="42.25", min_sku_original_price="39.98")
    detail = parse_product_detail(html(value), URL, SID)
    assert detail.price_max == Decimal("42.25") and detail.original_price_max is None


def test_http_uses_shared_gate_and_different_id_redirect_is_rejected():
    turns = []
    with ProductDetailClient(interval=0, before_request=lambda: turns.append(1),
                             transport=httpx.MockTransport(lambda request: httpx.Response(200, text=html()))) as client:
        result = client.fetch_detail(URL, SID)
    assert len(turns) == 1 and len(result.detail.variants) == 2
    def redirect(request):
        if str(request.url) == URL: return httpx.Response(302, headers={"Location": "https://shop.tiktok.com/us/pdp/wrong/111"})
        return httpx.Response(200, text=html())
    with ProductDetailClient(interval=0, transport=httpx.MockTransport(redirect)) as client:
        with pytest.raises(PublicPageError) as error: client.fetch_detail(URL, SID)
    assert error.value.code == "product_detail_redirect_conflict"


def test_evidence_does_not_save_reviews_aigc_shipping_or_warehouse(tmp_path):
    value = block()
    value["review_info"] = {"product_reviews": ["do-not-save-secret"]}
    value["aigc_info"] = {"long_text": "do-not-save-secret"}
    value["product_info"]["skus"][0]["warehouse_id"] = "do-not-save-secret"
    record = snapshot(value).save_evidence(tmp_path).read_text(encoding="utf-8")
    assert "do-not-save-secret" not in record and "warehouse_id" not in record
