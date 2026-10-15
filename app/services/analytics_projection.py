"""Build one bounded projection from a consistent source transaction."""

from collections import Counter
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import undefer

from app.models import ChangeEvent, Product, ProductImage, ProductSnapshot, ProductVariant, Shop, VariantSnapshot
from app.models.analytics_read import EventReadModel, ProductReadModel
from app.services.analytics_refresh import VERSION, invalidate_statistics, job_row, now, scope
from app.services.product_images import cover_images


def json_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(v) for v in value]
    return value


FIELDS = ("title", "product_url", "original_category", "normalized_category", "brand", "gender", "season", "style",
          "fit", "garment_length", "material", "pattern", "sales_count", "sales_count_text", "review_count", "rating",
          "currency", "pricing_context", "price_min", "price_max", "original_price_min", "original_price_max",
          "discount_rate", "stock_status", "availability_status", "color_summary", "size_summary", "first_seen_at",
          "last_seen_at", "last_success_at")


def price_kind(low, high):
    return "unknown" if low is None else "starting_price" if high is None else "exact" if Decimal(str(low)) == Decimal(str(high)) else "range"


def build_product(db, config, product_id, revision):
    product = db.get(Product, product_id)
    if product is None or product.platform != config.platform or product.region != config.region:
        return []
    shop = db.get(Shop, product.shop_id)
    stamp = now(db)
    data = {name: json_value(getattr(product, name)) for name in FIELDS}
    data.update(id=str(product.id), platform_product_id=product.platform_product_id, shop_id=str(product.shop_id),
                shop_name=shop.canonical_name, category=product.normalized_category or product.original_category or "unknown",
                detail_collected=product.last_success_at is not None, image_url=None, thumbnail_url=None, image_observed_at=None)
    cover = cover_images(db, [product.id]).get(product.id)
    if cover:
        data.update(json_value(cover))
    # Only expand this product's observation membership, never a catalog join.
    identifiers = (product.raw_attributes or {}).get("observed_sku_ids", [])
    identifiers = [identifiers] if isinstance(identifiers, str) else identifiers if isinstance(identifiers, list) else []
    identifiers = {value for value in identifiers if isinstance(value, str)}
    if len(identifiers) > 50000 or sum(len(value.encode()) for value in identifiers) > 8 * 1024 * 1024:
        raise ValueError("observed_sku_set_too_large")
    states, cells = Counter(), Counter()
    ordered = sorted(identifiers)
    for offset in range(0, len(ordered), 500):
        values = db.execute(select(ProductVariant.platform_variant_id, ProductVariant.color, ProductVariant.size,
            ProductVariant.stock_status).where(ProductVariant.product_id == product.id,
                ProductVariant.currency == product.currency, ProductVariant.platform_variant_id.in_(ordered[offset:offset + 500])))
        for identifier, color, size, status in values:
            # MySQL comparisons may be case insensitive or ignore trailing spaces.
            if identifier not in identifiers:
                continue
            states[str(status)] += 1
            color, size = (color or "").strip(), (size or "").strip()
            if color and size:
                cells[(color, size, str(status))] += 1
    data["variant_count"] = sum(states.values()) if product.last_success_at else None
    data["price_kind"] = price_kind(data["price_min"], data["price_max"])
    history_scope = (ProductSnapshot.product_id == product.id, ProductSnapshot.source_status == 200,
                     ProductSnapshot.currency == config.currency, ProductSnapshot.pricing_context == config.pricing_context,
                     ProductSnapshot.captured_at <= stamp)
    current_history_scope = (ProductSnapshot.product_id == product.id, ProductSnapshot.source_status == 200,
        ProductSnapshot.currency == product.currency, ProductSnapshot.pricing_context == product.pricing_context,
        ProductSnapshot.captured_at <= stamp)
    history = db.execute(select(func.count(), func.count(func.distinct(ProductSnapshot.captured_at)),
        func.min(ProductSnapshot.captured_at), func.max(ProductSnapshot.captured_at)).where(*current_history_scope)).one()
    facts = {"sku_states": dict(states), "sku_cells": [[c, s, status, n] for (c, s, status), n in sorted(cells.items())],
             "history_count": history[0], "history_observations": history[1],
             "history_first": json_value(history[2]), "history_last": json_value(history[3])}
    current = make_product_row(product, data, facts, "current", revision, stamp)
    current["category"] = (product.normalized_category or "").strip() or (product.original_category or "").strip() or "unknown"
    snapshot = db.scalar(select(ProductSnapshot).where(*history_scope)
        .order_by(ProductSnapshot.captured_at.desc(), ProductSnapshot.id.desc()).limit(1))
    record = {"id": str(product.id), "platform_product_id": product.platform_product_id, "shop_id": str(shop.id),
        "shop_name": shop.canonical_name, "platform_shop_id": shop.platform_shop_id, "product_url": product.product_url,
        "currency": config.currency, "pricing_context": config.pricing_context, "snapshot_id": str(snapshot.id) if snapshot else None}
    latest_states = Counter()
    visible = product.last_seen_at <= stamp
    if snapshot:
        observation = db.scalar(select(ProductSnapshot.raw_payload["observation"]).where(ProductSnapshot.id == snapshot.id))
        has_observation = isinstance(observation, dict) and bool(observation)
        for name in ("title", "original_category"):
            record[name] = observation.get(name) if has_observation else getattr(product, name) if visible else None
        record.update({name: json_value(getattr(snapshot, name)) for name in ("price_min", "price_max", "discount_rate", "stock_status")})
        for status, count in db.execute(select(VariantSnapshot.stock_status, func.count()).where(
            VariantSnapshot.product_snapshot_id == snapshot.id).group_by(VariantSnapshot.stock_status)):
            latest_states[str(status)] = count
        record["observed_at"] = snapshot.captured_at.isoformat()
        conflicts = (observation.get("raw_attributes") or {}).get("attribute_conflicts") if has_observation else None
        record["attribute_conflict_count"] = len(conflicts) if isinstance(conflicts, (dict, list)) else 0
    else:
        record.update({name: data.get(name) if visible else None for name in (
            "title", "original_category", "price_min", "price_max", "discount_rate", "stock_status")})
        record.update(observed_at=product.last_seen_at.isoformat() if visible else None, attribute_conflict_count=0)
    record.update(category=record.pop("original_category") or "unknown", category_basis="unknown",
                  sku_count=sum(latest_states.values()), sku_in_stock=latest_states["in_stock"],
                  sku_out_of_stock=latest_states["out_of_stock"], sku_unknown=latest_states["unknown"] + latest_states["partial"],
                  evidence_level="historical_detail" if snapshot else "current_observation" if visible else "identity_only_at_cutoff")
    record["category_basis"] = "platform_category" if record["category"] != "unknown" else "unknown"
    record["stock_status"] = record["stock_status"] or "unknown"
    record["price_kind"] = price_kind(record["price_min"], record["price_max"])
    eligible = bool(snapshot) or not visible or (product.currency == config.currency and product.pricing_context == config.pricing_context)
    historical = make_product_row(product, record, {}, "latest_valid_snapshot", revision, stamp, eligible=eligible)
    return [current, historical]


def make_product_row(product, data, facts, basis, revision, stamp, *, eligible=True):
    observed = data.get("observed_at", data.get("last_seen_at"))
    return dict(schema_version=VERSION, basis=basis, product_id=product.id, platform=product.platform, region=product.region,
        currency=data["currency"], pricing_context=data["pricing_context"], shop_id=product.shop_id,
        platform_product_id=product.platform_product_id, title=data.get("title"), category=data["category"],
        brand=data.get("brand"), price_min=Decimal(data["price_min"]) if data.get("price_min") is not None else None,
        stock_status=data.get("stock_status") or "unknown", sku_count=data.get("variant_count", data.get("sku_count")),
        rating=Decimal(data["rating"]) if data.get("rating") is not None else None, review_count=data.get("review_count"),
        observed_at=datetime.fromisoformat(observed) if observed else None, eligible=eligible, payload=data, facts=facts,
        applied_revision=revision, projected_at=stamp)


def publish_product(db, config, rows):
    for values in rows:
        key = (VERSION, values["basis"], values["product_id"])
        previous = db.get(ProductReadModel, key)
        if previous is not None and previous.applied_revision > values["applied_revision"]:
            continue
        if previous is None:
            db.add(ProductReadModel(**values))
        else:
            for name, value in values.items():
                setattr(previous, name, value)
    db.flush()
    invalidate_statistics(db, config)


def build_event(db, config, event_id):
    event = db.get(ChangeEvent, event_id)
    if event is None:
        return None
    product = db.get(Product, event.product_id)
    if product.platform != config.platform or product.region != config.region:
        return None
    snapshot = db.get(ProductSnapshot, event.snapshot_id)
    title = db.scalar(select(ProductSnapshot.raw_payload["observation"]["title"].as_string()).where(ProductSnapshot.id == snapshot.id))
    variant = db.get(ProductVariant, event.variant_id) if event.variant_id else None
    sku = variant.platform_variant_id if variant else None
    options = db.scalar(select(ProductSnapshot.raw_payload["observed_content"]["variant_options"][sku]).where(
        ProductSnapshot.id == snapshot.id)) if sku else None
    payload = dict(id=str(event.id), product_id=str(product.id), platform_product_id=product.platform_product_id,
        title=title, event_type=str(event.event_type), variant_id=str(event.variant_id) if event.variant_id else None,
        platform_variant_id=sku, variant_options=options if isinstance(options, dict) else {},
        snapshot_id=str(snapshot.id), previous_snapshot_id=str(event.previous_snapshot_id) if event.previous_snapshot_id else None,
        old_value=event.old_value, new_value=event.new_value, observed_at=snapshot.captured_at.isoformat(),
        detected_at=event.detected_at.isoformat())
    return dict(schema_version=VERSION, event_id=event.id, platform=product.platform, region=product.region,
        currency=snapshot.currency, pricing_context=snapshot.pricing_context, product_id=product.id, shop_id=product.shop_id,
        event_type=str(event.event_type), title=title, observed_at=snapshot.captured_at, payload=json_value(payload))


def publish_event(db, config, values):
    if values is None or db.get(EventReadModel, (VERSION, values["event_id"])) is not None:
        return
    control = job_row(db, "control", {**scope(config), "name": "event_sequence"})
    sequence = int(control.parameters.get("sequence", 0)) + 1
    control.parameters = {**control.parameters, "sequence": sequence}
    db.add(EventReadModel(**values, published_sequence=sequence))
    db.flush()
    invalidate_statistics(db, config)
