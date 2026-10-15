"""Anonymous public reviews. Native async HTTP; a missing page is never empty success."""

import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from urllib.parse import urljoin, urlsplit

from app.crawlers.async_http import AsyncHTTPClient

from app.crawlers.product_images import valid_image_url
from app.crawlers.product_list import identifier, integer_count, product_id_from_url, text_field
from app.crawlers.public_shop import MAX_RESPONSE_BYTES, PublicPageError, RouterScriptParser, public_url
from app.services.seed_importer import fingerprint


REVIEW_SOURCE_VERSION = "public_product_reviews_v1"
REVIEW_ENDPOINT = "https://shop.tiktok.com/api/shop/pdp_desktop/get_product_reviews"


def invalid(code="invalid_review_response", *, retryable=False):
    return PublicPageError(code, "公开评论字段、商品身份或分页结构不正确。", status=200, retryable=retryable)


def review_time(value):
    if value is None or value == "":
        return None
    number = integer_count(value)
    if number == 0:
        return None
    try:
        # The public review_time field is epoch milliseconds, including its
        # subsecond part. Never substitute crawl time for a missing source time.
        return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=number)
    except (OverflowError, TypeError):
        raise invalid("invalid_review_time") from None


def optional_bool(value):
    if value is not None and type(value) is not bool:
        raise invalid("invalid_review_boolean")
    return value


def review_text(value, maximum):
    return text_field(None if isinstance(value, str) and not value.strip() else value, maximum)


@dataclass(frozen=True)
class Review:
    platform_review_id: str
    platform_product_id: str
    platform_variant_id: str | None
    rating: int | None
    text: str | None
    reviewer_name: str | None
    reviewed_at: datetime | None
    sku_specification: str | None
    country: str | None
    verified_purchase: bool | None
    incentivized: bool | None
    images: tuple[str, ...]

    @property
    def content_hash(self):
        values = asdict(self)
        values["reviewed_at"] = self.reviewed_at.isoformat() if self.reviewed_at else None
        return fingerprint(values)


@dataclass(frozen=True)
class ReviewPage:
    platform_product_id: str
    reviews: tuple[Review, ...]
    total: int | None
    has_more: bool
    page_number: int
    page_size: int
    source_kind: str

    @property
    def page_hash(self):
        return fingerprint(sorted(row.platform_review_id for row in self.reviews))


@dataclass(frozen=True)
class ReviewSnapshot:
    page: ReviewPage
    source_url: str
    response_hash: str
    response_bytes: int
    captured_at: datetime
    latency_ms: int


def parse_review(row, product_id):
    if not isinstance(row, dict) or identifier(row.get("product_id")) != product_id:
        raise invalid("review_product_mismatch")
    rating = row.get("review_rating")
    if rating is not None and (type(rating) is not int or not 1 <= rating <= 5):
        raise invalid("invalid_review_rating")
    images = row.get("review_images")
    if images is None:
        images = []
    if not isinstance(images, list) or len(images) > 50:
        raise invalid("invalid_review_images")
    country = text_field(row.get("review_country"), 16)
    return Review(identifier(row.get("review_id")), product_id,
                  identifier(row["sku_id"]) if row.get("sku_id") else None,
                  rating, review_text(row.get("review_text"), 10000),
                  review_text(row.get("reviewer_name"), 255), review_time(row.get("review_time")),
                  review_text(row.get("sku_specification"), 2000), country,
                  optional_bool(row.get("is_verified_purchase")), optional_bool(row.get("is_incentivized_review")),
                  tuple(dict.fromkeys(url for value in images if (url := valid_image_url(value)))))


def parse_review_data(data, product_id, *, page_number, page_size, source_kind):
    if (not isinstance(data, dict) or not isinstance(data.get("product_reviews"), list)
            or type(data.get("has_more")) is not bool):
        raise invalid("missing_review_data", retryable=True)
    raw = data["product_reviews"]
    if len(raw) > 100 or (source_kind == "api" and len(raw) > page_size):
        raise invalid("review_page_too_large")
    total = integer_count(data.get("total_reviews"))
    rows = {}
    for value in raw:
        row = parse_review(value, product_id)
        if row.platform_review_id in rows and rows[row.platform_review_id] != row:
            raise invalid("conflicting_review_id")
        rows[row.platform_review_id] = row
    position = (page_number - 1) * page_size
    if (not rows and (data["has_more"] or total is None or position < total)) or (total == 0 and rows):
        raise invalid("inconsistent_review_page", retryable=True)
    if not data["has_more"] and total is not None and total > position + len(raw):
        raise invalid("premature_review_exhaustion", retryable=True)
    if data["has_more"] and source_kind == "document" and len(raw) != 5:
        # The observed desktop SSR page and its next-page caller both use five.
        # A changed preview length cannot safely be converted to an API offset.
        raise invalid("review_preview_size_changed")
    return ReviewPage(product_id, tuple(rows.values()), total, data["has_more"],
                      page_number, page_size, source_kind)


def parse_review_document(html, source_url, expected_shop_id):
    if re.search(r"<title[^>]*>\s*Security\s+Check\s*</title>", html, re.I):
        raise PublicPageError("access_limited", "评论入口返回验证页。", status=200, retryable=True)
    product_id = product_id_from_url(source_url)
    if not product_id:
        raise invalid("invalid_review_product_url")
    parser = RouterScriptParser()
    parser.feed(html)
    if parser.script_count != 1 or not parser.parts:
        raise invalid("missing_review_router", retryable=True)
    try:
        root = json.loads("".join(parser.parts))
        pages = [p for route, p in root["loaderData"].items() if route.endswith("/page") and isinstance(p, dict)]
        if len(pages) != 1:
            raise invalid("ambiguous_review_page")
        page = pages[0]
        if page["route_info"]["path_region"].upper() != "US":
            raise invalid("review_market_mismatch")
        components = page["page_config"]["components_map"]
        entries = components if isinstance(components, list) else components.values()
        blocks = [c["component_data"] for c in entries if c.get("component_type") == "product_info"]
        if len(blocks) != 1:
            raise invalid("missing_review_product")
        block = blocks[0]
        info, shop = block["product_info"], block["shop_info"]
        if (type(block.get("error_code")) is not int or block["error_code"] != 0
                or identifier(info["product_id"]) != product_id
                or identifier(info["seller_id"]) != expected_shop_id
                or identifier(shop["seller_id"]) != expected_shop_id or shop["region"] != "US"):
            raise invalid("review_product_scope_conflict")
        return parse_review_data(block["review_info"], product_id, page_number=1, page_size=5, source_kind="document")
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        raise invalid("invalid_review_document", retryable=True) from None


def parse_review_response(body, product_id, page_number, page_size=5):
    try:
        payload = json.loads(body)
        if type(payload.get("code")) is not int or payload["code"] != 0:
            # HTTP 200 with code 10000/100000 is an error, never zero reviews.
            raise invalid("review_api_business_error", retryable=True)
        return parse_review_data(payload["data"], product_id, page_number=page_number,
                                 page_size=page_size, source_kind="api")
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        raise invalid("invalid_review_api_response", retryable=True) from None


class AsyncReviewClient(AsyncHTTPClient):
    async def fetch_page(self, source_url, expected_shop_id, *, page_number=1, page_size=5):
        started = time.monotonic()
        source_url = public_url(source_url)
        product_id = product_id_from_url(source_url)
        if not product_id or type(page_number) is not int or not 1 <= page_number <= 500 or page_size != 5:
            raise invalid("invalid_review_pagination")
        final = source_url
        for hop in range(4):
            status, location, body = await self.read("GET", final)
            if status == 200:
                break
            if not location or hop == 3:
                raise invalid("review_redirect_error")
            final = public_url(urljoin(final, location))
            if product_id_from_url(final) != product_id:
                raise invalid("review_redirect_identity_conflict")
        page = parse_review_document(body.decode("utf-8", errors="replace"), final, expected_shop_id)
        if page_number > 1:
            # A fresh anonymous document confirms identity and supplies its own
            # server-issued cookies. No browser cookies or signatures are copied.
            _, _, body = await self.read("POST", REVIEW_ENDPOINT,
                json={"product_id": product_id, "page_start": page_number, "page_size": page_size, "sort_rule": 1,
                      "review_filter": {"filter_type": 1, "filter_value": 6}, "component_name": "pdp_left_reviews"},
                headers={"Referer": final, "Origin": "https://shop.tiktok.com", "Accept": "application/json,*/*;q=0.8"})
            page = parse_review_response(body, product_id, page_number, page_size)
        clean_url = urlsplit(final)._replace(query="", fragment="").geturl()
        return ReviewSnapshot(page, clean_url, sha256(body).hexdigest(), len(body),
                              datetime.now(timezone.utc), round((time.monotonic() - started) * 1000))
