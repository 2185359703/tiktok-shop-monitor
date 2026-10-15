"""Validated public US storefront feeds, with native anonymous pagination."""

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from app.crawlers.public_shop import MAX_RESPONSE_BYTES, PublicPageError, PublicShopClient, RouterScriptParser, public_url, shop_id_from_url
from app.crawlers.product_images import IMAGE_SOURCE_VERSION, ImageCandidate, listing_images


PARSER_VERSION = "public_store_feed_v1"
PRODUCT_LIST_ENDPOINT = "https://shop.tiktok.com/api/shop/brandy_desktop/store/product_list"
MAX_CURSOR_LENGTH = 4096
MAX_EXCLUDED_IDS = 10000


def invalid(code: str) -> PublicPageError:
    return PublicPageError(code, "公开商品列表字段或分页校验失败。", status=200)


def identifier(value) -> str:
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,128}", value):
        raise invalid("invalid_product_identifier")
    return value


def product_id_from_url(value: str) -> str | None:
    try:
        value = public_url(value)
    except (PublicPageError, TypeError, AttributeError):
        return None
    match = re.fullmatch(r"/us/pdp/(?:[^/]+/)?([0-9]{1,128})/?", urlsplit(value).path)
    return match.group(1) if match else None


def decimal_value(value, *, maximum: Decimal, scale: int, code: str) -> Decimal | None:
    if value is None or value == "":
        return None
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        raise invalid(code)
    try:
        number = Decimal(value)
        if not number.is_finite() or number < 0 or number > maximum or number != number.quantize(Decimal(10) ** -scale):
            raise invalid(code)
        return number
    except InvalidOperation:
        raise invalid(code) from None


def integer_count(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value):
        value = int(value)
    if type(value) is not int or not 0 <= value <= 2 ** 64 - 1:
        raise invalid("invalid_public_count")
    return value


def text_field(value, maximum: int, *, required: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise invalid("missing_product_field")
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise invalid("invalid_product_text")
    return value.strip()


def component_projection(component: dict) -> dict:
    try:
        fe = component["fe_config"]
        source = fe["data_source"]
        parameters = source["params"]
        if (component["component_type"] != "feed_list" or component["component_name"] != "feed_list_store_list_product"
                or source["type"] != "store_list_product" or type(parameters["count"]) is not int
                or not 1 <= parameters["count"] <= 100 or type(parameters["clamp"]) is not bool):
            raise invalid("unsupported_product_feed")
        flags = ("is_main_feed", "is_fmp_significant", "show_structure_data", "show_empty_page")
        if any(type(fe[key]) is not bool for key in flags):
            raise invalid("invalid_feed_configuration")
        return {"component_type": component["component_type"], "component_name": component["component_name"],
                "fe_config": {**{key: fe[key] for key in flags},
                              "data_source": {"type": source["type"], "params": {"count": parameters["count"], "clamp": parameters["clamp"]}}}}
    except (KeyError, TypeError):
        raise invalid("missing_feed_configuration") from None


@dataclass(frozen=True)
class ProductCandidate:
    platform_product_id: str
    platform_shop_id: str
    product_url: str
    title: str
    currency: str
    price_min: Decimal | None
    price_max: Decimal | None
    original_price_min: Decimal | None
    original_price_max: Decimal | None
    discount_rate: Decimal | None
    price_text: str | None
    rating: Decimal | None
    review_count: int | None
    sales_count: int | None
    sales_count_text: str | None
    brand: str | None
    # CDN mirrors can vary for the same business row; photos do not alter duplicate checks.
    images: tuple[ImageCandidate, ...] = field(default=(), compare=False)


def parse_product(value: dict, expected_shop_id: str) -> ProductCandidate:
    try:
        product_id = identifier(value["product_id"])
        seller_id = identifier(value["seller_info"]["seller_id"])
        if seller_id != expected_shop_id:
            raise invalid("product_owner_mismatch")
        canonical = value["seo_url"]["canonical_url"]
        if product_id_from_url(canonical) != product_id:
            raise invalid("product_url_identity_mismatch")
        canonical = public_url(canonical)
        title = text_field(value["title"], 512, required=True)
        price = value.get("product_price_info") or {}
        sale = decimal_value(price.get("sale_price_decimal"), maximum=Decimal("9999999999.99"), scale=2, code="invalid_price")
        origin = decimal_value(price.get("origin_price_decimal"), maximum=Decimal("9999999999.99"), scale=2, code="invalid_original_price")
        currency = price.get("currency_name")
        if currency not in {None, "", "USD"} or (currency != "USD" and (sale is not None or origin is not None)):
            raise invalid("product_currency_mismatch")
        prefix = text_field(price.get("price_prefix"), 64)
        # Any nonempty prefix is conservatively a lower bound, not an exact SKU price.
        sale_max, origin_max = (None, None) if prefix else (sale, origin)
        discount = decimal_value(price.get("discount_decimal"), maximum=Decimal(1), scale=4, code="invalid_discount")
        rate = value.get("rate_info") or {}
        score = rate.get("score")
        if type(score) is float:
            score = str(score)
        rating = decimal_value(score, maximum=Decimal(5), scale=2, code="invalid_rating")
        reviews = integer_count(rate.get("review_count"))
        raw_sold = (value.get("sold_info") or {}).get("sold_count")
        if isinstance(raw_sold, str) and raw_sold and not re.fullmatch(r"[0-9]+", raw_sold):
            sold, sold_text = None, text_field(raw_sold, 128)
        else:
            sold, sold_text = integer_count(raw_sold), str(raw_sold) if raw_sold is not None else None
        brand = text_field((value.get("brand_info") or {}).get("brand_name"), 255)
        price_text = f"{prefix + ' ' if prefix else ''}${sale}" if sale is not None else None
        return ProductCandidate(product_id, seller_id, canonical, title, "USD", sale, sale_max, origin, origin_max,
                                discount, price_text, rating, reviews, sold, sold_text, brand, listing_images(value))
    except (KeyError, TypeError, AttributeError):
        raise invalid("missing_product_field") from None


@dataclass(frozen=True)
class PublicPagination:
    component: dict
    search_params: str
    seller_id: str
    shop_name: str
    excluded_product_ids: tuple[str, ...]

    def __post_init__(self):
        object.__setattr__(self, "component", component_projection(self.component))
        if type(self.seller_id) is not str:
            raise invalid("invalid_pagination_shop")
        identifier(self.seller_id)
        text_field(self.shop_name, 255, required=True)
        if (not isinstance(self.search_params, str) or not 0 < len(self.search_params) <= MAX_CURSOR_LENGTH
                or any(ord(char) < 32 for char in self.search_params)):
            raise invalid("invalid_pagination_cursor")
        if not isinstance(self.excluded_product_ids, tuple) or len(self.excluded_product_ids) > MAX_EXCLUDED_IDS:
            raise invalid("pagination_exclusion_limit")
        if any(type(value) is not str or identifier(value) != value for value in self.excluded_product_ids):
            raise invalid("invalid_pagination_exclusions")
        if len(set(self.excluded_product_ids)) != len(self.excluded_product_ids):
            raise invalid("duplicate_pagination_exclusions")

    def to_payload(self) -> dict:
        return {"component": self.component, "search_params": self.search_params, "seller_id": self.seller_id,
                "shop_name": self.shop_name, "excluded_product_ids": list(self.excluded_product_ids)}

    @classmethod
    def from_payload(cls, value: dict) -> "PublicPagination":
        try:
            if set(value) != {"component", "search_params", "seller_id", "shop_name", "excluded_product_ids"} or not isinstance(value["excluded_product_ids"], list):
                raise invalid("invalid_pagination_payload")
            return cls(value["component"], value["search_params"], value["seller_id"], value["shop_name"], tuple(value["excluded_product_ids"]))
        except (KeyError, TypeError):
            raise invalid("invalid_pagination_payload") from None

    def request_payload(self) -> dict:
        return {"component_info": json.dumps(self.component, ensure_ascii=False, separators=(",", ":")),
                "load_more_params": json.dumps({"search_params": self.search_params, "offset": 0,
                    "seller_id": self.seller_id, "search_word": self.shop_name,
                    "exclude_product_ids": list(self.excluded_product_ids)}, ensure_ascii=False, separators=(",", ":"))}


@dataclass(frozen=True)
class ParsedFeed:
    platform_shop_id: str
    shop_name: str
    products: tuple[ProductCandidate, ...]
    has_more: bool
    next_pagination: PublicPagination | None
    duplicate_products: int
    total_product_count: int | None = None


def parse_feed_data(data: dict, shop_id: str, shop_name: str, component: dict, previous: PublicPagination | None = None) -> ParsedFeed:
    if not isinstance(data, dict) or not isinstance(data.get("products"), list) or type(data.get("has_more")) is not bool:
        raise invalid("invalid_product_feed_data")
    if len(data["products"]) > 100:
        raise invalid("product_page_too_large")
    unique = {}
    for value in data["products"]:
        if not isinstance(value, dict):
            raise invalid("invalid_product_record")
        candidate = parse_product(value, shop_id)
        if candidate.platform_product_id in unique and unique[candidate.platform_product_id] != candidate:
            raise invalid("duplicate_product_conflict")
        unique.setdefault(candidate.platform_product_id, candidate)
    excluded = previous.excluded_product_ids if previous else ()
    new_ids = [value for value in unique if value not in set(excluded)]
    next_page = None
    if data["has_more"]:
        if not new_ids:
            raise invalid("pagination_not_progressing")
        parameters = data.get("load_more_params")
        if not isinstance(parameters, dict):
            raise invalid("invalid_pagination_cursor")
        cursor = parameters.get("search_params")
        if previous is not None and cursor == previous.search_params:
            raise invalid("pagination_not_progressing")
        next_page = PublicPagination(component, cursor, shop_id, shop_name, tuple(dict.fromkeys((*excluded, *unique))))
    return ParsedFeed(shop_id, shop_name, tuple(unique.values()), data["has_more"], next_page,
                      len(data["products"]) - len(unique))


def parse_store_products(html: str, source_url: str) -> ParsedFeed:
    expected = shop_id_from_url(source_url)
    if expected is None:
        raise invalid("not_a_storefront")
    if re.search(r"<title[^>]*>\s*Security\s+Check\s*</title>", html, re.IGNORECASE):
        raise PublicPageError("access_limited", "公开店铺返回安全验证。", status=200)
    script = RouterScriptParser()
    script.feed(html)
    if script.script_count != 1 or not script.parts:
        raise invalid("missing_router_data")
    try:
        root = json.loads("".join(script.parts))
        pages = [page for route, page in root["loaderData"].items() if route.endswith("/page") and isinstance(page, dict)]
        if len(pages) != 1:
            raise invalid("ambiguous_store_page")
        page = pages[0]
        if (page.get("route_info") or {}).get("path_region", "us").lower() != "us":
            raise invalid("market_mismatch")
        components = page["page_config"]["components_map"]
        entries = components if isinstance(components, list) else list(components.values())
        shops = [value["component_data"]["shopInfo"] for value in entries if value.get("component_type") == "shop_info"]
        feeds = [value for value in entries if value.get("component_type") == "feed_list"]
        if len(shops) != 1 or len(feeds) != 1 or identifier(shops[0]["seller_id"]) != expected or shops[0]["region"] != "US":
            raise invalid("store_identity_mismatch")
        name = text_field(shops[0]["shop_name"], 255, required=True)
        component = component_projection(feeds[0])
        parsed = parse_feed_data(feeds[0]["component_data"], expected, name, component)
        return ParsedFeed(parsed.platform_shop_id, parsed.shop_name, parsed.products, parsed.has_more,
                          parsed.next_pagination, parsed.duplicate_products, integer_count(shops[0].get("on_sell_product_count")))
    except (KeyError, TypeError, AttributeError, ValueError, RecursionError):
        raise invalid("invalid_store_feed_structure") from None


def parse_product_page_json(body: bytes, pagination: PublicPagination) -> ParsedFeed:
    if re.search(rb"<title[^>]*>\s*Security\s+Check\s*</title>", body, re.IGNORECASE):
        raise PublicPageError("access_limited", "公开分页返回安全验证。", status=200)
    try:
        value = json.loads(body)
        if type(value.get("code")) is not int or value["code"] != 0:
            raise invalid("product_api_business_error")
        return parse_feed_data(value["data"], pagination.seller_id, pagination.shop_name, pagination.component, pagination)
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise invalid("invalid_product_api_response") from None


@dataclass(frozen=True)
class ProductPageSnapshot:
    source_url: str
    final_url: str
    status: int
    response_bytes: int
    response_hash: str
    latency_ms: int
    started_at: datetime
    captured_at: datetime
    feed: ParsedFeed

    def save_evidence(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.response_hash}.json"
        data = asdict(self)
        data.update(parser_version=PARSER_VERSION, image_source_version=IMAGE_SOURCE_VERSION,
                    evidence_kind="public_product_feed_projection")
        def serialize(value):
            if isinstance(value, datetime): return value.isoformat()
            if isinstance(value, Decimal): return str(value)
            raise TypeError("Unsupported projection value")
        path.write_text(json.dumps(data, ensure_ascii=False, default=serialize, indent=2) + "\n", encoding="utf-8")
        return path


class ProductPageClient(PublicShopClient):
    def fetch_store(self, source_url: str) -> ProductPageSnapshot:
        source_url = public_url(source_url)
        began, start = datetime.now(timezone.utc), time.monotonic()
        body = None
        try:
            final, body, encoding = self._read(source_url)
            feed = parse_store_products(body.decode(encoding, errors="replace"), final)
        except httpx.RequestError:
            error = PublicPageError("network_error", "公开店铺连接失败或超时。", retryable=True)
            annotate_failure(error, source_url, began)
            raise error from None
        except PublicPageError as error:
            annotate_failure(error, source_url, began, body)
            raise
        return ProductPageSnapshot(source_url, final, 200, len(body), sha256(body).hexdigest(),
                                   round((time.monotonic() - start) * 1000), began, datetime.now(timezone.utc), feed)

    def fetch_next(self, pagination: PublicPagination) -> ProductPageSnapshot:
        began, start = datetime.now(timezone.utc), time.monotonic()
        self.before_http_request()
        try:
            with self.client.stream("POST", PRODUCT_LIST_ENDPOINT, json=pagination.request_payload(),
                                    headers={"Accept": "application/json"}) as response:
                if response.status_code in {401, 403, 429}:
                    raise PublicPageError("access_limited", "公开分页入口受限，停止请求。", status=response.status_code)
                if response.status_code != 200:
                    raise PublicPageError("product_http_error", "公开分页返回非成功状态。", status=response.status_code,
                                          retryable=response.status_code >= 500)
                chunks, length = [], 0
                for chunk in response.iter_bytes():
                    length += len(chunk)
                    if length > MAX_RESPONSE_BYTES:
                        raise PublicPageError("response_too_large", "分页响应超过 8 MiB。", status=200)
                    chunks.append(chunk)
            body = b"".join(chunks)
        except httpx.RequestError:
            error = PublicPageError("network_error", "公开分页连接失败或超时。", retryable=True)
            annotate_failure(error, PRODUCT_LIST_ENDPOINT, began)
            raise error from None
        except PublicPageError as error:
            annotate_failure(error, PRODUCT_LIST_ENDPOINT, began)
            raise
        finally:
            self.last_request_finished = time.monotonic()
        try:
            feed = parse_product_page_json(body, pagination)
        except PublicPageError as error:
            annotate_failure(error, PRODUCT_LIST_ENDPOINT, began, body)
            raise
        return ProductPageSnapshot(PRODUCT_LIST_ENDPOINT, PRODUCT_LIST_ENDPOINT, 200, len(body), sha256(body).hexdigest(),
                                   round((time.monotonic() - start) * 1000), began, datetime.now(timezone.utc), feed)


def annotate_failure(error: PublicPageError, source_url: str, began: datetime, body: bytes | None = None) -> None:
    error.diagnostics = {"request_url": source_url, "response_status": error.status,
                         "started_at": began.isoformat(), "captured_at": datetime.now(timezone.utc).isoformat(),
                         "response_hash": sha256(body).hexdigest() if body is not None else None,
                         "response_bytes": len(body) if body is not None else None}


def save_product_failure(error: PublicPageError, directory: Path, *, parser_version: str = PARSER_VERSION) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"failure-{uuid4().hex}.json"
    data = {"evidence_kind": "public_product_failure_summary", "parser_version": parser_version,
            "code": error.code, **getattr(error, "diagnostics", {})}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
