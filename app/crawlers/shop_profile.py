"""Read public storefront identity and metrics independently of product feeds."""

import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path

import httpx

from app.crawlers.product_images import first_url
from app.crawlers.product_list import annotate_failure
from app.crawlers.public_shop import PublicPageError, PublicShopClient, RouterScriptParser, public_url, shop_id_from_url


PARSER_VERSION = "shop_profile_ssr_1"
PROFILE_VALUE_FIELDS = (
    "logo_url", "verification_type", "identity_label_key", "identity_label_text",
    "follower_count", "follower_count_text", "sales_count", "sales_count_text",
    "global_sales_count", "global_sales_count_text", "video_count", "video_count_text",
    "product_count", "product_count_text", "rating", "review_count",
)
MAX_UNSIGNED_BIGINT = 2 ** 64 - 1
_COUNTS = (
    ("follower_count", "followers_count", "format_followers_count", "storefront_followers"),
    ("sales_count", "sold_count", "format_sold_count", "storefront_sold_count_geography_unspecified"),
    ("global_sales_count", "global_sold_count", "format_global_sold_count", "source_labeled_global_sold_count"),
    ("video_count", "video_count", "format_video_count", "storefront_displayed_video_count"),
    ("product_count", "on_sell_product_count", "display_on_sell_product_count", "platform_on_sell_product_count"),
)


@dataclass(frozen=True)
class ParsedShopProfile:
    platform_shop_id: str
    canonical_name: str
    canonical_url: str
    region: str
    fields: dict
    field_state: dict
    observation_status: str


def _error(code: str) -> PublicPageError:
    return PublicPageError(code, "公开店铺档案身份或页面结构校验失败。", status=200)


def _text(value, maximum: int) -> tuple[str | None, str]:
    if value is None or value == "":
        return None, "not_provided"
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or re.search(r"[\x00-\x1f\x7f]", value)):
        return None, "invalid"
    return value, "observed"


def _integer(value) -> tuple[int | None, str]:
    if value is None or value == "":
        return None, "not_provided"
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value):
        value = int(value)
    if type(value) is not int or not 0 <= value <= MAX_UNSIGNED_BIGINT:
        return None, "invalid"
    return value, "exact"


def _count(info: dict, path: str, output: str, raw_key: str, display_key: str, scope: str):
    raw = info.get(raw_key)
    number, state = _integer(raw)
    display, display_state = _text(info.get(display_key), 128)
    # A source may supply only an abbreviated count. Retain it as display text,
    # never multiply K/M/B or turn a lower bound into an exact count.
    if (state == "invalid" and isinstance(raw, str) and len(raw) <= 128
            and re.fullmatch(r"(?:[0-9]+(?:\.[0-9]+)?[KMBTkmbt]\+?|[0-9]+\+)", raw)):
        if display_state == "not_provided":
            display, display_state = raw, "observed"
        state = "display_only" if display_state == "observed" else "invalid"
    elif state == "not_provided" and display_state == "observed":
        state = "display_only"
    elif state == "not_provided" and display_state == "invalid":
        state = "invalid"
    metadata = {"state": state, "source": f"{path}.{raw_key}",
                "display_source": f"{path}.{display_key}", "display_state": display_state,
                "scope": scope}
    if display == raw and state == "display_only" and info.get(display_key) in {None, ""}:
        metadata["display_source"] = f"{path}.{raw_key}"
    return {output: number, f"{output}_text": display}, metadata


def _badge(info: dict, path: str, container: str, key: str, maximum: int):
    parent = info.get(container)
    source = f"{path}.{container}.{key}"
    if parent is None or parent == "":
        return None, {"state": "not_displayed", "source": source}
    if not isinstance(parent, dict):
        return None, {"state": "invalid", "source": source}
    text, state = _text(parent.get(key), maximum)
    return text, {"state": "not_displayed" if state == "not_provided" else state, "source": source}


def _shop_info(html: str, source_url: str) -> tuple[dict, str, str]:
    expected = shop_id_from_url(source_url)
    if expected is None:
        raise _error("not_a_storefront")
    if re.search(r"<title[^>]*>\s*Security\s+Check\s*</title>", html, re.IGNORECASE):
        raise PublicPageError("access_limited", "公开店铺返回安全验证。", status=200)
    script = RouterScriptParser()
    script.feed(html)
    if script.script_count != 1 or not script.parts:
        raise _error("missing_router_data")
    try:
        root = json.loads("".join(script.parts))
        loaders = root.get("loaderData") if isinstance(root, dict) else None
        if not isinstance(loaders, dict):
            raise _error("invalid_router_data")
        pages = [(route, page) for route, page in loaders.items()
                 if re.search(r"(?:^|/)store/", route) and route.endswith("/page") and isinstance(page, dict)]
        if len(pages) != 1:
            raise _error("ambiguous_store_page")
        route, page = pages[0]
        region = page.get("route_info")
        if (not isinstance(region, dict) or not isinstance(region.get("path_region"), str)
                or region["path_region"].lower() != "us"):
            raise _error("market_mismatch")
        configuration = page.get("page_config")
        components = configuration.get("components_map") if isinstance(configuration, dict) else None
        if isinstance(components, list):
            entries = enumerate(components)
        elif isinstance(components, dict):
            entries = components.items()
        else:
            raise _error("missing_components")
        shops = [(index, component) for index, component in entries
                 if isinstance(component, dict) and component.get("component_type") == "shop_info"]
        if len(shops) != 1:
            raise _error("ambiguous_shop_info")
        index, component = shops[0]
        info = component.get("component_data")
        info = info.get("shopInfo") if isinstance(info, dict) else None
        if not isinstance(info, dict):
            raise _error("invalid_shop_info")
        identifier = info.get("seller_id")
        if type(identifier) is int:
            identifier = str(identifier)
        if (not isinstance(identifier, str) or not re.fullmatch(r"[0-9]{1,128}", identifier)
                or identifier != expected):
            raise _error("store_identity_mismatch")
        if info.get("region") != "US":
            raise _error("market_mismatch")
        name, state = _text(info.get("shop_name"), 255)
        if state != "observed":
            raise _error("invalid_shop_name")
        path = f"loaderData.{route}.page_config.components_map.{index}.component_data.shopInfo"
        return info, path, name.strip()
    except (ValueError, RecursionError, TypeError, AttributeError):
        raise _error("invalid_router_data") from None


def parse_shop_profile(html: str, source_url: str) -> ParsedShopProfile:
    """Project one verified store header; malformed optional values remain NULL."""
    source_url = public_url(source_url)
    info, path, name = _shop_info(html, source_url)
    fields = dict.fromkeys(PROFILE_VALUE_FIELDS)
    states = {}
    logo = info.get("shop_logo")
    if logo is None or logo == "":
        logo_state = "not_provided"
    elif isinstance(logo, dict):
        urls = logo.get("url_list")
        fields["logo_url"] = first_url(urls)
        logo_state = "observed" if fields["logo_url"] else ("not_provided" if urls is None or urls == [] else "invalid")
    else:
        logo_state = "invalid"
    states["logo_url"] = {"state": logo_state, "source": f"{path}.shop_logo.url_list", "download_verified": False}
    for output, container, key, maximum in (
        ("verification_type", "store_brand_show", "label_type_str", 32),
        ("identity_label_key", "shop_identity_label", "ShopIdentityLabelKey", 64),
        ("identity_label_text", "shop_identity_label", "identity_label_text", 255),
    ):
        fields[output], states[output] = _badge(info, path, container, key, maximum)
    for output, raw_key, display_key, scope in _COUNTS:
        values, metadata = _count(info, path, output, raw_key, display_key, scope)
        fields.update(values)
        states[output] = metadata
    fields["review_count"], state = _integer(info.get("review_count"))
    states["review_count"] = {"state": state, "source": f"{path}.review_count", "scope": "storefront_review_count"}
    raw_rating = info.get("shop_rating")
    rating_state = "not_provided"
    if raw_rating is not None and raw_rating != "":
        rating_state = "invalid"
        if type(raw_rating) in {int, float}:
            raw_rating = str(raw_rating)
        if isinstance(raw_rating, str) and re.fullmatch(r"[0-9]{1,2}(?:\.[0-9]{1,2})?", raw_rating):
            try:
                value = Decimal(raw_rating)
                if value.is_finite() and 0 <= value <= 5:
                    fields["rating"], rating_state = value, "exact"
            except InvalidOperation:
                pass
    states["rating"] = {"state": rating_state, "source": f"{path}.shop_rating", "scope": "storefront_rating_out_of_5"}
    complete = (
        states["logo_url"]["state"] == "observed"
        and all(states[key]["state"] in {"exact", "display_only"} for key in ("follower_count", "sales_count", "video_count"))
        and all(states[key]["state"] in {"observed", "not_displayed"}
                for key in ("verification_type", "identity_label_key", "identity_label_text"))
        and all(value["state"] != "invalid" and value.get("display_state") != "invalid" for value in states.values())
    )
    return ParsedShopProfile(str(info["seller_id"]), name, source_url, "US", fields, states,
                             "complete" if complete else "partial")


@dataclass(frozen=True)
class ShopProfilePageSnapshot:
    source_url: str
    final_url: str
    status: int
    response_bytes: int
    response_hash: str
    latency_ms: int
    network_attempts: int
    started_at: datetime
    captured_at: datetime
    profile: ParsedShopProfile

    def save_evidence(self, directory: Path) -> Path:
        """Persist public facts and provenance without HTML, layout or cookies."""
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.response_hash}.json"
        record = asdict(self)
        record.update(parser_version=PARSER_VERSION, evidence_kind="public_shop_profile_projection")
        def serialize(value):
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, Decimal):
                return str(value)
            raise TypeError("Unsupported shop profile projection value")
        path.write_text(json.dumps(record, ensure_ascii=False, default=serialize, indent=2) + "\n", encoding="utf-8")
        return path


class ShopProfileClient(PublicShopClient):
    def fetch_profile(self, source_url: str) -> ShopProfilePageSnapshot:
        source_url = public_url(source_url)
        expected = shop_id_from_url(source_url)
        if expected is None:
            raise _error("not_a_storefront")
        began, start = datetime.now(timezone.utc), time.monotonic()
        for attempt in range(1, self.max_attempts + 1):
            body = None
            try:
                final, body, encoding = self._read(source_url)
                if shop_id_from_url(final) != expected:
                    raise _error("store_identity_mismatch")
                profile = parse_shop_profile(body.decode(encoding, errors="replace"), final)
                return ShopProfilePageSnapshot(source_url, final, 200, len(body), sha256(body).hexdigest(),
                    round((time.monotonic() - start) * 1000), attempt, began, datetime.now(timezone.utc), profile)
            except httpx.RequestError:
                failure = PublicPageError("network_error", "公开店铺档案连接失败或超时。", retryable=True)
            except PublicPageError as exc:
                failure = exc
            annotate_failure(failure, source_url, began, body)
            failure.diagnostics.update(network_attempts=attempt, latency_ms=round((time.monotonic() - start) * 1000))
            if not failure.retryable or attempt == self.max_attempts:
                raise failure from None
            time.sleep(min(2 ** attempt, 4))
        raise AssertionError("unreachable")
