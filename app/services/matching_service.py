"""Human-reviewed matching. Suggestions never create relations or imply SKU identity."""

import heapq
import re
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from itertools import combinations

from sqlalchemy import func, or_, select

from app.models.product import Product, ProductVariant
from app.models.matching import MatchStatus, ProductMatch, SkuMatch


RULE_VERSION = "catalog_rules_v1"
CANDIDATE_SCAN_LIMIT = 400
STOP_WORDS = frozenset("a an the and or with for of in to women womens woman men mens new sale clothing clothes fashion apparel ladies girls boys size sizes color colors".split())


class MatchingError(ValueError):
    def __init__(self, code, *, status=422, reasons=None):
        super().__init__(code)
        self.code, self.status, self.reasons = code, status, reasons or []


def market_scope(model, settings):
    return model.platform == settings.platform, model.region == settings.region


def normalize(value):
    return _normalized_text(str(value or ""))


@lru_cache(maxsize=4096)
def _normalized_text(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def title_tokens(value):
    return _title_tokens(str(value or ""))


@lru_cache(maxsize=1024)
def _title_tokens(value):
    return frozenset(re.findall(r"[^\W_]+", normalize(value))) - STOP_WORDS


def match_evidence(left, right):
    """Explain ranking from public fields, without using price as identity evidence."""
    a, b = title_tokens(left.title), title_tokens(right.title)
    shared = a & b
    title_similarity = len(shared) / len(a | b) if a | b else 0
    left_brand, right_brand = normalize(left.brand), normalize(right.brand)
    brand_equal = bool(left_brand and right_brand and left_brand == right_brand)
    brand_conflict = bool(left_brand and right_brand and left_brand != right_brand)
    category_equal = bool(left.normalized_category and right.normalized_category and normalize(left.normalized_category) == normalize(right.normalized_category))
    colors = sorted({normalize(v) for v in left.color_summary or []} & {normalize(v) for v in right.color_summary or []})
    sizes = sorted({normalize(v) for v in left.size_summary or []} & {normalize(v) for v in right.size_summary or []})
    score = min(1, title_similarity * 0.75 + (0.15 if brand_equal else 0) + (0.06 if category_equal else 0) + (0.02 if colors else 0) + (0.02 if sizes else 0))
    candidate = bool(not brand_conflict and len(shared) >= 2 and ((brand_equal and title_similarity >= 0.35) or title_similarity >= 0.65))
    evidence = {
        "title": {"left": left.title, "right": right.title, "similarity": round(title_similarity, 4), "shared_tokens": sorted(shared)[:12]},
        "brand": {"left": left.brand, "right": right.brand, "equal": brand_equal, "conflict": brand_conflict},
        "category": {"left": left.normalized_category, "right": right.normalized_category, "equal": category_equal},
        "spec": {"shared_colors": colors, "shared_sizes": sizes},
        "limitations": ["标题、品牌和规格交集仅用于推荐；同款和精确SKU均需人工确认。"],
    }
    return round(score, 4), evidence, candidate


def product_summary(product):
    return {"id": str(product.id), "shop_id": str(product.shop_id), "title": product.title,
            "brand": product.brand, "category": product.normalized_category, "product_url": product.product_url,
            "currency": product.currency, "pricing_context": product.pricing_context,
            "price_min": str(product.price_min) if product.price_min is not None else None,
            "price_max": str(product.price_max) if product.price_max is not None else None,
            "last_success_at": product.last_success_at}


def require_product(session, settings, product_id, *, lock=False):
    query = select(Product).where(Product.id == product_id, *market_scope(Product, settings))
    if lock:
        query = query.with_for_update()
    product = session.scalar(query)
    if product is None:
        raise MatchingError("product_not_found_in_market", status=404)
    return product


def load_pair(session, settings, left_id, right_id):
    if left_id == right_id:
        raise MatchingError("cannot_match_product_to_itself")
    # Consistent lock order serializes confirmations for the same pair and avoids deadlocks.
    left_id, right_id = sorted((left_id, right_id))
    left = require_product(session, settings, left_id, lock=True)
    right = require_product(session, settings, right_id, lock=True)
    return left, right


def variant_specs(variant):
    """Keep size systems and unknown attributes explicit; never infer missing values."""
    result, reasons = {}, []
    aliases = {"colour": "color", "colors": "color", "colours": "color", "sizes": "size"}
    options = variant.options or {}
    if not isinstance(options, dict):
        return {}, ["invalid_spec_options"]
    for key, value in options.items():
        name = aliases.get(normalize(key), normalize(key))
        if not name or not isinstance(value, (str, int, float)) or isinstance(value, bool) or not normalize(value):
            reasons.append("missing_or_invalid_spec_value")
            continue
        normalized = normalize(value)
        if name in result and result[name] != normalized:
            reasons.append("conflicting_spec_values")
        result[name] = normalized
    for name in ("color", "size", "material"):
        value = getattr(variant, name, None)
        if value is not None and normalize(value):
            if name in result and result[name] != normalize(value):
                reasons.append("conflicting_" + name)
            result[name] = normalize(value)
    for name in ("color", "size"):
        if not result.get(name):
            reasons.append("missing_" + name)
    return result, sorted(set(reasons))


def validate_sku_specs(left, right):
    a, left_reasons = variant_specs(left)
    b, right_reasons = variant_specs(right)
    reasons = ["left_" + value for value in left_reasons] + ["right_" + value for value in right_reasons]
    if a.keys() != b.keys():
        reasons.append("spec_keys_differ")
    if any(a.get(key) != b.get(key) for key in a.keys() | b.keys()):
        reasons.append("spec_values_differ")
    if reasons:
        raise MatchingError("sku_specs_not_exact_or_incomplete", reasons=sorted(set(reasons)))
    return {"left_specs": a, "right_specs": b,
            "limitations": ["尺码仅按原始体系和属性复核；不自动把 S 与 Small 或不同国家尺码视为同规格。"]}


def confirm_match(session, settings, *, left_product_id, right_product_id, match_type="same_product",
                  left_variant_id=None, right_variant_id=None, note=None):
    if match_type not in {"same_product", "same_sku"}:
        raise MatchingError("invalid_match_type")
    original_left = left_product_id
    left, right = load_pair(session, settings, left_product_id, right_product_id)
    if match_type == "same_product" and (left_variant_id is not None or right_variant_id is not None):
        raise MatchingError("product_match_does_not_accept_variant_ids")
    variant_evidence = None
    if match_type == "same_sku":
        if not left_variant_id or not right_variant_id or left_variant_id == right_variant_id:
            raise MatchingError("two_distinct_variant_ids_required")
        if original_left != left.id:
            left_variant_id, right_variant_id = right_variant_id, left_variant_id
        left_variant = session.get(ProductVariant, left_variant_id)
        right_variant = session.get(ProductVariant, right_variant_id)
        if left_variant is None or right_variant is None:
            raise MatchingError("variant_not_found", status=404)
        if left_variant.product_id != left.id or right_variant.product_id != right.id:
            raise MatchingError("variant_does_not_belong_to_product")
        variant_evidence = validate_sku_specs(left_variant, right_variant)
        variant_evidence["currency"] = {"left": left_variant.currency, "right": right_variant.currency}
        variant_evidence["pricing_context"] = {"left": left.pricing_context, "right": right.pricing_context}
    now = datetime.now(timezone.utc)
    confidence, evidence, _ = match_evidence(left, right)
    match = session.scalar(select(ProductMatch).where(ProductMatch.left_product_id == left.id, ProductMatch.right_product_id == right.id).with_for_update())
    created = match is None
    if created:
        match = ProductMatch(platform=left.platform, region=left.region, left_product_id=left.id, right_product_id=right.id,
                             status=MatchStatus.CONFIRMED, source="manual_confirmation", confidence=Decimal(str(confidence)),
                             rule_version=RULE_VERSION, evidence=evidence, note=note, reviewed_at=now)
        session.add(match)
        session.flush()
    elif match.status != MatchStatus.CONFIRMED:
        match.status, match.reviewed_at = MatchStatus.CONFIRMED, now
        match.source, match.confidence, match.rule_version, match.evidence = "manual_confirmation", Decimal(str(confidence)), RULE_VERSION, evidence
    if note is not None:
        match.note = note
    sku = None
    if variant_evidence is not None:
        sku = session.scalar(select(SkuMatch).where(SkuMatch.product_match_id == match.id, SkuMatch.left_variant_id == left_variant_id, SkuMatch.right_variant_id == right_variant_id))
        created = sku is None
        if created:
            sku = SkuMatch(product_match_id=match.id, left_product_id=left.id, right_product_id=right.id,
                           left_variant_id=left_variant_id, right_variant_id=right_variant_id, status=MatchStatus.CONFIRMED,
                           source="manual_confirmation", rule_version=RULE_VERSION, evidence=variant_evidence, note=note, reviewed_at=now)
            session.add(sku)
        else:
            if sku.status != MatchStatus.CONFIRMED:
                sku.status, sku.reviewed_at = MatchStatus.CONFIRMED, now
            sku.evidence, sku.rule_version = variant_evidence, RULE_VERSION
            if note is not None:
                sku.note = note
    session.flush()
    from app.services.analytics_refresh import mark_market
    mark_market(session, settings.platform, settings.region, immediate=True)
    return match, sku, created


def require_match(session, settings, match_id, *, lock=False):
    query = select(ProductMatch).where(ProductMatch.id == match_id, *market_scope(ProductMatch, settings))
    if lock:
        query = query.with_for_update()
    match = session.scalar(query)
    if match is None:
        raise MatchingError("match_not_found_in_market", status=404)
    # Check catalog scope too, so malformed legacy rows cannot disclose another market.
    require_product(session, settings, match.left_product_id)
    require_product(session, settings, match.right_product_id)
    return match


def review_match(session, settings, match_id, status, *, sku_match_id=None, note=None):
    status = MatchStatus(status)
    match = require_match(session, settings, match_id, lock=True)
    row = match
    if sku_match_id is not None:
        row = session.scalar(select(SkuMatch).where(SkuMatch.id == sku_match_id, SkuMatch.product_match_id == match.id).with_for_update())
        if row is None:
            raise MatchingError("sku_match_not_found", status=404)
        if status == MatchStatus.CONFIRMED:
            if match.status != MatchStatus.CONFIRMED:
                raise MatchingError("product_match_must_be_confirmed", status=409)
            left = session.get(ProductVariant, row.left_variant_id)
            right = session.get(ProductVariant, row.right_variant_id)
            if left is None or right is None or left.product_id != match.left_product_id or right.product_id != match.right_product_id:
                raise MatchingError("variant_does_not_belong_to_product")
            row.evidence = validate_sku_specs(left, right)
            left_product = require_product(session, settings, match.left_product_id)
            right_product = require_product(session, settings, match.right_product_id)
            row.evidence["currency"] = {"left": left.currency, "right": right.currency}
            row.evidence["pricing_context"] = {"left": left_product.pricing_context, "right": right_product.pricing_context}
    if row.status != status:
        row.status, row.reviewed_at = status, datetime.now(timezone.utc)
    if note is not None:
        row.note = note
    if sku_match_id is None and status == MatchStatus.REJECTED:
        for sku in session.scalars(select(SkuMatch).where(SkuMatch.product_match_id == match.id, SkuMatch.status == MatchStatus.CONFIRMED)):
            sku.status, sku.reviewed_at = MatchStatus.REJECTED, row.reviewed_at
    # Reconfirming a product pair does not silently restore its rejected SKU mappings.
    session.flush()
    from app.services.analytics_refresh import mark_market
    mark_market(session, settings.platform, settings.region, immediate=True)
    return match


def sku_comparison(sku, match, left_product, right_product, left, right):
    reasons = []
    if match.status != MatchStatus.CONFIRMED or sku.status != MatchStatus.CONFIRMED:
        reasons.append("match_not_confirmed")
    if left is None or right is None:
        reasons.append("variant_not_found")
    else:
        if left.product_id != left_product.id or right.product_id != right_product.id:
            reasons.append("variant_does_not_belong_to_product")
        try:
            validate_sku_specs(left, right)
        except MatchingError as error:
            reasons.extend(error.reasons)
        if not left.currency or left.currency != right.currency or left.currency != left_product.currency or right.currency != right_product.currency:
            reasons.append("currency_mismatch")
        if not left_product.pricing_context or left_product.pricing_context != right_product.pricing_context:
            reasons.append("pricing_context_mismatch")
        if left_product.platform != right_product.platform or left_product.region != right_product.region:
            reasons.append("market_mismatch")
        if left.price is None or right.price is None:
            reasons.append("exact_sku_price_missing")
        for label, variant, product in (("left", left, left_product), ("right", right, right_product)):
            latest = (product.raw_attributes or {}).get("observed_sku_ids")
            if not isinstance(latest, list):
                reasons.append(label + "_latest_sku_observation_unknown")
            elif variant.platform_variant_id not in latest:
                reasons.append(label + "_sku_not_observed_in_latest_detail")
    comparable = not reasons
    difference = right.price - left.price if comparable else None
    return {"comparable": comparable, "reasons": sorted(set(reasons)), "currency": left.currency if comparable else None,
            "left_price": str(left.price) if left is not None and left.price is not None else None,
            "right_price": str(right.price) if right is not None and right.price is not None else None,
            "difference_right_minus_left": str(difference) if difference is not None else None,
            "difference_pct_of_left": str((difference / left.price * 100).quantize(Decimal("0.01"))) if comparable and left.price > 0 else None}


def serialize_match(session, settings, match):
    left = require_product(session, settings, match.left_product_id)
    right = require_product(session, settings, match.right_product_id)
    sku_rows = session.scalars(select(SkuMatch).where(SkuMatch.product_match_id == match.id).order_by(SkuMatch.id)).all()
    sku_items = []
    for row in sku_rows:
        left_variant, right_variant = session.get(ProductVariant, row.left_variant_id), session.get(ProductVariant, row.right_variant_id)
        sku_items.append({"id": str(row.id), "left_variant_id": str(row.left_variant_id), "right_variant_id": str(row.right_variant_id),
                          "status": row.status, "source": row.source, "rule_version": row.rule_version, "evidence": row.evidence,
                          "note": row.note, "reviewed_at": row.reviewed_at,
                          "comparison": sku_comparison(row, match, left, right, left_variant, right_variant)})
    return {"id": str(match.id), "match_type": "same_product", "status": match.status, "source": match.source,
            "confidence": float(match.confidence) if match.confidence is not None else None,
            "rule_version": match.rule_version, "evidence": match.evidence, "note": match.note,
            "created_at": match.created_at, "reviewed_at": match.reviewed_at,
            "left_product_id": str(left.id), "right_product_id": str(right.id),
            "left_product": product_summary(left), "right_product": product_summary(right), "sku_matches": sku_items}


def list_matches(session, settings, *, limit=20, after_id=0, product_id=None, status=None):
    if product_id is not None:
        require_product(session, settings, product_id)
    query = select(ProductMatch).where(*market_scope(ProductMatch, settings), ProductMatch.id > after_id)
    if product_id is not None:
        query = query.where(or_(ProductMatch.left_product_id == product_id, ProductMatch.right_product_id == product_id))
    if status is not None:
        query = query.where(ProductMatch.status == status)
    rows = session.scalars(query.order_by(ProductMatch.id).limit(limit)).all()
    return {"items": [serialize_match(session, settings, row) for row in rows],
            "next_after_id": str(rows[-1].id) if len(rows) == limit else None}


def candidate_matches(session, settings, *, product_id=None, limit=20):
    """Bounded recent-detail scan; counts are explicitly sample counts, not full-market totals."""
    anchor = require_product(session, settings, product_id) if product_id is not None else None
    query = select(Product).where(*market_scope(Product, settings), Product.title.is_not(None), Product.last_success_at.is_not(None))
    query = query.with_hint(Product, "FORCE INDEX (ix_products_recent_detail)", dialect_name="mysql")
    total = session.scalar(query.with_only_columns(func.count())) or 0
    if anchor is not None:
        query = query.where(Product.id != anchor.id)
    products = session.scalars(query.order_by(Product.last_success_at.desc(), Product.id.desc()).limit(CANDIDATE_SCAN_LIMIT)).all()
    if anchor is not None and (not anchor.title or anchor.last_success_at is None):
        return {"items": [], "unconfirmed_candidates": 0, "rule_version": RULE_VERSION,
                "scan": {"scanned_products": len(products), "eligible_products": total, "bounded": total > len(products), "reason": "anchor_details_incomplete"}}
    known = set(session.execute(select(ProductMatch.left_product_id, ProductMatch.right_product_id).where(*market_scope(ProductMatch, settings))).all())
    ordered = sorted(products, key=lambda product: product.id)
    pairs = ((anchor, product) for product in ordered) if anchor is not None else combinations(ordered, 2)
    best, evaluated, candidate_count = [], 0, 0
    for first, second in pairs:
        left, right = sorted((first, second), key=lambda item: item.id)
        evaluated += 1
        if (left.id, right.id) in known:
            continue
        confidence, evidence, candidate = match_evidence(left, right)
        if not candidate:
            continue
        candidate_count += 1
        priority = confidence, -left.id, -right.id
        if limit <= 0 or len(best) >= limit and priority <= best[0][0]:
            continue
        result = {"match_type": "same_product", "status": "suggested", "source": "catalog_rules",
                       "confidence": confidence, "rule_version": RULE_VERSION, "requires_confirmation": True,
                       "left_product_id": str(left.id), "right_product_id": str(right.id),
                       "left_product": product_summary(left), "right_product": product_summary(right), "evidence": evidence}
        if len(best) < limit:
            heapq.heappush(best, (priority, result))
        else:
            heapq.heapreplace(best, (priority, result))
    ranked = [item[1] for item in best]
    ranked.sort(key=lambda item: (-item["confidence"], int(item["left_product_id"]), int(item["right_product_id"])))
    eligible_other = total - (1 if anchor is not None else 0)
    return {"items": ranked, "unconfirmed_candidates": candidate_count, "rule_version": RULE_VERSION,
            "scan": {"scanned_products": len(products), "eligible_products": total, "evaluated_pairs": evaluated,
                     "limit": CANDIDATE_SCAN_LIMIT, "bounded": eligible_other > len(products), "returned_limit": limit,
                     "description": "最近完成详情的商品内建议；尚未完成详情的商品不参与，也不自动确认任何关系。"}}


def matching_summary(session, settings):
    candidates = candidate_matches(session, settings, limit=1)
    confirmed = session.scalar(select(func.count()).select_from(ProductMatch).where(*market_scope(ProductMatch, settings), ProductMatch.status == MatchStatus.CONFIRMED)) or 0
    confirmed_skus = session.scalar(select(func.count()).select_from(SkuMatch).join(ProductMatch, ProductMatch.id == SkuMatch.product_match_id).where(
        *market_scope(ProductMatch, settings), ProductMatch.status == MatchStatus.CONFIRMED, SkuMatch.status == MatchStatus.CONFIRMED)) or 0
    return {"confirmed_product_pairs": confirmed, "confirmed_sku_pairs": confirmed_skus,
            "unconfirmed_candidates": candidates["unconfirmed_candidates"], "candidate_scan": candidates["scan"],
            "candidate_count_is_complete": not candidates["scan"]["bounded"], "rule_version": RULE_VERSION}
