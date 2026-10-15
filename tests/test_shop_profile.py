import json
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import httpx
import pytest

from app.crawlers.public_shop import PublicPageError
from app.crawlers.shop_profile import (
    PARSER_VERSION, PROFILE_VALUE_FIELDS, ShopProfileClient, ShopProfilePageSnapshot, parse_shop_profile,
)


SHOP_ID = "7495552971774921097"
URL = f"https://shop.tiktok.com/us/store/chouyatou/{SHOP_ID}"
ROUTE = "(region)/store/(route_keyword)/(route_keyword_2)/page"
IMAGE = "https://p16-oec-general-useast5.ttcdn-us.com/store-logo.webp"


def profile_info():
    return {
        "seller_id": SHOP_ID, "shop_name": "CHOUYATOU", "region": "US",
        "shop_logo": {"url_list": [IMAGE], "width": 300, "height": 300},
        "store_brand_show": {"brand_type": 3, "label_type_str": "blue_v"},
        "shop_identity_label": {"ShopIdentityLabelKey": "official", "identity_label_text": "OFFICIAL SHOP"},
        "followers_count": "34872", "format_followers_count": "34.8K+",
        "sold_count": 498223, "format_sold_count": "498.2K",
        "global_sold_count": "498223", "format_global_sold_count": "498.2K",
        "video_count": "1391", "format_video_count": "1391",
        "on_sell_product_count": 205, "display_on_sell_product_count": "205",
        "shop_rating": "4.5", "review_count": 36124,
    }


def router(info=None, *, mapping=True):
    components = [{"component_type": "shop_info", "component_data": {"shopInfo": info if info is not None else profile_info()}}]
    return {"loaderData": {
        "layout": {"private_token": "must-not-save-router-private-data"},
        ROUTE: {"route_info": {"path_region": "us"}, "page_config": {
            "components_map": {"dynamic-header-7": components[0]} if mapping else components}},
    }}


def html(root):
    return '<title>CHOUYATOU</title><script id="__MODERN_ROUTER_DATA__">' + json.dumps(root) + "</script>"


@pytest.mark.parametrize("mapping", [True, False])
def test_header_is_independent_of_feed_and_preserves_source_precision(mapping):
    result = parse_shop_profile(html(router(mapping=mapping)), URL + "?tracking=must-not-save")
    assert result.platform_shop_id == SHOP_ID and result.canonical_url == URL and result.region == "US"
    assert result.canonical_name == "CHOUYATOU" and result.observation_status == "complete"
    assert tuple(result.fields) == PROFILE_VALUE_FIELDS
    assert result.fields == {
        "logo_url": IMAGE, "verification_type": "blue_v", "identity_label_key": "official",
        "identity_label_text": "OFFICIAL SHOP", "follower_count": 34872, "follower_count_text": "34.8K+",
        "sales_count": 498223, "sales_count_text": "498.2K", "global_sales_count": 498223,
        "global_sales_count_text": "498.2K", "video_count": 1391, "video_count_text": "1391",
        "product_count": 205, "product_count_text": "205", "rating": Decimal("4.5"), "review_count": 36124,
    }
    assert result.field_state["follower_count"]["state"] == "exact"
    assert result.field_state["sales_count"]["scope"] != result.field_state["global_sales_count"]["scope"]
    assert result.field_state["video_count"]["source"].endswith(".shopInfo.video_count")


def test_documented_public_projection_maps_without_private_router_fixture():
    record = json.loads((Path(__file__).resolve().parents[1] / "docs/evidence/shop-profile-20261015.json").read_text(encoding="utf-8"))
    info = deepcopy(record["public_source_check"]["fields"])
    info["shop_logo"]["url_list"] = info["shop_logo"].pop("public_image_urls")
    parsed = parse_shop_profile(html(router(info)), URL)
    assert parsed.fields["follower_count"] == 34872 and parsed.fields["video_count"] == 1391
    assert parsed.fields["sales_count"] == 498223 and parsed.observation_status == "complete"


@pytest.mark.parametrize("raw,display,number,text,state", [
    (None, None, None, None, "not_provided"),
    ("", "", None, None, "not_provided"),
    (None, "34.8K+", None, "34.8K+", "display_only"),
    ("34.8K+", None, None, "34.8K+", "display_only"),
    ("120+", None, None, "120+", "display_only"),
    (0, "0", 0, "0", "exact"),
    ("0", None, 0, None, "exact"),
    (2 ** 64 - 1, None, 2 ** 64 - 1, None, "exact"),
    (str(2 ** 64 - 1), None, 2 ** 64 - 1, None, "exact"),
    (2 ** 64, "18.4B+", None, "18.4B+", "invalid"),
    (str(2 ** 64), None, None, None, "invalid"),
    (True, "1", None, "1", "invalid"),
    (False, None, None, None, "invalid"),
    (1.0, None, None, None, "invalid"),
    (-1, None, None, None, "invalid"),
    ("-1", None, None, None, "invalid"),
    ("unknown", None, None, None, "invalid"),
    ([], None, None, None, "invalid"),
])
def test_counts_distinguish_exact_zero_missing_abbreviation_and_invalid(raw, display, number, text, state):
    info = profile_info()
    info.update(followers_count=raw, format_followers_count=display)
    parsed = parse_shop_profile(html(router(info)), URL)
    assert parsed.fields["follower_count"] == number and parsed.fields["follower_count_text"] == text
    assert parsed.field_state["follower_count"]["state"] == state
    assert parsed.observation_status == ("complete" if state in {"exact", "display_only"} else "partial")


def test_invalid_display_preserves_valid_exact_count_but_marks_partial():
    info = profile_info()
    info["format_followers_count"] = {"private": "do-not-save"}
    parsed = parse_shop_profile(html(router(info)), URL)
    assert parsed.fields["follower_count"] == 34872 and parsed.fields["follower_count_text"] is None
    assert parsed.field_state["follower_count"]["display_state"] == "invalid"
    assert parsed.observation_status == "partial"


@pytest.mark.parametrize("badge", [None, {}, ""])
def test_no_badges_is_a_checked_absence_not_an_unverified_boolean(badge):
    info = profile_info()
    info.update(store_brand_show=badge, shop_identity_label=badge, official_creator_uid="123")
    parsed = parse_shop_profile(html(router(info)), URL)
    for key in ("verification_type", "identity_label_key", "identity_label_text"):
        assert parsed.fields[key] is None and parsed.field_state[key]["state"] == "not_displayed"
    assert parsed.observation_status == "complete"


@pytest.mark.parametrize("badge", [False, [], "official", {"label_type_str": True}])
def test_invalid_badge_is_not_treated_as_absent(badge):
    info = profile_info()
    info["store_brand_show"] = badge
    parsed = parse_shop_profile(html(router(info)), URL)
    assert parsed.fields["verification_type"] is None
    assert parsed.field_state["verification_type"]["state"] == "invalid"
    assert parsed.observation_status == "partial"


@pytest.mark.parametrize("logo,state", [
    (None, "not_provided"), ({}, "not_provided"), ({"url_list": []}, "not_provided"),
    (False, "invalid"), ({"url_list": "https://example.com/logo.jpg"}, "invalid"),
    ({"url_list": ["http://127.0.0.1/logo.jpg"]}, "invalid"),
    ({"url_list": ["https://user:secret@example.com/logo.jpg"]}, "invalid"),
])
def test_missing_or_unsafe_logo_does_not_become_a_downloadable_url(logo, state):
    info = profile_info()
    info["shop_logo"] = logo
    parsed = parse_shop_profile(html(router(info)), URL)
    assert parsed.fields["logo_url"] is None and parsed.field_state["logo_url"]["state"] == state
    assert parsed.observation_status == "partial"


@pytest.mark.parametrize("rating,expected,state", [(None, None, "not_provided"), (0, Decimal(0), "exact"),
    ("5.00", Decimal("5"), "exact"), (4.5, Decimal("4.5"), "exact"), (5.1, None, "invalid"),
    (True, None, "invalid"), ("NaN", None, "invalid"), ("4.555", None, "invalid")])
def test_rating_scale_is_bounded(rating, expected, state):
    info = profile_info()
    info["shop_rating"] = rating
    parsed = parse_shop_profile(html(router(info)), URL)
    assert parsed.fields["rating"] == expected and parsed.field_state["rating"]["state"] == state


@pytest.mark.parametrize("changes,code", [
    ({"seller_id": "111"}, "store_identity_mismatch"),
    ({"seller_id": float(SHOP_ID)}, "store_identity_mismatch"),
    ({"seller_id": True}, "store_identity_mismatch"),
    ({"region": "GB"}, "market_mismatch"),
    ({"region": None}, "market_mismatch"),
    ({"shop_name": ""}, "invalid_shop_name"),
])
def test_wrong_target_or_market_never_produces_a_profile(changes, code):
    info = profile_info()
    info.update(changes)
    with pytest.raises(PublicPageError) as error:
        parse_shop_profile(html(router(info)), URL)
    assert error.value.code == code


def test_unique_store_route_and_shop_component_are_required_without_hardcoded_index():
    root = router(mapping=False)
    component = root["loaderData"][ROUTE]["page_config"]["components_map"][0]
    root["loaderData"][ROUTE]["page_config"]["components_map"].append(deepcopy(component))
    with pytest.raises(PublicPageError) as error:
        parse_shop_profile(html(root), URL)
    assert error.value.code == "ambiguous_shop_info"
    root = router()
    root["loaderData"]["other/store/route/page"] = deepcopy(root["loaderData"][ROUTE])
    with pytest.raises(PublicPageError) as error:
        parse_shop_profile(html(root), URL)
    assert error.value.code == "ambiguous_store_page"
    root = router()
    root["loaderData"][ROUTE]["route_info"]["path_region"] = "gb"
    with pytest.raises(PublicPageError) as error:
        parse_shop_profile(html(root), URL)
    assert error.value.code == "market_mismatch"


@pytest.mark.parametrize("body", ["<title>Security Check</title>", "no router", '<script id="__MODERN_ROUTER_DATA__">false</script>',
    '<script id="__MODERN_ROUTER_DATA__">{}</script>', '<script id="__MODERN_ROUTER_DATA__">oops</script>'])
def test_challenge_and_malformed_pages_never_produce_empty_success(body):
    with pytest.raises(PublicPageError):
        parse_shop_profile(body, URL)


def test_evidence_contains_only_projection_and_public_source(tmp_path):
    root = router()
    info = root["loaderData"][ROUTE]["page_config"]["components_map"]["dynamic-header-7"]["component_data"]["shopInfo"]
    info.update(cookie="must-not-save-cookie", official_creator_uid="must-not-save-uid")
    body = html(root).encode()
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, content=body)
    with ShopProfileClient(interval=0, before_request=lambda: None, transport=httpx.MockTransport(respond)) as client:
        snapshot = client.fetch_profile(URL + "?session=must-not-save-session")
    text = snapshot.save_evidence(tmp_path).read_text(encoding="utf-8")
    saved = json.loads(text)
    assert "must-not-save" not in text and "official_creator_uid" not in text and "cookie" not in text
    assert saved["parser_version"] == PARSER_VERSION and saved["profile"]["fields"]["rating"] == "4.5"
    assert saved["response_hash"] == sha256(body).hexdigest() and saved["response_bytes"] == len(body)
    assert snapshot.network_attempts == 1 and snapshot.captured_at >= snapshot.started_at
    assert not calls[0].url.query and "cookie" not in calls[0].headers


@pytest.mark.parametrize("status", [401, 403, 429])
def test_auth_and_access_limits_do_not_retry_or_generate_success(status):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status)
    with ShopProfileClient(interval=0, transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(PublicPageError) as error:
            client.fetch_profile(URL)
    assert len(calls) == 1 and error.value.code == "access_limited"
    assert error.value.diagnostics["response_status"] == status


def test_transient_network_retries_use_gate_and_remain_bounded(monkeypatch):
    monkeypatch.setattr("app.crawlers.shop_profile.time.sleep", lambda _: None)
    calls, gates = [], []
    def respond(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) < 3 else 200, text=html(router()))
    with ShopProfileClient(interval=0, max_attempts=3, before_request=lambda: gates.append(1),
                           transport=httpx.MockTransport(respond)) as client:
        result = client.fetch_profile(URL)
    assert result.network_attempts == len(calls) == len(gates) == 3


def test_redirect_cannot_change_expected_store_identity():
    calls = []
    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(302, headers={"Location": "https://shop.tiktok.com/us/store/another/111"})
        info = profile_info()
        info["seller_id"] = "111"
        return httpx.Response(200, text=html(router(info)))
    with ShopProfileClient(interval=0, transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(PublicPageError) as error:
            client.fetch_profile(URL)
    assert error.value.code == "store_identity_mismatch" and len(calls) == 2
    assert error.value.diagnostics["response_status"] == 200


def test_snapshot_serializes_decimal_and_datetime(tmp_path):
    now = datetime(2026, 10, 15, 9, tzinfo=timezone.utc)
    profile = parse_shop_profile(html(router()), URL)
    record = ShopProfilePageSnapshot(URL, URL, 200, 123, "a" * 64, 10, 1, now, now, profile)
    saved = json.loads(record.save_evidence(tmp_path).read_text(encoding="utf-8"))
    assert saved["captured_at"] == now.isoformat() and saved["profile"]["fields"]["rating"] == "4.5"
