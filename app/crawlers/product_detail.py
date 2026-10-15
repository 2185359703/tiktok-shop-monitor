"""Public PDP, SKU and product photo parsing; reviews and AI summaries excluded."""

import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path

import httpx

from app.crawlers.product_list import annotate_failure, decimal_value, identifier, integer_count, product_id_from_url, text_field
from app.crawlers.public_shop import PublicPageError, PublicShopClient, RouterScriptParser, public_url
from app.crawlers.product_images import IMAGE_SOURCE_VERSION, ImageCandidate, detail_images
from app.models import StockStatus
from app.services.seed_importer import fingerprint


PARSER_VERSION = "public_pdp_sku_v1"
ATTRIBUTE_ALIASES = {
    "brand": {"brand", "品牌"}, "material": {"material", "materials", "fabric", "fabric type", "材质", "面料"},
    "gender": {"gender", "sex", "性别"}, "season": {"season", "季节"}, "style": {"style", "风格"},
    "fit": {"fit", "fit type", "版型"}, "garment_length": {"clothing length", "dress length", "top length", "item length description", "衣长", "裙长"},
    "pattern": {"pattern", "图案"},
}
COLOR_NAMES = {"color", "colour", "颜色"}
SIZE_NAMES = {"size", "尺码", "尺寸"}
PROPERTY_NAME_ALIASES = {"size(general size)": "size"}


def invalid(code):
    return PublicPageError(code, "公开详情或 SKU 字段校验失败。", status=200)


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.ignored = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}: self.ignored += 1
        if tag in {"p", "li", "br", "div"} and not self.ignored: self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}: self.ignored = max(0, self.ignored - 1)
        if tag in {"p", "li", "div"} and not self.ignored: self.parts.append("\n")

    def handle_data(self, data):
        if not self.ignored: self.parts.append(data)


def plain_text(value):
    if not isinstance(value, str): raise invalid("invalid_merchant_description")
    parser = PlainText()
    parser.feed(value)
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())


def parse_description(blocks):
    if blocks is None: return None, []
    if not isinstance(blocks, list) or len(blocks) > 500: raise invalid("invalid_merchant_description")
    lines, unsupported = [], []
    for block in blocks:
        if not isinstance(block, dict): raise invalid("invalid_merchant_description")
        kind = block.get("type")
        if kind == "image": continue
        value = block.get("text") if kind == "text" else block.get("content")
        if value is None:
            unsupported.append(str(kind))
            continue
        values = value if isinstance(value, list) else [value]
        lines.extend(plain_text(item) for item in values)
    result = "\n".join(line for line in lines if line) or None
    if result is not None and len(result.encode("utf-8")) > 65535: raise invalid("description_too_large")
    return result, sorted(set(unsupported))


def parse_attributes(specifications, description):
    if not isinstance(specifications, list) or len(specifications) > 200: raise invalid("invalid_product_specifications")
    records = []
    for row in specifications:
        if not isinstance(row, dict): raise invalid("invalid_product_specifications")
        records.append({"name": text_field(row.get("name"), 255, required=True),
                        "value": text_field(row.get("value"), 4000, required=True), "source": "specifications"})
    # Merchant-labelled material lines are evidence, unlike title guesses or AIGC.
    for line in (description or "").splitlines():
        match = re.fullmatch(r"\s*(Fabric type|Material|Materials|面料|材质)\s*[:：]\s*(.{1,4000})", line, re.IGNORECASE)
        if match:
            records.append({"name": match.group(1), "value": match.group(2).strip(), "source": "merchant_description_label"})
    attributes, conflicts, issues = {}, {}, {}
    limits = {"brand": 255, "gender": 32, "season": 64, "style": 128, "fit": 64, "garment_length": 64, "pattern": 128, "material": 4000}
    for field, names in ATTRIBUTE_ALIASES.items():
        values = {}
        for record in records:
            if record["name"].casefold() in names:
                values.setdefault(" ".join(record["value"].casefold().split()), record["value"])
        value = next(iter(values.values())) if len(values) == 1 else None
        if value is not None and len(value) > limits[field]:
            attributes[field] = None
            issues[field] = {"reason": "exceeds_normalized_column_length", "limit": limits[field], "actual_length": len(value)}
        else:
            attributes[field] = value
        if len(values) > 1: conflicts[field] = list(values.values())
    return attributes, records, conflicts, issues


def property_definitions(props):
    if not isinstance(props, list) or len(props) > 64: raise invalid("invalid_sale_properties")
    result = {}
    for prop in props:
        prop_id = identifier(prop["prop_id"])
        name = text_field(prop["prop_name"], 255, required=True)
        if prop_id in result or not isinstance(prop["sale_prop_values"], list): raise invalid("duplicate_sale_property")
        values = {}
        for value in prop["sale_prop_values"]:
            value_id = identifier(value["prop_value_id"])
            if value_id in values: raise invalid("duplicate_sale_property_value")
            values[value_id] = text_field(value["prop_value"], 255, required=True)
        result[prop_id] = (name, values)
    return result


def money(value, code="invalid_sku_price"):
    # Only admit well-formed US thousands groups. Do not guess a decimal mark
    # or silently repair malformed amounts such as "9,88.00".
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{1,2})?", value):
        value = value.replace(",", "")
    number = decimal_value(value, maximum=Decimal("9999999999.99"), scale=2, code=code)
    return number.quantize(Decimal("0.01")) if number is not None else None


def property_name(value):
    normalized = value.casefold()
    return PROPERTY_NAME_ALIASES.get(normalized, normalized)


def stock_state(quantity, button):
    # No guessing the platform's numeric status enum.
    if quantity is not None and quantity > 0 and button == "default": return StockStatus.IN_STOCK
    if quantity == 0 and button == "sold_out": return StockStatus.OUT_OF_STOCK
    return StockStatus.UNKNOWN


@dataclass(frozen=True)
class VariantDetail:
    platform_variant_id: str
    variant_key: str
    options: dict[str, str]
    color: str | None
    size: str | None
    material: str | None
    currency: str
    price: Decimal | None
    original_price: Decimal | None
    stock_status: StockStatus
    available_quantity: int | None
    variant_payload_hash: str


def parse_variant(row, definitions):
    sku_id = identifier(row["sku_id"])
    props = row["sku_sale_props"]
    if not isinstance(props, list): raise invalid("invalid_sku_properties")
    options, property_ids = {}, set()
    for prop in props:
        prop_id, value_id = identifier(prop["prop_id"]), identifier(prop["prop_value_id"])
        name, value = text_field(prop["prop_name"], 255, required=True), text_field(prop["prop_value"], 255, required=True)
        if (prop_id in property_ids or prop_id not in definitions or value_id not in definitions[prop_id][1]
                or property_name(name) != property_name(definitions[prop_id][0]) or value != definitions[prop_id][1][value_id]
                or name in options):
            raise invalid("sku_property_identity_conflict")
        property_ids.add(prop_id)
        options[name] = value
    if property_ids != set(definitions): raise invalid("incomplete_sku_properties")
    price = row.get("price") or {}
    if price.get("currency_name") != "USD": raise invalid("sku_currency_mismatch")
    quantity = integer_count(row.get("stock"))
    status = stock_state(quantity, row.get("pdp_button_area_id"))
    by_name = {name.casefold(): value for name, value in options.items()}
    def option(names, maximum):
        values = {value for key, value in by_name.items() if key in names}
        if len(values) > 1: raise invalid("ambiguous_sku_dimension")
        return text_field(next(iter(values)), maximum) if values else None
    values = {"platform_variant_id": sku_id, "variant_key": fingerprint({"platform": "tiktok_shop", "sku_id": sku_id}),
              "options": options, "color": option(COLOR_NAMES, 128), "size": option(SIZE_NAMES, 64),
              "material": option(ATTRIBUTE_ALIASES["material"], 255), "currency": "USD",
              "price": money(price.get("sale_price_decimal")), "original_price": money(price.get("origin_price_decimal")),
              "stock_status": status, "available_quantity": quantity}
    stable = {key: str(value) if isinstance(value, Decimal) else value for key, value in values.items()}
    return VariantDetail(**values, variant_payload_hash=fingerprint(stable))


@dataclass(frozen=True)
class ProductDetail:
    platform_product_id: str
    platform_shop_id: str
    product_url: str
    title: str
    description: str | None
    original_category: str | None
    attributes: dict[str, str | None]
    raw_attributes: dict
    color_summary: list[str]
    size_summary: list[str]
    currency: str
    price_text: str | None
    price_min: Decimal | None
    price_max: Decimal | None
    original_price_min: Decimal | None
    original_price_max: Decimal | None
    discount_rate: Decimal | None
    stock_status: StockStatus
    rating: Decimal | None
    review_count: int | None
    sales_count: int | None
    sales_count_text: str | None
    variants: tuple[VariantDetail, ...]
    images: tuple[ImageCandidate, ...] = ()


def parse_detail_block(block, source_url, expected_shop_id):
    try:
        expected_product_id = product_id_from_url(source_url)
        info, shop = block["product_info"], block["shop_info"]
        if type(block["error_code"]) is not int or block["error_code"] != 0: raise invalid("product_detail_business_error")
        if (identifier(info["product_id"]) != expected_product_id or identifier(info["seller_id"]) != expected_shop_id
                or identifier(shop["seller_id"]) != expected_shop_id or shop["region"] != "US"):
            raise invalid("product_detail_identity_conflict")
        if type(info["status"]) is not int or info["status"] != 1: raise invalid("unsupported_product_status")
        title = text_field(info["title"], 512, required=True)
        description, unsupported = parse_description(info.get("desc_blocks"))
        attributes, specifications, conflicts, issues = parse_attributes(info.get("specifications") or [], description)
        definitions = property_definitions(info.get("sale_props") or [])
        if not isinstance(info.get("skus"), list) or not 1 <= len(info["skus"]) <= 5000: raise invalid("missing_product_skus")
        variants = {}
        for row in info["skus"]:
            variant = parse_variant(row, definitions)
            if variant.platform_variant_id in variants: raise invalid("duplicate_sku_identifier")
            variants[variant.platform_variant_id] = variant
        variants = tuple(sorted(variants.values(), key=lambda value: value.platform_variant_id))
        categories = block.get("categories") or []
        if not isinstance(categories, list) or len(categories) > 16: raise invalid("invalid_product_categories")
        original_category = " > ".join(text_field(row["category_name"], 512, required=True) for row in categories) or None
        price = info["price"]
        if price.get("currency") != "USD" or type(price.get("is_interval_price")) is not bool: raise invalid("detail_price_context_mismatch")
        minimum, maximum = money(price.get("min_sku_price")), money(price.get("max_sku_price"))
        origin_min, origin_max = money(price.get("min_sku_original_price")), money(price.get("max_sku_original_price"))
        if not price["is_interval_price"]:
            maximum, origin_max = minimum, origin_min
        if (minimum is not None and maximum is not None and minimum > maximum) or (origin_min is not None and origin_max is not None and origin_min > origin_max):
            raise invalid("invalid_detail_price_range")
        discount = None
        if price.get("discount"):
            match = re.fullmatch(r"-?([0-9]+(?:\.[0-9]+)?)%", price["discount"])
            if not match: raise invalid("invalid_detail_discount")
            discount = decimal_value(str(Decimal(match.group(1)) / 100), maximum=Decimal(1), scale=4, code="invalid_detail_discount")
        statuses = {variant.stock_status for variant in variants}
        stock = StockStatus.UNKNOWN if StockStatus.UNKNOWN in statuses else next(iter(statuses)) if len(statuses) == 1 else StockStatus.PARTIAL
        review = info.get("review") or {}
        rating_value = review.get("product_rating")
        if type(rating_value) is float: rating_value = str(rating_value)
        rating = decimal_value(rating_value, maximum=Decimal(5), scale=2, code="invalid_detail_rating")
        raw_sold = info.get("sold_count")
        sold = None if isinstance(raw_sold, str) and not re.fullmatch(r"[0-9]+", raw_sold) else integer_count(raw_sold)
        note = info.get("sold_count_note")
        sold_text = text_field(note if isinstance(note, str) and note else (str(raw_sold) if raw_sold is not None else None), 128)
        raw_attributes = {"specifications": specifications, "attribute_conflicts": conflicts,
                          "normalization_issues": issues,
                          "unsupported_description_blocks": unsupported, "category_ids": [identifier(row["category_id"]) for row in categories],
                          "metric_scopes": {"review_count": "product_info.review", "sales_count":
                              "global_cumulative_excluding_returned" if isinstance(note, dict) and "globally" in str(note.get("text", "")).lower()
                              else "unspecified"}}
        return ProductDetail(expected_product_id, expected_shop_id, public_url(source_url), title, description, original_category,
                             attributes, raw_attributes, sorted({variant.color for variant in variants if variant.color}),
                             sorted({variant.size for variant in variants if variant.size}), "USD", text_field(price.get("real_price"), 255),
                             minimum, maximum, origin_min, origin_max, discount, stock, rating, integer_count(review.get("review_count")),
                             sold, sold_text, variants, detail_images(info))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise invalid("invalid_product_detail_structure") from None


def parse_product_detail(html, source_url, expected_shop_id):
    if re.search(r"<title[^>]*>\s*Security\s+Check\s*</title>", html, re.IGNORECASE):
        raise PublicPageError("access_limited", "详情返回安全验证，停止访问。", status=200)
    if product_id_from_url(source_url) is None: raise invalid("unsupported_product_url")
    script = RouterScriptParser()
    script.feed(html)
    if script.script_count != 1 or not script.parts: raise invalid("missing_detail_router_data")
    try:
        root = json.loads("".join(script.parts))
        pages = [page for route, page in root["loaderData"].items() if route.endswith("/page") and isinstance(page, dict)]
        if len(pages) != 1: raise invalid("ambiguous_product_detail_page")
        page = pages[0]
        if (page.get("route_info") or {}).get("path_region", "us").lower() != "us": raise invalid("detail_market_mismatch")
        components = page["page_config"]["components_map"]
        entries = components if isinstance(components, list) else list(components.values())
        blocks = [row["component_data"] for row in entries if row.get("component_type") == "product_info"]
        if len(blocks) != 1: raise invalid("missing_product_detail_component")
        return parse_detail_block(blocks[0], source_url, expected_shop_id)
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        raise invalid("invalid_product_detail_router") from None


@dataclass(frozen=True)
class DetailSnapshot:
    source_url: str
    final_url: str
    status: int
    response_bytes: int
    response_hash: str
    latency_ms: int
    started_at: datetime
    captured_at: datetime
    detail: ProductDetail

    @classmethod
    def from_evidence(cls, record: dict):
        """Restore only this application's allowlisted successful projection."""
        try:
            if record.get("evidence_kind") != "public_product_detail_projection" or record.get("parser_version") != PARSER_VERSION:
                raise ValueError
            raw = record["detail"]
            product_fields = ("price_min", "price_max", "original_price_min", "original_price_max", "discount_rate", "rating")
            values = {key: raw[key] for key in ProductDetail.__dataclass_fields__ if key not in {"variants", "images"}}
            if raw.get("images") and record.get("image_source_version") != IMAGE_SOURCE_VERSION:
                raise ValueError
            values["images"] = tuple(ImageCandidate.from_record(row) for row in raw.get("images", []))
            for key in product_fields:
                values[key] = Decimal(raw[key]) if raw[key] is not None else None
            values["stock_status"] = StockStatus(raw["stock_status"])
            variants = []
            for row in raw["variants"]:
                item = {key: row[key] for key in VariantDetail.__dataclass_fields__}
                for key in ("price", "original_price"):
                    item[key] = Decimal(row[key]) if row[key] is not None else None
                item["stock_status"] = StockStatus(row["stock_status"])
                variants.append(VariantDetail(**item))
            values["variants"] = tuple(variants)
            detail = ProductDetail(**values)
            source, final = public_url(record["source_url"]), public_url(record["final_url"])
            if product_id_from_url(final) != detail.platform_product_id or product_id_from_url(source) != detail.platform_product_id:
                raise ValueError
            started, captured = datetime.fromisoformat(record["started_at"]), datetime.fromisoformat(record["captured_at"])
            if (started.tzinfo is None or captured.tzinfo is None or captured < started or record["status"] != 200
                    or not re.fullmatch(r"[a-f0-9]{64}", record["response_hash"])):
                raise ValueError
            return cls(source, final, 200, record["response_bytes"], record["response_hash"], record["latency_ms"], started, captured, detail)
        except (KeyError, ValueError, TypeError, AttributeError):
            raise ValueError("详情证据格式、版本或身份不一致。") from None

    def save_evidence(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.response_hash}.json"
        data = {**asdict(self), "parser_version": PARSER_VERSION, "image_source_version": IMAGE_SOURCE_VERSION,
                "evidence_kind": "public_product_detail_projection"}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2,
                                   default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value)) + "\n", encoding="utf-8")
        return path


class ProductDetailClient(PublicShopClient):
    def fetch_detail(self, source_url, expected_shop_id):
        source_url = public_url(source_url)
        began, start, body = datetime.now(timezone.utc), time.monotonic(), None
        try:
            final, body, encoding = self._read(source_url)
            if product_id_from_url(final) != product_id_from_url(source_url): raise invalid("product_detail_redirect_conflict")
            detail = parse_product_detail(body.decode(encoding, errors="replace"), final, expected_shop_id)
        except httpx.RequestError:
            error = PublicPageError("network_error", "公开详情连接失败或超时。", retryable=True)
            annotate_failure(error, source_url, began)
            raise error from None
        except PublicPageError as error:
            if error.code in {"invalid_product_detail_structure", "missing_product_detail_component", "invalid_product_detail_router"}:
                # A sampled PDP sometimes returns an incomplete generic shell.
                # Keep identity/price errors permanent, and bound these retries
                # by the existing durable task budget; no empty result is saved.
                error.retryable = True
            annotate_failure(error, source_url, began, body)
            raise
        return DetailSnapshot(source_url, final, 200, len(body), sha256(body).hexdigest(),
                              round((time.monotonic() - start) * 1000), began, datetime.now(timezone.utc), detail)
